"""Pipeline orchestrator.

Drives the FSM: assemble context → invoke Claude → parse contract →
checkpoint → route → save state.  Public surface is :class:`Orchestrator`
plus a small dataclass for the run summary.

The orchestrator is deliberately stage-agnostic: it talks to stages
through the :class:`Stage` interface and to the model through the
:class:`ClaudeAdapter` interface.  This keeps tests focused and the
implementation substitutable.

v4 routing (plan §C, spec §5.2):

- The orchestrator iterates :class:`StageConfig` records rather than
  hard-coded stage ids.
- After each stage, the next stage is chosen by:
  1. If the stage's output was JSON and the JSON contains a ``route``
     field whose value is a known, enabled stage id → jump there.
  2. Otherwise → the next stage in the resolved stage list.
- A ``route_history`` counter is maintained so repeated jumps
  (write ↔ review loops) are bounded by
  ``execution.max_review_iterations``; on overflow the current
  stage's ``on_failure`` disposition is applied (default: pause).
- ``enabled: false`` stages are skipped wholesale: no Claude call,
  no product file, no batch tick.  A jump to a disabled stage is
  counted in ``route_history`` (so a misconfigured prompt cannot
  bypass the loop ceiling — A17) and triggers ``on_failure``.

Backward compatibility (spec §5.5.1, A1):

- v3 yaml (``pipeline.template`` only, or ``template`` +
  ``stages_override``) is still loaded; the orchestrator uses the
  legacy v3 stage classes so the existing test suite keeps passing.
- Deprecation warnings for the v3 fields are emitted at run start
  (see :func:`novelforge.config.deprecation_warnings_for`).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .claude.adapter import ClaudeAdapter, ClaudeCLIAdapter, MockClaudeAdapter
from .claude.context import ContextAssembler
from .claude.tokens import TokenUsageLog
from .config import (
    NovelProjectConfig,
    StageConfig,
    deprecation_warnings_for,
    stage_ids_for,
    with_max_chapters,
)
from .errors import (
    CheckpointCorrupt,
    ContextOverflow,
    FundamentIssue,
    NovelForgeError,
    OutputParseError,
    RateLimited,
    ReviewLoopExceeded,
    RouteCycleExceeded,
    SchemaInvalid,
    StageDisabled,
    StateError,
    WriteFailure,
)
from .review.gate import ReviewGate
from .stages import GenericStage, Stage, build_stage_registry, build_v4_stage, is_v4_config
from .stages.base import StageContext, StageExecutionResult
from .state import Checkpoint, State, StateStore
from .templates import get_template
from .utils.fs import ensure_dir, sha256_file
from .utils import count_words
from .utils.log import get_logger, log_stage_enter, log_stage_exit

log = get_logger("orchestrator")


@dataclass
class RunSummary:
    """Result of a full pipeline run (or run attempt)."""

    ok: bool = False
    paused: bool = False
    paused_reason: Optional[str] = None
    status: str = "ok"
    stages_run: int = 0
    chapters_written: int = 0
    total_tokens: int = 0
    next_stage: Optional[str] = None
    decision_reason: str = ""
    forced_stage: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "status": self.status,
            "stages_run": self.stages_run,
            "chapters_written": self.chapters_written,
            "total_tokens": self.total_tokens,
            "next_stage": self.next_stage,
            "decision_reason": self.decision_reason,
            "forced_stage": self.forced_stage,
            "extras": dict(self.extras),
        }


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class Orchestrator:
    """High-level pipeline driver.

    Parameters
    ----------
    config:
        Validated :class:`NovelProjectConfig`.
    config_path:
        Path to the yaml on disk (used to resolve the project root).
    project_root:
        Directory containing the project files.
    use_mock:
        If True, swap in :class:`MockClaudeAdapter` (handy for tests and
        offline runs).  Also auto-enabled when ``--use-mock`` is set on
        the CLI or when the ``NOVELFORGE_MOCK`` env var is truthy.
    max_chapters_override, skip_polish:
        Convenience hooks for debugging and small samples.
    """

    def __init__(
        self,
        *,
        config: NovelProjectConfig,
        config_path: Path,
        project_root: Path,
        use_mock: bool = False,
        max_chapters_override: Optional[int] = None,
        skip_polish: bool = False,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.project_root = Path(project_root)
        self.state_dir = self.project_root / ".novelforge"
        self.state = StateStore(self.state_dir)
        if max_chapters_override is not None:
            self.config = with_max_chapters(self.config, max_chapters_override)
        self.skip_polish = skip_polish
        self._use_mock = use_mock
        # Adapter and gate are constructed lazily so tests can patch
        # ``_build_adapter`` / ``_build_gate``.
        self._adapter: Optional[ClaudeAdapter] = None
        self._gate: Optional[ReviewGate] = None
        self._stages: Optional[dict[str, Stage]] = None
        self._v4_stage: Optional[GenericStage] = None
        self._is_v4: bool = is_v4_config(self.config)

    # -- factories -----------------------------------------------------

    def _build_adapter(self) -> ClaudeAdapter:
        usage_log = TokenUsageLog(self.state.logs_dir / "token-usage.log")
        if self._use_mock:
            return MockClaudeAdapter(usage_log=usage_log)
        return ClaudeCLIAdapter(usage_log=usage_log)

    def _build_gate(self) -> ReviewGate:
        return ReviewGate()

    def _build_stages(self) -> dict[str, Stage]:
        if self._is_v4:
            # The v4 path uses a single GenericStage; the registry
            # exists only so the existing per-id lookup in _drive
            # stays uniform.
            return {"_generic_": self._require_v4_stage()}
        return build_stage_registry(self._require_adapter(), self._require_gate())

    def _require_adapter(self) -> ClaudeAdapter:
        if self._adapter is None:
            self._adapter = self._build_adapter()
        return self._adapter

    def _require_gate(self) -> ReviewGate:
        if self._gate is None:
            self._gate = self._build_gate()
        return self._gate

    def _require_stages(self) -> dict[str, Stage]:
        if self._stages is None:
            self._stages = self._build_stages()
        return self._stages

    def _require_v4_stage(self) -> GenericStage:
        if self._v4_stage is None:
            self._v4_stage = build_v4_stage(self._require_adapter())
        return self._v4_stage

    # -- public --------------------------------------------------------

    def run(
        self, *, fresh: bool, force_stage: Optional[str] = None
    ) -> dict[str, Any]:
        """Execute the pipeline.  Returns a JSON-serializable summary."""

        # Emit deprecation warnings for v3 fields (A1, spec §5.5.1).
        for msg in deprecation_warnings_for(self.config):
            log.warning("DeprecationWarning: %s", msg)

        self.state.ensure_dirs()
        stages = self._resolve_stages()
        if not stages:
            log.warning("no stages to run; pipeline is empty")
            return RunSummary(ok=True, status="empty", decision_reason="no_stages").to_dict()

        # Clear stale state when running fresh.
        if fresh:
            self._clear_state_for_fresh_run()

        # When the user explicitly calls resume (fresh=False), clear the
        # paused flag so the pipeline can attempt to continue.  Per spec:
        # "暂停状态可被 resume 命令拉起".  If the underlying issue
        # persists the pipeline will pause again after retrying.
        if not fresh and not force_stage:
            existing = self.state.load()
            if existing.paused:
                log.info(
                    "clearing paused flag on explicit resume (was: %s)",
                    existing.paused_reason,
                )
                existing.paused = False
                existing.paused_reason = None
                self.state.save(existing)

        # Decide where to start.
        plan = self.state.recovery_plan(
            project_root=self.project_root,
            stages=stages,
            force_stage=force_stage,
        )
        log.info(
            "recovery_plan: next=%s reason=%s paused=%s forced=%s",
            plan.next_stage,
            plan.reason,
            plan.paused,
            plan.forced,
        )

        if plan.paused:
            self._record_decision_reason(plan.reason)
            return RunSummary(
                paused=True,
                paused_reason=plan.paused_reason or "paused",
                status="paused",
                next_stage=plan.next_stage,
                decision_reason=plan.reason,
            ).to_dict()

        if plan.next_stage is None:
            log.info("pipeline already complete: %s", plan.reason)
            self._record_decision_reason(plan.reason)
            return RunSummary(
                ok=True,
                status="complete",
                decision_reason=plan.reason,
            ).to_dict()

        # If forced, record the decision reason and jump to that stage.
        current_state = self.state.load()
        if plan.forced:
            current_state.forced_stage = plan.forced_stage
        current_state.current_stage = plan.next_stage
        self.state.save(current_state)
        self._record_decision_reason(plan.reason)

        summary = RunSummary(next_stage=plan.next_stage, decision_reason=plan.reason)
        try:
            self._drive(stages, start_at=plan.next_stage, summary=summary)
        except FundamentIssue as exc:
            self._pause_with("FUNDAMENTAL_ISSUE", exc, summary)
            log.error("pipeline paused: FUNDAMENTAL_ISSUE (%s)", exc)
        except RouteCycleExceeded as exc:
            # A8: route loop exceeded -> pause (default on_failure).
            self._pause_with("RouteCycleExceeded", exc, summary)
        except RateLimited as exc:
            self._pause_with("RateLimited", exc, summary)
        except WriteFailure as exc:
            self._pause_with("WriteFailure", exc, summary)
        except ContextOverflow as exc:
            self._pause_with("ContextOverflow", exc, summary)
        except SchemaInvalid as exc:
            self._pause_with("SchemaInvalid", exc, summary)
        except StageDisabled as exc:
            # A7: route pointed at a disabled stage -> pause.
            self._pause_with("StageDisabled", exc, summary)
        except OutputParseError as exc:
            # A11: split regex did not match -> pause (default on_failure).
            self._pause_with("OutputParseError", exc, summary)
        except CheckpointCorrupt as exc:
            self._pause_with("CheckpointCorrupt", exc, summary)
        except NovelForgeError as exc:
            self._pause_with(type(exc).__name__, exc, summary)
        except Exception as exc:  # pragma: no cover - last resort
            self._pause_with("UnexpectedError", exc, summary)
            log.exception("pipeline failed unexpectedly")
        finally:
            if self._adapter is not None and hasattr(self._adapter, "usage_log"):
                usage_log = getattr(self._adapter, "usage_log", None)
                if usage_log is not None:
                    tin, tout = usage_log.total_tokens()
                    summary.total_tokens = tin + tout
            # On successful completion, clear any leftover paused flag
            # (e.g. from a prior pause that was overridden via --force-stage).
            if summary.ok:
                s = self.state.load()
                if s.paused:
                    log.info("clearing stale paused flag after successful completion")
                    s.paused = False
                    s.paused_reason = None
                    self.state.save(s)
        return summary.to_dict()

    # -- internals -----------------------------------------------------

    def _resolve_stages(self) -> list[str]:
        stages = list(stage_ids_for(self.config))
        if self.skip_polish and "final_polish" in stages:
            stages = [s for s in stages if s != "final_polish"]
        return stages

    def _stage_config_by_id(self, stage_id: str) -> Optional[StageConfig]:
        for s in self.config.pipeline.stages:
            if s.id == stage_id:
                return s
        return None

    def _clear_state_for_fresh_run(self) -> None:
        if self.state.state_path.exists():
            log.info("fresh run: removing existing state.yaml")
            self.state.state_path.unlink()
        # Drop checkpoint history too: fresh starts have no checkpoints.
        for cp in self.state.list_checkpoints():
            try:
                cp.unlink()
            except OSError:
                pass

    def _drive(
        self,
        stages: list[str],
        *,
        start_at: str,
        summary: RunSummary,
    ) -> None:
        registry = self._require_stages()
        if start_at not in stages:
            raise NovelForgeError(f"unknown stage: {start_at!r}")
        # StageConfig lookup index (used in the v4 path).
        configs_by_id = {s.id: s for s in self.config.pipeline.stages}
        # Per-stage review iteration counters, persisted in state.extra so
        # that resumes don't reset the count.
        persisted = self.state.load()
        review_iters: dict[str, int] = dict(
            persisted.extra.get("review_iterations", {}) or {}
        )
        route_history: list[dict[str, Any]] = list(
            persisted.extra.get("route_history", []) or []
        )

        cursor = stages.index(start_at)
        max_iter = self.config.execution.max_review_iterations

        while cursor < len(stages):
            stage_id = stages[cursor]
            stage_config = configs_by_id.get(stage_id)

            # A4: enabled=false stages are skipped wholesale.
            if stage_config is not None and not stage_config.enabled:
                log.info(
                    "stage_skipped stage=%s reason=enabled_false",
                    stage_id,
                )
                summary.stages_run += 1
                cursor += 1
                continue

            # Track loop iterations for review-style stages (A8).
            iter_count = 0
            if stage_id.startswith("review_") or (
                stage_config is not None
                and stage_config.output.endswith(".json")
            ):
                review_iters[stage_id] = review_iters.get(stage_id, 0) + 1
                iter_count = review_iters[stage_id]
                if iter_count > max_iter:
                    msg = (
                        f"RouteCycleExceeded stage={stage_id} "
                        f"iterations={iter_count} max={max_iter}; "
                        f"applying on_failure"
                    )
                    log.warning(msg)
                    route_history.append(
                        {
                            "stage": stage_id,
                            "iteration": iter_count,
                            "outcome": "cycle_exceeded",
                        }
                    )
                    self._checkpoint_current(
                        stage_id=stage_id,
                        batch=self._batch_for(stage_id),
                        files=[],
                        stage_result=None,
                    )
                    self._mark_state(
                        current_stage=stages[cursor + 1]
                        if cursor + 1 < len(stages)
                        else None,
                        last_review_iterations=iter_count,
                        review_loop_warning=True,
                        review_loop_stage=stage_id,
                        review_iterations=review_iters,
                        route_history=route_history,
                    )
                    if self._is_v4:
                        # v4 spec A8: apply on_failure (default pause).
                        self._apply_on_failure(
                            stage_id=stage_id,
                            reason=f"loop exceeded ({iter_count} > {max_iter})",
                            summary=None,
                        )
                    # v3 backward compat: accept the current version
                    # and move on.  The original orchestrator logged a
                    # warning + checkpoint and advanced.
                    cursor += 1
                    continue

            log_stage_enter(stage_id)
            t0 = time.monotonic()
            try:
                result = self._execute_with_retry(
                    registry=registry,
                    stage_id=stage_id,
                    stage_config=stage_config,
                )
            except FundamentIssue:
                raise
            duration = time.monotonic() - t0

            route = result.route
            # v3: FUNDAMENTAL_ISSUE halt path
            if route == "FUNDAMENTAL_ISSUE":
                self._checkpoint_current(
                    stage_id=stage_id,
                    batch=result.batch or "001",
                    files=result.files,
                    stage_result=result,
                )
                self._mark_state(
                    current_stage=stage_id,
                    last_review_iterations=iter_count,
                    review_iterations=review_iters,
                    route_history=route_history,
                )
                raise FundamentIssue(
                    f"review stage {stage_id} returned FUNDAMENTAL_ISSUE: "
                    f"{result.findings[:3]}"
                )

            # Persist checkpoint for this stage.
            self._checkpoint_current(
                stage_id=stage_id,
                batch=result.batch or "001",
                files=result.files,
                stage_result=result,
            )
            self._mark_state(
                current_stage=stages[cursor + 1] if cursor + 1 < len(stages) else None,
                last_review_iterations=(
                    iter_count
                    if (stage_id.startswith("review_") or
                        (stage_config and stage_config.output.endswith(".json")))
                    else None
                ),
                review_iterations=review_iters,
                route_history=route_history,
            )
            # Reset the loop counter only when the review actually
            # passed (APPROVED / DONE / no route).  NEEDS_REWRITE and
            # v4-style jumps keep the counter so the cycle ceiling
            # can fire.
            if (stage_id.startswith("review_") or (
                stage_config and stage_config.output.endswith(".json")
            )) and route in (None, "", "APPROVED", "DONE", "SKIPPED"):
                review_iters[stage_id] = 0
            log_stage_exit(stage_id, route=route, duration=duration)
            summary.stages_run += 1
            if stage_id == "write_chapter":
                written = self._chapter_files(self.project_root)
                summary.chapters_written = len(written)

            # Decide the next cursor via the v4 routing rules.
            next_cursor = self._next_cursor(
                stages=stages,
                cursor=cursor,
                stage_id=stage_id,
                route=route,
                result=result,
                review_iters=review_iters,
                max_iter=max_iter,
                route_history=route_history,
            )
            if next_cursor is None:
                # on_failure disposition triggered pause.
                return
            cursor = next_cursor

        summary.ok = True
        summary.status = "complete"

    # -- v4 routing ----------------------------------------------------

    def _next_cursor(
        self,
        *,
        stages: list[str],
        cursor: int,
        stage_id: str,
        route: str,
        result: StageExecutionResult,
        review_iters: dict[str, int],
        max_iter: int,
        route_history: list[dict[str, Any]],
    ) -> Optional[int]:
        """Decide which cursor to advance to after a stage completes.

        Returns ``None`` to signal that the current stage's
        ``on_failure`` has triggered a pause that the orchestrator
        must surface via ``_pause_with``.

        Rules (spec §5.2 + v3 backward compat):

        1. JSON stage with a ``route`` value that matches an enabled
           stage id → jump there.
        2. v3 backward-compat: ``route`` in {``NEEDS_REWRITE``,
           ``FUNDAMENTAL_ISSUE``, ``APPROVED``} → use the v3
           semantic (rewind / halt / natural next).
        3. JSON stage with a ``route`` value that doesn't match any
           known enabled id → on_failure (default pause).
        4. JSON stage without a ``route`` field → natural next.
        5. Text / split stages → natural next (A6).
        """

        # Only JSON outputs can carry a route.  Text / split stages
        # always advance.
        target = self._stage_config_by_id(stage_id)
        is_json = target is not None and target.output.endswith(".json")
        if not is_json:
            return cursor + 1
        # Missing / APPROVED / DONE / SKIPPED → natural next.
        if route in (None, "", "APPROVED", "DONE", "SKIPPED"):
            return cursor + 1
        # ---- v3 backward compat ----
        if route == "NEEDS_REWRITE":
            target_idx = self._rewind_to(stages, cursor)
            if target_idx is not None:
                return target_idx
            return cursor + 1
        if route == "FUNDAMENTAL_ISSUE":
            # Caller already raised FundamentIssue earlier; defensively
            # advance if it slipped through.
            return cursor + 1
        # ---- v4 path: treat route as a stage id ----
        if route not in stages:
            log.warning(
                "route_invalid stage=%s route=%s valid=%s",
                stage_id, route, stages,
            )
            self._apply_on_failure(
                stage_id=stage_id, reason=f"route '{route}' unknown", summary=None
            )
            return None
        target_cfg = self._stage_config_by_id(route)
        if target_cfg is not None and not target_cfg.enabled:
            log.warning(
                "route_target_disabled stage=%s route=%s",
                stage_id, route,
            )
            # Count the attempt in history (still) so the loop ceiling
            # cannot be bypassed by pointing at a disabled stage.
            review_iters[stage_id] = review_iters.get(stage_id, 0) + 1
            iter_count = review_iters[stage_id]
            if iter_count > max_iter:
                msg = (
                    f"RouteCycleExceeded stage={stage_id} "
                    f"iterations={iter_count} max={max_iter}; "
                    f"target '{route}' is disabled"
                )
                log.warning(msg)
                route_history.append(
                    {
                        "stage": stage_id,
                        "iteration": iter_count,
                        "outcome": "cycle_exceeded_disabled_target",
                        "target": route,
                    }
                )
                raise RouteCycleExceeded(msg)
            # A17: record the disabled-target attempt so the user can
            # see how many times the misconfigured route fired before
            # the threshold tripped.
            route_history.append(
                {
                    "stage": stage_id,
                    "target": route,
                    "iteration": iter_count,
                    "outcome": "target_disabled",
                }
            )
            # Apply the source stage's on_failure disposition.  ``skip``
            # re-executes the source (the loop continues until the
            # iter check above trips); ``pause``/``fail`` raise so the
            # orchestrator surfaces the run state.
            src_cfg = self._stage_config_by_id(stage_id)
            disposition = src_cfg.on_failure if src_cfg is not None else "pause"
            log.warning(
                "on_failure_triggered stage=%s disposition=%s reason=%s",
                stage_id,
                disposition,
                f"route '{route}' points to disabled stage",
            )
            if disposition == "skip":
                # Re-execute the source stage on the next loop pass.
                return cursor
            if disposition == "fail":
                raise NovelForgeError(
                    f"Fail: route '{route}' points to disabled stage"
                )
            # pause (default)
            raise NovelForgeError(
                f"pause: route '{route}' points to disabled stage"
            )
        # Valid jump.
        log.info("route_jump stage=%s → %s", stage_id, route)
        route_history.append(
            {
                "stage": stage_id,
                "target": route,
                "iteration": review_iters.get(stage_id, 0),
            }
        )
        return stages.index(route)

    @staticmethod
    def _rewind_to(stages: list[str], cursor: int) -> Optional[int]:
        """v3 backward-compat: rewind to the content stage being reviewed.

        For a stage id ``review_X``, return the index of stage ``X``;
        if missing, the immediately preceding stage; if there is
        none, return None.
        """

        review = stages[cursor]
        if not review.startswith("review_"):
            return None
        content_id = review[len("review_"):]
        if content_id in stages:
            return stages.index(content_id)
        if cursor > 0:
            return cursor - 1
        return None

    def _apply_on_failure(
        self,
        *,
        stage_id: str,
        reason: str,
        summary: Optional[RunSummary],
    ) -> None:
        """Apply the current stage's ``on_failure`` disposition.

        Mirrors the v3 behaviour: ``pause`` is the default; ``skip``
        advances to the next stage; ``fail`` pauses with reason
        prefixed ``Fail:``.  Pauses set ``summary.paused = True`` so
        the CLI exit code is 3.
        """

        target = self._stage_config_by_id(stage_id)
        disposition = target.on_failure if target is not None else "pause"
        log.warning(
            "on_failure_triggered stage=%s disposition=%s reason=%s",
            stage_id, disposition, reason,
        )
        if disposition == "skip":
            return
        if disposition == "fail":
            raise NovelForgeError(f"Fail: {reason}")
        # pause (default)
        raise NovelForgeError(f"pause: {reason}")

    def _execute_with_retry(
        self,
        *,
        registry: dict[str, Stage],
        stage_id: str,
        stage_config: Optional[StageConfig],
    ) -> StageExecutionResult:
        """Invoke the stage with retry/backoff on retryable errors.

        Errors that are NOT retried (caller handles):

        - ``FundamentIssue`` (review veto)
        - ``SchemaInvalid`` (model misbehaviour)
        - ``CheckpointCorrupt`` (state is broken)
        - ``OutputParseError`` (A11 — split did not match)
        - ``RouteCycleExceeded`` (A8)
        - ``StageDisabled`` (A7)
        """

        retry = self.config.execution.retry
        attempts = 0
        last_exc: Optional[Exception] = None
        stage = registry.get(stage_id) or registry.get("_generic_")
        if stage is None:
            raise NovelForgeError(f"unknown stage: {stage_id!r}")
        while attempts <= retry.max_retries:
            try:
                ctx = StageContext(
                    config=self.config,
                    project_root=self.project_root,
                    stage_id=stage_id,
                    batch=self._batch_for(stage_id),
                    extras={"stage_config": stage_config} if stage_config is not None else {},
                )
                return stage.execute(ctx)
            except (RateLimited, WriteFailure, ContextOverflow) as exc:
                last_exc = exc
                if attempts >= retry.max_retries:
                    break
                wait = self._backoff_seconds(retry.backoff, attempts, retry.max_wait)
                log.warning(
                    "stage=%s attempt=%d failed (%s); sleeping %.1fs before retry",
                    stage_id,
                    attempts + 1,
                    exc,
                    wait,
                )
                time.sleep(wait)
                attempts += 1
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _backoff_seconds(strategy: str, attempt: int, max_wait: int) -> float:
        if strategy == "exponential":
            wait = min(max_wait, 2 ** attempt)
        elif strategy == "linear":
            wait = min(max_wait, attempt + 1)
        else:
            wait = 1.0
        return float(wait)

    def _batch_for(self, stage_id: str) -> str:
        if stage_id == "write_chapter":
            n = self._next_chapter_index(self.project_root)
            return f"{n:03d}"
        return "001"

    def _checkpoint_current(
        self,
        *,
        stage_id: str,
        batch: str,
        files: list[Path],
        stage_result: Optional[StageExecutionResult],
    ) -> None:
        """Record a checkpoint file with file hashes."""

        file_entries: list[dict[str, str]] = []
        for path in files:
            if not path.exists():
                continue
            rel = str(path.relative_to(self.project_root))
            try:
                file_entries.append(
                    {
                        "path": rel,
                        "sha256": sha256_file(path),
                        "size": str(path.stat().st_size),
                    }
                )
            except OSError as exc:
                log.warning("could not hash %s: %s", rel, exc)
        cp = Checkpoint(
            stage=stage_id,
            batch=batch,
            files=file_entries,
            timestamp=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            extras={
                "token_usage": {
                    "input": stage_result.token_usage_in if stage_result else 0,
                    "output": stage_result.token_usage_out if stage_result else 0,
                },
            },
        )
        self.state.write_checkpoint(cp)

    def _mark_state(
        self,
        *,
        current_stage: Optional[str],
        last_review_iterations: Optional[int],
        review_loop_warning: bool = False,
        review_loop_stage: Optional[str] = None,
        review_iterations: Optional[dict[str, int]] = None,
        route_history: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        s = self.state.load()
        s.current_stage = current_stage
        # ``None`` means "don't touch this field" — used by non-review
        # stages so the value reflects the most recent review loop
        # instead of being clobbered to 0.
        if last_review_iterations is not None:
            s.last_review_iterations = last_review_iterations
        s.last_checkpoint_at = datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )
        # token usage sum
        usage_log = self.state.logs_dir / "token-usage.log"
        if usage_log.exists():
            tin, tout = TokenUsageLog(usage_log).total_tokens()
            s.token_usage["total_input"] = tin
            s.token_usage["total_output"] = tout
        # chapter progress
        s.progress["chapters_written"] = len(
            list((self.project_root / "output" / "chapters").glob("*.md"))
        )
        s.progress["chapters_reviewed"] = self._count_reviewed(self.project_root)
        s.progress["total_words"] = self._total_words(self.project_root)
        if stage_progress_key := self._stage_progress_key(current_stage):
            s.progress[stage_progress_key] = "complete"
        if review_iterations is not None:
            s.extra["review_iterations"] = dict(review_iterations)
        if route_history is not None:
            # Truncate the persisted history to the configured maximum
            # so it doesn't grow unbounded over long runs (A8 / §6).
            limit = self.config.execution.route_history_max
            trimmed = route_history[-limit:] if len(route_history) > limit else list(route_history)
            s.extra["route_history"] = trimmed
        if review_loop_warning:
            s.extra.setdefault("review_loop_warnings", []).append(
                {
                    "stage": review_loop_stage or current_stage,
                    "iterations": last_review_iterations or 0,
                    "timestamp": s.last_checkpoint_at,
                }
            )
        self.state.save(s)

    @staticmethod
    def _stage_progress_key(stage_id: Optional[str]) -> Optional[str]:
        return {
            "generate_outline": "outline",
            "review_outline": "outline",
            "design_characters": "characters",
            "review_characters": "characters",
            "simulate_plot": "simulation",
            "review_simulation": "simulation",
        }.get(stage_id or "")

    @staticmethod
    def _chapter_files(project_root: Path) -> list[Path]:
        chap_dir = project_root / "output" / "chapters"
        if not chap_dir.exists():
            return []
        return sorted(chap_dir.glob("*.md"))

    @staticmethod
    def _next_chapter_index(project_root: Path) -> int:
        import re
        nums: list[int] = []
        for p in (project_root / "output" / "chapters").glob("*.md"):
            m = re.match(r"^(\d{3,})", p.name)
            if m:
                nums.append(int(m.group(1)))
        return (max(nums) + 1) if nums else 1

    @staticmethod
    def _count_reviewed(project_root: Path) -> int:
        review_dir = project_root / "output" / "review"
        if not review_dir.exists():
            return 0
        return sum(1 for _ in review_dir.glob("*-review.json"))

    @staticmethod
    def _total_words(project_root: Path) -> int:
        total = 0
        chap_dir = project_root / "output" / "chapters"
        if not chap_dir.exists():
            return 0
        for p in chap_dir.glob("*.md"):
            total += count_words(p.read_text(encoding="utf-8", errors="ignore"))
        return total

    def _pause_with(
        self, reason: str, exc: BaseException, summary: RunSummary
    ) -> None:
        log.error("pipeline paused reason=%s (%s)", reason, exc)
        s = self.state.load()
        s.paused = True
        s.paused_reason = reason
        s.current_stage = s.current_stage or summary.next_stage
        s.last_checkpoint_at = datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )
        self.state.save(s)
        summary.paused = True
        summary.paused_reason = reason
        summary.status = "paused"

    def _record_decision_reason(self, reason: str) -> None:
        s = self.state.load()
        s.recovery["last_decision_reason"] = reason
        s.recovery["last_batch_status"] = "resuming" if reason.startswith("resuming") else reason
        self.state.save(s)
