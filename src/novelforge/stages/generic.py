"""GenericStage — the single v4 stage executor.

v4 collapses every per-stage class into one :class:`GenericStage`.  The
per-stage behaviour is fully described by the
:class:`novelforge.config.StageConfig` record (``produces`` /
``done_when`` / ``consumes`` / ``batch``) passed to :meth:`execute`
through :class:`StageContext.extras`.  The runtime drives every step of
the pipeline through this class.

The transactional flow (per spec §5.3 / plan D2 / AC-1..AC-3):

1. Resolve the prompt body (inline text or project-relative file path).
2. Render placeholders (``{{novel.*}}`` / ``{{ctx.*}}`` /
   ``{{include:}}`` / ``{{upstream.*}}``) via :class:`PromptRenderer`.
3. Build the upstream context slices via :class:`ContextAssembler`
   (registry-driven, honours the stage's ``consumes`` contract).
4. Append the ``attempt_hint`` when the orchestrator is on a C-tier
   retry (AC-17) — supplied via ``StageContext.extras['attempt']`` /
   ``['last_failure']``.
5. Invoke the Claude adapter (suffix appending is delegated to
   :func:`novelforge.claude.adapter.build_prompt` so this stage stays
   suffix-agnostic).
6. **First-layer check** — if the stage expects a completion signal
   (``done_when.completion_signal`` non-null) and the adapter reports
   ``result.completion_signal is False`` → raise :class:`StageIncomplete`
   (tier C).  The orchestrator catches this and retries the whole stage
   with an ``attempt_hint``.
7. Persist every declared ``produces[]`` via
   :mod:`novelforge.claude.output_parser` (one file, multi-file, batch,
   or split — handled by the form flag).
8. **Second-layer check** — run every ``done_when.checks`` entry against
   the produced files.  Any failure → raise :class:`VerifyFailed`
   (tier C).  As with the first layer, the orchestrator retries.
9. Register the produced file(s) under each ``produces[].alias`` in the
   :class:`ArtifactRegistry` so downstream stages can reference them via
   ``{{upstream.<stage_id>.<alias>}}``.

The stage deliberately does **not** loop on its own failures — every
``raise`` propagates to the orchestrator, which owns the dual-layer
retry matrix.  Tests assert that a single :meth:`execute` call triggers
at most one adapter invoke.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from ..artifact_registry import ArtifactRegistry
from ..claude.adapter import ClaudeAdapter, StageResult
from ..claude.context import ContextAssembler, PromptRenderer
from ..claude.output_parser import ParseResult, parse
from ..config import NovelProjectConfig, StageConfig
from ..errors import ConfigError, StageIncomplete
from ..stages.base import Stage, StageContext, StageExecutionResult
from ..utils.log import get_logger
from ..verify import (
    DEFAULT_COMPLETION_SIGNAL,
    DoneWhenSpec,
    verify_or_raise,
)

log = get_logger("stages.generic")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve_prompt(prompt: str, project_root: Path) -> str:
    """Return the prompt body for a v4 stage.

    Resolution rules:

    - If the body already contains a newline → use as-is (inline text).
    - Otherwise check whether ``prompt`` is a project-relative file
      path that exists on disk; if so, read it.
    - Otherwise treat the bare string as inline text (a single-line
      prompt is valid).
    """

    if not isinstance(prompt, str):
        raise ConfigError("stage prompt must be a string")
    if "\n" in prompt:
        return prompt
    candidate = project_root / prompt
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8", errors="ignore")
    return prompt


def _resolve_model(stage: StageConfig, cfg: NovelProjectConfig) -> str:
    """Pick the model name to send to Claude."""

    if stage.model:
        return stage.model
    if stage.id.startswith("review_") or stage.id.startswith("judge"):
        return cfg.execution.review_model
    return cfg.execution.write_model


def _build_ctx_map(
    stage: StageConfig,
    ctx: StageContext,
    *,
    attempt: int,
    last_failure: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the ``{{ctx.*}}`` substitution map for a stage run."""

    return {
        "stage_id": stage.id,
        "batch": ctx.batch or "001",
        "chapter_index": ctx.chapter_index,
        "attempt": attempt,
        "last_failure_type": (last_failure or {}).get("type", ""),
        "last_failure_detail": (last_failure or {}).get("detail", ""),
    }


