"""GenericStage — single v4 stage executor.

v4 collapses the ten v3 stage classes into one :class:`GenericStage`.
The per-stage behaviour (which prompt to load, where to write output,
how to parse the model's reply) is fully described by the
:class:`novelforge.config.StageConfig` record, so the runtime only
needs one class to drive every step of the pipeline.

The flow (per spec §5 / plan D2 / T11–T13):

1. Resolve the prompt (inline text **or** file path that exists on
   disk) — see :func:`_resolve_prompt`.
2. Render placeholders via :class:`PromptRenderer` (T05/T06).
3. Build the per-stage context (delegated to the existing
   :class:`ContextAssembler` so token budgeting is unchanged).
4. Invoke the Claude adapter.
5. Persist the model's reply using
   :mod:`novelforge.claude.output_parser` based on the form
   inferred from ``StageConfig.output`` (T03/T12).
6. For JSON output, surface the ``route`` field on the result so
   the orchestrator can jump (T13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..claude.adapter import ClaudeAdapter, StageResult
from ..claude.context import ContextAssembler, PromptRenderer
from ..claude.output_parser import OutputForm, infer_form, parse
from ..config import ContextSpec, NovelProjectConfig, StageConfig
from ..errors import ConfigError
from ..stages._helpers import ensure_output_dirs
from ..stages.base import Stage, StageContext, StageExecutionResult
from ..utils.fs import atomic_write, ensure_dir
from ..utils.log import get_logger

log = get_logger("stages.generic")


def _resolve_prompt(prompt: str, project_root: Path) -> str:
    """Return the prompt body for a v4 stage.

    Rules (per plan D2):

    - ``prompt`` contains ``\\n`` → inline, use as-is.
    - Otherwise treat as a project-relative file path; load it if the
      file exists.
    - Otherwise treat the bare string as inline text (no ``\\n`` is
      still a valid single-line prompt).
    """

    if not isinstance(prompt, str):
        raise ConfigError("stage prompt must be a string")
    if "\n" in prompt:
        return prompt
    candidate = project_root / prompt
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8", errors="ignore")
    # Fallback: treat as inline text.
    return prompt


def _resolve_model(stage: StageConfig, cfg: NovelProjectConfig) -> str:
    """Pick a model for a stage when the config does not pin one.

    Today the engine passes the stage's explicit ``model`` to Claude;
    this helper exists so tests can synthesise a default that
    matches the v3 behaviour (review stages use ``review_model``,
    everyone else uses ``write_model``).
    """

    if stage.model:
        return stage.model
    if stage.id.startswith("review_"):
        return cfg.execution.review_model
    return cfg.execution.write_model


def _build_ctx_map(stage: StageConfig, ctx: StageContext) -> dict[str, Any]:
    """Build the ``{{ctx.*}}`` substitution map for a stage run."""

    return {
        "stage_id": stage.id,
        "batch": ctx.batch or "001",
        "chapter_index": ctx.chapter_index,
        "iteration": 0,
    }


@dataclass
class _RawOutputInfo:
    """Internal holder between parse() and the StageExecutionResult."""

    form: OutputForm
    files: list[Path]
    raw_output: str
    parsed_data: Optional[dict[str, Any]] = None
    segments: list[dict[str, Any]] = None  # type: ignore[type-arg]


class GenericStage(Stage):
    """One stage to rule them all.

    Constructed without arguments; the :class:`StageConfig` is passed
    to :meth:`execute` via :class:`StageContext`.  The orchestrator
    builds a single :class:`GenericStage` instance and reuses it for
    every step (T14 / T11).
    """

    id = "_generic_"
    name = "Generic Stage"

    def __init__(self, adapter: Optional[ClaudeAdapter] = None) -> None:
        self.adapter = adapter

    # The orchestrator may not have wired the adapter by the time the
    # stage is constructed; we accept a late binding via
    # ``StageContext.extras['adapter']`` (legacy code path) or
    # :meth:`set_adapter`.

    def set_adapter(self, adapter: ClaudeAdapter) -> None:
        self.adapter = adapter

    # -- main entry point ---------------------------------------------

    def execute(self, ctx: StageContext) -> StageExecutionResult:
        """Run a single v4 stage and persist the output.

        The :class:`StageConfig` is read from ``ctx.extras['stage_config']``
        which the orchestrator populates for every step.
        """

        stage_config: Optional[StageConfig] = (
            ctx.extras.get("stage_config") if ctx.extras else None
        )
        if stage_config is None:
            raise ConfigError(
                "GenericStage.execute requires stage_config in StageContext.extras"
            )
        return self._run(stage_config, ctx)

    # -- core path ----------------------------------------------------

    def _run(
        self,
        stage: StageConfig,
        ctx: StageContext,
    ) -> StageExecutionResult:
        cfg: NovelProjectConfig = ctx.config
        if not stage.enabled:
            # Defensive: orchestrator should skip these itself.
            log.info("stage_disabled stage=%s", stage.id)
            return StageExecutionResult(
                stage_id=stage.id,
                raw_output="",
                files=[],
                batch=ctx.batch,
                route="SKIPPED",
            )

        # 1. Resolve prompt text + render placeholders.
        prompt_text = _resolve_prompt(stage.prompt, ctx.project_root)
        renderer = PromptRenderer(
            project_root=ctx.project_root,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        rendered = renderer.render(
            prompt_text,
            ctx=_build_ctx_map(stage, ctx),
        )
        log.info(
            "stage_prompt stage=%s novel=%d ctx=%d includes=%d warnings=%d",
            stage.id,
            rendered.novel_expansions,
            rendered.ctx_expansions,
            len(rendered.include_files),
            len(rendered.warnings),
        )

        # 2. Build context slices (re-use ContextAssembler).
        ensure_output_dirs(ctx.project_root)
        assembler = ContextAssembler(
            project_root=ctx.project_root,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        chapter_index = ctx.chapter_index or self._next_chapter_index(ctx.project_root)
        assembled = assembler.assemble(
            stage.id,
            batch=ctx.batch,
            chapter_index=chapter_index,
        )
        full_prompt = assembled.render() + "\n\n" + rendered.text

        # 3. Invoke the model.
        if self.adapter is None:
            raise ConfigError("GenericStage has no adapter wired")
        result: StageResult = self.adapter.invoke(
            full_prompt,
            stage=stage.id,
            model=_resolve_model(stage, cfg),
            batch=ctx.batch or "001",
        )

        # 4. Persist output based on the inferred form.
        form = infer_form(stage.output)
        info = self._persist(
            stage=stage,
            form=form,
            raw_output=result.raw_output,
            project_root=ctx.project_root,
            chapter_index=chapter_index,
        )

        # 5. Build the route decision.
        route = "APPROVED"
        if info.parsed_data is not None:
            route = str(info.parsed_data.get("route") or "APPROVED")
        elif info.segments:
            # Pick the first segment's parsed route (if any).
            for seg in info.segments:
                if isinstance(seg, dict) and "route" in seg:
                    route = str(seg["route"])
                    break

        return StageExecutionResult.from_adapter(
            stage.id,
            result,
            files=info.files,
            batch=ctx.batch or "001",
            route=route,
            extras={"form": form},
        )

    # -- internals ----------------------------------------------------

    def _persist(
        self,
        *,
        stage: StageConfig,
        form: OutputForm,
        raw_output: str,
        project_root: Path,
        chapter_index: int,
    ) -> _RawOutputInfo:
        """Persist ``raw_output`` based on the inferred form.

        Returns a small bundle so the caller can populate
        :class:`StageExecutionResult` with the right ``files`` /
        ``route``.
        """

        if form == "text":
            target = project_root / stage.output
            ensure_dir(target.parent)
            atomic_write(target, raw_output)
            return _RawOutputInfo(
                form="text",
                files=[target],
                raw_output=raw_output,
            )
        if form == "json":
            # Delegate to output_parser.parse() so spec A9 is enforced:
            # a non-JSON payload for a `.json` stage raises SchemaInvalid
            # rather than being silently written verbatim.
            placeholder_values = {
                "num": chapter_index,
                "chapter_index": chapter_index,
            }
            result = parse(
                raw_output,
                form="json",
                output_template=stage.output,
                split_regex=None,
                project_root=project_root,
                stage_id=stage.id,
                placeholder_values=placeholder_values,
            )
            payload = result.data if isinstance(result.data, dict) else None
            return _RawOutputInfo(
                form="json",
                files=[result.written_path] if result.written_path else [],
                raw_output=raw_output,
                parsed_data=payload,
            )
        if form == "split":
            if not stage.split:
                raise ConfigError(
                    f"stage {stage.id!r}: output template {stage.output!r} "
                    f"contains placeholders but `split` regex is missing"
                )
            result = parse(
                raw_output,
                form="split",
                output_template=stage.output,
                split_regex=stage.split,
                project_root=project_root,
                stage_id=stage.id,
            )
            # `parse` may not have access to chapter_index; substitute
            # it if the user references {{num}} in their template.
            return _RawOutputInfo(
                form="split",
                files=result.written_paths,
                raw_output=raw_output,
                segments=result.segments,
            )
        raise ConfigError(f"unknown output form: {form!r}")

    @staticmethod
    def _next_chapter_index(project_root: Path) -> int:
        chap_dir = project_root / "output" / "chapters"
        if not chap_dir.exists():
            return 1
        nums: list[int] = []
        for p in chap_dir.glob("*.md"):
            m = re.match(r"^(\d{3,})", p.name)
            if m:
                nums.append(int(m.group(1)))
        return (max(nums) + 1) if nums else 1


__all__ = ["GenericStage"]
