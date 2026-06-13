"""Pipeline orchestrator (v4 contract model).

Drives the contract pipeline: for each :class:`StageConfig`, the
orchestrator walks the dual-layer retry matrix (A-tier infrastructure
errors + C-tier model-incomplete errors), drives batch stages item by
item, persists progress to ``state.yaml``, and registers each
successful produce in the :class:`ArtifactRegistry`.

Highlights (spec §5.4 / plan D3-D14):

- **Single GenericStage instance** — every step is driven through the
  same :class:`GenericStage` executor; the per-step behaviour comes
  from :class:`StageConfig`.
- **Dual-layer retry** —

  * A-tier (``RateLimited`` / ``WriteFailure`` / ``ContextOverflow``)
    is handled by :meth:`_execute_with_retry`'s inner loop with the
    configured backoff (``execution.retry``); does **not** increment
    the C-tier counter.
  * C-tier (``StageIncomplete`` / ``VerifyFailed``) is handled by the
    outer loop; each failure injects an ``attempt_hint`` and retries
    up to ``done_when.max_attempts``; on exhaustion the stage's
    ``on_failure`` disposition applies.

- **Batch driving** — a ``batch: N`` stage runs N times in series,
  with each item getting its own ``StageContext.batch`` (``"001"``,
  ``"002"``, …) and its own entry in ``state.extra.stage_attempts``
  (``list[int]`` of length N).  A non-batch stage stores a single
  ``int`` (plan D14).
- **State persistence** — every successful produce is registered in
  the :class:`ArtifactRegistry` and serialised to
  ``state.extra.artifacts`` so checkpoints / resumes see a consistent
  view.
- **Purely linear pipeline** — v3 routing tokens are gone; the next
  stage is always the next entry in ``pipeline.stages`` (spec §AC-15,
  §AC-18).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from .artifact_registry import ArtifactRegistry
from .claude.adapter import (
    ClaudeAdapter,
    ClaudeCLIAdapter,
    MockClaudeAdapter,
)
from .claude.tokens import TokenUsageLog
from .config import (
    NovelProjectConfig,
    StageConfig,
    stage_ids_for,
    with_max_chapters,
)
from .errors import (
    CheckpointCorrupt,
    ContextOverflow,
    NovelForgeError,
    OutputParseError,
    RateLimited,
    SchemaInvalid,
    StateError,
    StageIncomplete,
    StageDisabled,
    VerifyFailed,
    WriteFailure,
)
from .stages import build_v4_stage
from .stages.base import StageContext, StageExecutionResult
from .state import Checkpoint, State, StateStore
from .utils.fs import ensure_dir, sha256_file
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
# Helpers
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _attempt_state_key(stage_id: str) -> str:
    return f"stage_attempts.{stage_id}"


def _coerce_attempts(
    raw: Any, *, batch: int
) -> Union[int, list[int]]:
    """Coerce a persisted attempts value into the expected form.

    Raises :class:`CheckpointCorrupt` when a batch stage's persisted
    attempts length doesn't match ``batch`` (plan D14).
    """

    if batch > 1:
        if isinstance(raw, list):
            cleaned = [int(x) if isinstance(x, (int, float)) else 0 for x in raw]
            if len(cleaned) != batch:
                raise CheckpointCorrupt(
                    f"persisted attempts length {len(cleaned)} does not match "
                    f"batch={batch}; run with --fresh to reset"
                )
            return cleaned
        if raw is None:
            return [0] * batch
        # Tolerate a stray int (legacy form) — expand to a list.
        if isinstance(raw, (int, float)):
            return [int(raw)] + [0] * (batch - 1)
        return [0] * batch
    if isinstance(raw, list):
        # Tolerate a stray list; fold to int.
        return int(raw[0]) if raw else 0
    if isinstance(raw, (int, float)):
        return int(raw)
    return 0


def _serialize_registry(registry: ArtifactRegistry, project_root: Path) -> dict[str, Any]:
    """Return a yaml-friendly ``{stage_id: {alias: path | [paths]}}`` map.

    Paths are stored relative to ``project_root`` so the persisted
    state is portable across machines.
    """

    raw = registry.to_dict()
    out: dict[str, dict[str, Any]] = {}
    for sid, bucket in raw.items():
        out[sid] = {}
        for alias, value in bucket.items():
            if isinstance(value, list):
                rels: list[str] = []
                for p in value:
                    try:
                        rels.append(str(Path(p).relative_to(project_root)))
                    except ValueError:
                        rels.append(str(p))
                out[sid][alias] = rels
            else:
                try:
                    out[sid][alias] = str(Path(value).relative_to(project_root))
                except ValueError:
                    out[sid][alias] = str(value)
    return out


def _deserialize_registry(raw: Any, project_root: Path) -> ArtifactRegistry:
    """Inverse of :func:`_serialize_registry`."""

    if not isinstance(raw, Mapping):
        return ArtifactRegistry()
    normalised: dict[str, dict[str, Any]] = {}
    for sid, bucket in raw.items():
        if not isinstance(bucket, Mapping):
            continue
        normalised[sid] = {}
        for alias, value in bucket.items():
            if isinstance(value, list):
                normalised[sid][alias] = [
                    (project_root / p).resolve() for p in value
                ]
            else:
                normalised[sid][alias] = (project_root / str(value)).resolve()
    return ArtifactRegistry.from_dict(normalised)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class Orchestrator:
    """High-level pipeline driver (v4 contract model)."""

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
        self._adapter: Optional[ClaudeAdapter] = None
        self._v4_stage = None  # type: ignore[assignment]
        self._registry: Optional[ArtifactRegistry] = None

    # -- factories -----------------------------------------------------

    def _build_adapter(self) -> ClaudeAdapter:
        usage_log = TokenUsageLog(self.state.logs_dir / "token-usage.log")
        if self._use_mock:
            return MockClaudeAdapter(usage_log=usage_log)
        return ClaudeCLIAdapter(usage_log=usage_log)

    def _require_adapter(self) -> ClaudeAdapter:
        if self._adapter is None:
            self._adapter = self._build_adapter()
        return self._adapter

    def _require_stage(self):
        if self._v4_stage is None:
            self._v4_stage = build_v4_stage(self._require_adapter())
        return self._v4_stage

    def _require_registry(self) -> ArtifactRegistry:
        if self._registry is None:
            # Try to restore from state.extra.artifacts first.
            existing = self.state.load()
            raw_artifacts = existing.extra.get("artifacts")
            if raw_artifacts:
                try:
                    self._registry = _deserialize_registry(
                        raw_artifacts, self.project_root
                    )
                except (StateError, TypeError, ValueError) as exc:
                    log.warning(
                        "failed to deserialise persisted artifacts (%s); "
                        "starting with an empty registry",
                        exc,
                    )
                    self._registry = ArtifactRegistry()
            else:
                self._registry = ArtifactRegistry()
        return self._registry

    # -- public --------------------------------------------------------

    def run(
        self, *, fresh: bool, force_stage: Optional[str] = None
    ) -> dict[str, Any]:
        """Execute the pipeline.  Returns a JSON-serializable summary."""

        self.state.ensure_dirs()
        stages = self._resolve_stages()
        if not stages:
            log.warning("no stages to run; pipeline is empty")
            return RunSummary(
                ok=True, status="empty", decision_reason="no_stages"
            ).to_dict()

        # Always (re)build the registry from persisted state unless the
        # user explicitly requested a fresh run.
        if fresh:
            self._clear_state_for_fresh_run()
            self._registry = ArtifactRegistry()
        else:
            self._require_registry()

        # When the user explicitly calls resume, clear the paused flag.
        if not fresh and not force_stage:
            existing = self.state.load()
            if existing.paused:
                log.info(
                    "clearing paused flag on explicit resume (was: %s)",
                    existing.paused_reason,
                )
                existing.paused = False
                existing.paused_reason = None
                # AC-10: a pause ends the previous attempt budget; the
                # resuming stage starts a fresh ``max_attempts`` round.
                paused_stage_id = existing.current_stage
                if paused_stage_id:
                    attempts_map = dict(existing.extra.get("stage_attempts") or {})
                    if paused_stage_id in attempts_map:
                        stage_cfg = self._stage_config_by_id(paused_stage_id)
                        if stage_cfg is not None and stage_cfg.batch > 1:
                            attempts_map[paused_stage_id] = [0] * stage_cfg.batch
                        else:
                            attempts_map[paused_stage_id] = 0
                        existing.extra["stage_attempts"] = attempts_map
                        log.info(
                            "reset_stage_attempts stage=%s (AC-10 resume)",
                            paused_stage_id,
                        )
                self.state.save(existing)

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

        current_state = self.state.load()
        if plan.forced:
            current_state.forced_stage = plan.forced_stage
        current_state.current_stage = plan.next_stage
        self.state.save(current_state)
        self._record_decision_reason(plan.reason)

        summary = RunSummary(next_stage=plan.next_stage, decision_reason=plan.reason)
        try:
            self._drive(stages, start_at=plan.next_stage, summary=summary)
        except (RateLimited, WriteFailure, ContextOverflow) as exc:
            self._pause_with(type(exc).__name__, exc, summary)
        except (SchemaInvalid, OutputParseError) as exc:
            self._pause_with(type(exc).__name__, exc, summary)
        except StageDisabled as exc:
            self._pause_with("StageDisabled", exc, summary)
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
            if summary.ok:
                s = self.state.load()
                if s.paused:
                    log.info("clearing stale paused flag after successful completion")
                    s.paused = False
                    s.paused_reason = None
                    self.state.save(s)
        return summary.to_dict()

    # -- internals: stage iteration -----------------------------------

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
        if start_at not in stages:
            raise NovelForgeError(f"unknown stage: {start_at!r}")
        configs_by_id = {s.id: s for s in self.config.pipeline.stages}
        registry = self._require_registry()
        stage_exec = self._require_stage()

        cursor = stages.index(start_at)
        while cursor < len(stages):
            stage_id = stages[cursor]
            stage_config = configs_by_id.get(stage_id)
            if stage_config is None:
                raise NovelForgeError(
                    f"no StageConfig for stage {stage_id!r}"
                )
            if not stage_config.enabled:
                log.info(
                    "stage_skipped stage=%s reason=enabled_false",
                    stage_id,
                )
                summary.stages_run += 1
                cursor += 1
                continue

            log_stage_enter(stage_id)
            t0 = time.monotonic()
            try:
                self._drive_stage(stage_config, stage_exec, registry, summary)
            except _OnFailureApplied:
                # The stage exhausted its retries and the configured
                # ``on_failure`` disposition was pause / fail.  Exit.
                return
            duration = time.monotonic() - t0
            log_stage_exit(stage_id, route="ok", duration=duration)
            summary.stages_run += 1
            self._mark_state(current_stage=stages[cursor + 1] if cursor + 1 < len(stages) else None)
            cursor += 1

        summary.ok = True
        summary.status = "complete"

    # -- per-stage driving --------------------------------------------

    def _drive_stage(
        self,
        stage: StageConfig,
        stage_exec,
        registry: ArtifactRegistry,
        summary: RunSummary,
    ) -> None:
        """Drive a single stage end-to-end (batch items included)."""

        if stage.batch > 1:
            self._drive_batch(stage, stage_exec, registry, summary)
        else:
            self._drive_single(stage, stage_exec, registry, summary)

    def _drive_single(
        self,
        stage: StageConfig,
        stage_exec,
        registry: ArtifactRegistry,
        summary: RunSummary,
    ) -> None:
        """Drive a non-batch stage through the dual-layer retry matrix."""

        attempts = self._load_attempts(stage, item=None)
        while True:
            attempt = attempts + 1
            try:
                result = self._execute_with_retry(
                    stage_exec=stage_exec,
                    stage=stage,
                    registry=registry,
                    batch="001",
                    attempt=attempt,
                    last_failure=self._last_failure_for(stage.id),
                )
            except (StageIncomplete, VerifyFailed) as exc:
                attempts += 1
                self._persist_attempts(stage, attempts, item=None)
                self._record_last_failure(stage.id, exc)
                if attempts >= stage.done_when.max_attempts:
                    skipped = self._apply_on_failure(
                        stage_id=stage.id,
                        reason=f"max_attempts={stage.done_when.max_attempts} "
                        f"exhausted ({type(exc).__name__})",
                        summary=summary,
                    )
                    if skipped:
                        # on_failure: skip → advance to the next stage.
                        return
                    raise _OnFailureApplied()
                wait = self._backoff_seconds(
                    self.config.execution.retry.backoff,
                    attempts - 1,
                    self.config.execution.retry.max_wait,
                )
                log.warning(
                    "stage=%s attempt=%d failed (%s); retrying after %.1fs",
                    stage.id, attempts, exc, wait,
                )
                time.sleep(wait)
                continue
            # Success — checkpoint, register, advance.
            self._checkpoint_current(
                stage_id=stage.id,
                batch="001",
                files=result.files,
                stage_result=result,
            )
            self._clear_last_failure(stage.id)
            self._persist_attempts(stage, 0, item=None)
            return

    def _drive_batch(
        self,
        stage: StageConfig,
        stage_exec,
        registry: ArtifactRegistry,
        summary: RunSummary,
    ) -> None:
        """Drive a ``batch: N`` stage item by item (plan D14)."""

        n = stage.batch
        attempts_list = self._load_attempts(stage, item=None)
        if not isinstance(attempts_list, list):
            attempts_list = [0] * n
        if len(attempts_list) != n:
            raise CheckpointCorrupt(
                f"stage {stage.id!r}: persisted attempts length "
                f"{len(attempts_list)} does not match batch={n}; "
                f"run with --fresh to reset"
            )

        # Per-alias accumulator so multi-produce batch stages register
        # each alias as its own length-N list (AC-7).  Single-produce
        # batch stages collapse to the same flat list as before.
        per_alias_paths: dict[str, list[Path]] = {
            p.alias: [] for p in stage.produces
        }
        successful_paths: list[Path] = []
        for idx in range(1, n + 1):
            batch_label = f"{idx:03d}"
            item_attempts = attempts_list[idx - 1]
            while True:
                attempt = item_attempts + 1
                try:
                    result = self._execute_with_retry(
                        stage_exec=stage_exec,
                        stage=stage,
                        registry=registry,
                        batch=batch_label,
                        attempt=attempt,
                        last_failure=self._last_failure_for(
                            f"{stage.id}.{batch_label}"
                        ),
                    )
                except (StageIncomplete, VerifyFailed) as exc:
                    item_attempts += 1
                    attempts_list[idx - 1] = item_attempts
                    self._persist_attempts(stage, attempts_list, item=None)
                    self._record_last_failure(
                        f"{stage.id}.{batch_label}", exc
                    )
                    if item_attempts >= stage.done_when.max_attempts:
                        log.warning(
                            "batch_item_exhausted stage=%s item=%s attempts=%d",
                            stage.id, batch_label, item_attempts,
                        )
                        # Per D14: prior successful items stay
                        # registered; remaining items do not run; the
                        # stage as a whole walks on_failure.
                        skipped = self._apply_on_failure(
                            stage_id=stage.id,
                            reason=f"batch item {batch_label} exhausted "
                            f"max_attempts={stage.done_when.max_attempts} "
                            f"({type(exc).__name__})",
                            summary=summary,
                        )
                        # Persist whatever items succeeded before bailing.
                        if successful_paths:
                            self._register_batch_paths(
                                stage, registry, per_alias_paths
                            )
                        if skipped:
                            return
                        raise _OnFailureApplied()
                    wait = self._backoff_seconds(
                        self.config.execution.retry.backoff,
                        item_attempts - 1,
                        self.config.execution.retry.max_wait,
                    )
                    log.warning(
                        "batch_retry stage=%s item=%s attempt=%d (%s); sleeping %.1fs",
                        stage.id, batch_label, item_attempts, exc, wait,
                    )
                    time.sleep(wait)
                    continue
                # Item succeeded — record its files and move on.
                successful_paths.extend(result.files)
                item_paths = result.extras.get("produces_paths") or {}
                for alias, paths in item_paths.items():
                    if alias in per_alias_paths:
                        per_alias_paths[alias].extend(paths)
                self._checkpoint_current(
                    stage_id=stage.id,
                    batch=batch_label,
                    files=result.files,
                    stage_result=result,
                )
                self._clear_last_failure(f"{stage.id}.{batch_label}")
                attempts_list[idx - 1] = 0
                self._persist_attempts(stage, attempts_list, item=None)
                break

        # Register each alias as its own length-N list (AC-7).  Note
        # that GenericStage already registers each individual produce
        # when the call succeeds; the explicit re-register here keeps
        # the data flow consistent for downstream ``{{upstream.*[*]}}``.
        if successful_paths:
            self._register_batch_paths(stage, registry, per_alias_paths)

    @staticmethod
    def _register_batch_paths(
        stage: StageConfig,
        registry: ArtifactRegistry,
        per_alias_paths: Mapping[str, Sequence[Path]],
    ) -> None:
        """Persist batch produces as a list[Path] under each alias.

        ``GenericStage._register_produces`` already registers the
        per-item single path on success; we override it here so each
        alias holds the complete length-N list expected by downstream
        ``{{upstream.<id>.<alias>[*]}}`` references.
        """

        for produce in stage.produces:
            paths = per_alias_paths.get(produce.alias) or []
            if not paths:
                continue
            registry.register(stage.id, produce.alias, list(paths))

    # -- retry / backoff ----------------------------------------------

    def _execute_with_retry(
        self,
        *,
        stage_exec,
        stage: StageConfig,
        registry: ArtifactRegistry,
        batch: str,
        attempt: int,
        last_failure: Optional[Mapping[str, Any]],
    ) -> StageExecutionResult:
        """Inner A-tier retry loop.

        Catches ``RateLimited`` / ``WriteFailure`` / ``ContextOverflow``
        (infrastructure errors) up to ``execution.retry.max_retries``
        with backoff; lets C-tier errors (``StageIncomplete`` /
        ``VerifyFailed``) propagate to :meth:`_drive_single` /
        :meth:`_drive_batch`.
        """

        retry = self.config.execution.retry
        attempts = 0
        last_exc: Optional[Exception] = None
        while True:
            try:
                ctx = StageContext(
                    config=self.config,
                    project_root=self.project_root,
                    stage_id=stage.id,
                    batch=batch,
                    extras={
                        "stage_config": stage,
                        "registry": registry,
                        "attempt": attempt,
                        "last_failure": dict(last_failure) if last_failure else None,
                    },
                )
                return stage_exec.execute(ctx)
            except (RateLimited, WriteFailure, ContextOverflow) as exc:
                last_exc = exc
                if attempts >= retry.max_retries:
                    raise
                wait = self._backoff_seconds(
                    retry.backoff, attempts, retry.max_wait
                )
                log.warning(
                    "stage=%s batch=%s A-tier attempt=%d failed (%s); sleeping %.1fs",
                    stage.id, batch, attempts + 1, exc, wait,
                )
                time.sleep(wait)
                attempts += 1

    @staticmethod
    def _backoff_seconds(strategy: str, attempt: int, max_wait: int) -> float:
        if strategy == "exponential":
            wait = min(max_wait, 2 ** max(0, attempt))
        elif strategy == "linear":
            wait = min(max_wait, attempt + 1)
        else:
            wait = 1.0
        return float(wait)

    # -- on_failure disposition --------------------------------------

    def _apply_on_failure(
        self,
        *,
        stage_id: str,
        reason: str,
        summary: Optional[RunSummary],
    ) -> bool:
        """Apply the current stage's ``on_failure`` disposition.

        Returns ``True`` for ``skip`` (caller should advance to the
        next stage), ``False`` for ``pause`` / ``fail`` (caller should
        propagate the surfaced exception).
        """

        target = self._stage_config_by_id(stage_id)
        disposition = target.on_failure if target is not None else "pause"
        log.warning(
            "on_failure_triggered stage=%s disposition=%s reason=%s",
            stage_id, disposition, reason,
        )
        if disposition == "skip":
            return True
        if disposition == "fail":
            raise NovelForgeError(f"Fail: {reason}")
        # pause (default)
        raise NovelForgeError(f"pause: {reason}")

    # -- state persistence --------------------------------------------

    def _load_attempts(
        self,
        stage: StageConfig,
        *,
        item: Optional[int],
    ) -> Union[int, list[int]]:
        """Load persisted attempts for ``stage`` from ``state.extra``."""

        s = self.state.load()
        raw_attempts = (s.extra.get("stage_attempts") or {}).get(stage.id)
        return _coerce_attempts(raw_attempts, batch=stage.batch)

    def _persist_attempts(
        self,
        stage: StageConfig,
        value: Union[int, list[int]],
        *,
        item: Optional[int],
    ) -> None:
        s = self.state.load()
        attempts_map = dict(s.extra.get("stage_attempts") or {})
        attempts_map[stage.id] = value
        s.extra["stage_attempts"] = attempts_map
        # Persist the registry too so resumes see consistent produces.
        registry = self._require_registry()
        s.extra["artifacts"] = _serialize_registry(registry, self.project_root)
        self.state.save(s)

    def _last_failure_for(self, key: str) -> Optional[Mapping[str, Any]]:
        s = self.state.load()
        bucket = s.extra.get("last_failures") or {}
        return bucket.get(key)

    def _record_last_failure(self, key: str, exc: BaseException) -> None:
        s = self.state.load()
        bucket = dict(s.extra.get("last_failures") or {})
        detail: Any = getattr(exc, "detail", None) if hasattr(exc, "detail") else None
        bucket[key] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "detail": dict(detail) if isinstance(detail, Mapping) else (str(detail) if detail is not None else ""),
        }
        s.extra["last_failures"] = bucket
        self.state.save(s)

    def _clear_last_failure(self, key: str) -> None:
        s = self.state.load()
        bucket = dict(s.extra.get("last_failures") or {})
        if key in bucket:
            del bucket[key]
            s.extra["last_failures"] = bucket
            self.state.save(s)

    def _checkpoint_current(
        self,
        *,
        stage_id: str,
        batch: str,
        files: Sequence[Path],
        stage_result: Optional[StageExecutionResult],
    ) -> None:
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
            timestamp=_now_iso(),
            extras={
                "token_usage": {
                    "input": stage_result.token_usage_in if stage_result else 0,
                    "output": stage_result.token_usage_out if stage_result else 0,
                },
            },
        )
        self.state.write_checkpoint(cp)

    def _mark_state(self, *, current_stage: Optional[str]) -> None:
        s = self.state.load()
        s.current_stage = current_stage
        s.last_checkpoint_at = _now_iso()
        usage_log = self.state.logs_dir / "token-usage.log"
        if usage_log.exists():
            tin, tout = TokenUsageLog(usage_log).total_tokens()
            s.token_usage["total_input"] = tin
            s.token_usage["total_output"] = tout
        # Persist the registry alongside the state save.
        registry = self._require_registry()
        s.extra["artifacts"] = _serialize_registry(registry, self.project_root)
        self.state.save(s)

    def _pause_with(
        self, reason: str, exc: BaseException, summary: RunSummary
    ) -> None:
        log.error("pipeline paused reason=%s (%s)", reason, exc)
        s = self.state.load()
        s.paused = True
        s.paused_reason = reason
        s.current_stage = s.current_stage or summary.next_stage
        s.last_checkpoint_at = _now_iso()
        # Persist the registry so the resume sees the same data flow.
        try:
            registry = self._require_registry()
            s.extra["artifacts"] = _serialize_registry(
                registry, self.project_root
            )
        except Exception:  # pragma: no cover - defensive
            pass
        self.state.save(s)
        summary.paused = True
        summary.paused_reason = reason
        summary.status = "paused"

    def _record_decision_reason(self, reason: str) -> None:
        s = self.state.load()
        s.recovery["last_decision_reason"] = reason
        s.recovery["last_batch_status"] = (
            "resuming" if reason.startswith("resuming") else reason
        )
        self.state.save(s)


class _OnFailureApplied(Exception):
    """Sentinel raised internally to short-circuit the stage driving loop
    after ``on_failure`` has been applied (pause / fail).
    """

    def __init__(self) -> None:
        super().__init__("on_failure applied; stopping stage driving")