def _format_attempt_hint(
    attempt: int,
    last_failure: Optional[Mapping[str, Any]],
) -> str:
    """Render the C-tier retry hint appended to the prompt (AC-17).

    The hint is intentionally compact: a single fenced block the model
    can scan when constructing its second attempt.  Returning an empty
    string suppresses the block (e.g. when ``attempt == 1``).
    """

    if attempt <= 1 and not last_failure:
        return ""
    parts = [
        f"Attempt: {attempt}",
    ]
    if last_failure:
        ftype = last_failure.get("type") or ""
        detail = last_failure.get("detail") or ""
        if ftype:
            parts.append(f"Previous failure type: {ftype}")
        if detail:
            parts.append(f"Previous failure detail: {detail}")
    body = "; ".join(parts)
    return (
        f"\n\n<!-- NovelForge retry hint -->\n"
        f"Your previous attempt did not pass verification.  "
        f"Address the issue and emit the completion signal when done.\n"
        f"({body})"
    )


# --------------------------------------------------------------------------- #
# GenericStage
# --------------------------------------------------------------------------- #


class GenericStage(Stage):
    """One stage to rule them all.

    Constructed with a :class:`ClaudeAdapter`; the per-step
    :class:`StageConfig` is supplied to :meth:`execute` via
    :class:`StageContext.extras['stage_config']`.  The orchestrator
    builds a single instance and reuses it for every step (plan §2.2).
    """

    id = "_generic_"
    name = "Generic Stage"

    def __init__(self, adapter: Optional[ClaudeAdapter] = None) -> None:
        self.adapter = adapter

    def set_adapter(self, adapter: ClaudeAdapter) -> None:
        self.adapter = adapter

    # -- public --------------------------------------------------------

    def execute(self, ctx: StageContext) -> StageExecutionResult:
        """Run a single stage transactionally; never retries on its own."""

        stage_config: Optional[StageConfig] = (
            ctx.extras.get("stage_config") if ctx.extras else None
        )
        if stage_config is None:
            raise ConfigError(
                "GenericStage.execute requires stage_config in StageContext.extras"
            )
        registry: Optional[ArtifactRegistry] = ctx.extras.get("registry")
        if registry is None:
            raise ConfigError(
                "GenericStage.execute requires registry in StageContext.extras"
            )
        return self._run(stage_config, ctx, registry)

    # -- core ----------------------------------------------------------

    def _run(
        self,
        stage: StageConfig,
        ctx: StageContext,
        registry: ArtifactRegistry,
    ) -> StageExecutionResult:
        cfg: NovelProjectConfig = ctx.config
        if not stage.enabled:
            log.info("stage_disabled stage=%s", stage.id)
            return StageExecutionResult(
                stage_id=stage.id,
                raw_output="",
                files=[],
                batch=ctx.batch,
            )

        attempt = int(ctx.extras.get("attempt", 1) or 1)
        last_failure = ctx.extras.get("last_failure")

        # 1-2. Resolve prompt body + render placeholders.
        prompt_text = _resolve_prompt(stage.prompt, ctx.project_root)
        renderer = PromptRenderer(
            project_root=ctx.project_root,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=registry,
        )
        ctx_map = _build_ctx_map(
            stage, ctx, attempt=attempt, last_failure=last_failure
        )
        rendered = renderer.render(prompt_text, ctx=ctx_map, stage_id=stage.id)
        log.info(
            "stage_prompt stage=%s novel=%d ctx=%d upstream=%d includes=%d "
            "warnings=%d",
            stage.id,
            rendered.novel_expansions,
            rendered.ctx_expansions,
            rendered.upstream_expansions,
            len(rendered.include_files),
            len(rendered.warnings),
        )

        # 3. Build upstream context slices (registry-driven, consumes-aware).
        executed_stages: list[str] = []
        # The orchestrator records which stages have already produced
        # output; we filter by what the registry actually has so a
        # never-run stage never appears in the upstream list.
        executed_stages = [
            sid for sid in registry.stages() if sid != stage.id
        ]
        # Honour an explicit consumes order so a downstream stage can
        # ask for a subset of upstreams even when more are available.
        consumes = stage.consumes
        if consumes is not None:
            executed_stages = [c for c in consumes if c in executed_stages]
        assembler = ContextAssembler(
            project_root=ctx.project_root,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=registry,
        )
        assembled = assembler.assemble(
            stage.id,
            consumes=consumes,
            executed_stages=executed_stages,
        )
        attempt_hint = _format_attempt_hint(attempt, last_failure)
        full_prompt = (
            assembled.render()
            + "\n\n# Task instructions\n"
            + rendered.text
            + attempt_hint
        )

        # 4. Invoke the model.  build_prompt() appends EXECUTION_SUFFIX
        #    + COMPLETION_SUFFIX when appropriate.
        if self.adapter is None:
            raise ConfigError("GenericStage has no adapter wired")
        completion_signal = stage.done_when.completion_signal
        result: StageResult = self.adapter.invoke(
            full_prompt,
            stage=stage.id,
            model=_resolve_model(stage, cfg),
            batch=ctx.batch or "001",
            append_suffix=True,
            completion_signal=completion_signal,
        )

        # 5. First-layer completion-signal check.
        if completion_signal and not result.completion_signal:
            log.warning(
                "stage_incomplete stage=%s attempt=%d (no completion signal)",
                stage.id,
                attempt,
            )
            raise StageIncomplete(
                f"stage {stage.id!r} attempt {attempt}: model did not emit "
                f"the declared completion signal {completion_signal!r}",
                stage_id=stage.id,
                attempt=attempt,
            )

        # 6. Persist produces via the output parser.
        placeholder_values = self._placeholder_values(ctx, stage)
        parsed = parse(
            result.raw_output,
            stage.produces,
            project_root=ctx.project_root,
            stage_id=stage.id,
            placeholder_values=placeholder_values,
            completion_signal=completion_signal,
        )

        # 7. Second-layer done_when.checks verification.
        per_file_placeholders = self._per_file_placeholders(parsed, ctx, stage)
        verify_or_raise(
            stage.done_when,
            parsed.all_paths,
            ctx.project_root,
            placeholder_values,
            stage_id=stage.id,
            attempt=attempt,
            per_file_placeholders=per_file_placeholders,
        )

        # 8. Register produces in the ArtifactRegistry so downstream
        #    stages can reference them via {{upstream.*}}.
        self._register_produces(stage, parsed, registry)

        return StageExecutionResult.from_adapter(
            stage.id,
            result,
            files=parsed.all_paths,
            batch=ctx.batch,
            completion_signal=result.completion_signal,
            extras={
                "produces": [p.alias for p in parsed.produces],
                # Per-alias path mapping so the orchestrator can register
                # each batch produce as its own length-N list (AC-7).
                "produces_paths": {
                    p.alias: list(p.paths) for p in parsed.produces
                },
                "attempt": attempt,
            },
        )

    # -- placeholder plumbing -----------------------------------------

    @staticmethod
    def _placeholder_values(
        ctx: StageContext,
        stage: StageConfig,
    ) -> dict[str, Any]:
        """Values used to render ``{{num}}`` etc. in produces paths."""

        values: dict[str, Any] = {
            "stage_id": stage.id,
        }
        if ctx.batch and ctx.batch.isdigit():
            values["num"] = int(ctx.batch)
        if ctx.chapter_index is not None:
            values["chapter_index"] = ctx.chapter_index
        return values

    @staticmethod
    def _per_file_placeholders(
        parsed: ParseResult,
        ctx: StageContext,
        stage: StageConfig,
    ) -> list[dict[str, Any]]:
        """Per-file placeholder overrides driven by split captures.

        For split stages, each file's regex match groups become its own
        placeholder set (so a check ``target: output/c-{{num}}.md``
        evaluates against each file in turn).  Non-split stages get a
        single shared map (the global ``num`` from the batch id).
        """

        base = GenericStage._placeholder_values(ctx, stage)
        out: list[dict[str, Any]] = []
        for produce in parsed.produces:
            if produce.segments:
                for seg in produce.segments:
                    merged = dict(base)
                    matches = seg.get("matches") or {}
                    for k, v in matches.items():
                        # Numeric captures coerce to int so ``{{num}}``
                        # renders as ``001`` under the ``03d`` format.
                        if isinstance(v, str) and v.isdigit():
                            merged[k] = int(v)
                        else:
                            merged[k] = v
                    out.append(merged)
            else:
                # text / json produce — one shared map per file.
                for _ in produce.paths:
                    out.append(dict(base))
        return out

    @staticmethod
    def _register_produces(
        stage: StageConfig,
        parsed: ParseResult,
        registry: ArtifactRegistry,
    ) -> None:
        """Push the produced file(s) into the registry under each alias."""

        for produce in parsed.produces:
            if not produce.paths:
                continue
            if len(produce.paths) == 1 and not _stage_is_list_form(stage, produce.alias):
                registry.register(stage.id, produce.alias, produce.paths[0])
            else:
                registry.register(stage.id, produce.alias, list(produce.paths))


def _stage_is_list_form(stage: StageConfig, alias: str) -> bool:
    """Return True iff the produce at ``alias`` should be stored as a list.

    A produce is list-form when:

    - the stage is a batch stage (``batch > 1``) — every produce in a
      batch stage yields one file per batch item; or
    - the produce declares its own ``split`` regex (one file per regex
      match).
    """

    if stage.batch > 1:
        return True
    for p in stage.produces:
        if p.alias == alias:
            return p.split is not None
    return False


__all__ = ["GenericStage"]
