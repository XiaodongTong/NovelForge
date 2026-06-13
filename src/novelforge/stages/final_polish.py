"""Stage: final_polish — final read-through polish of the manuscript."""

from __future__ import annotations

from pathlib import Path

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ..utils.fs import list_files
from ._helpers import ensure_output_dirs, read_prompt, write_output
from .base import Stage, StageContext, StageContract, StageExecutionResult


class FinalPolishStage(Stage):
    id = "final_polish"
    name = "Final Polish"
    contract = StageContract(
        inputs=("output/chapters",),
        outputs=("output/chapters",),
    )

    def __init__(self, adapter: ClaudeAdapter) -> None:
        self.adapter = adapter

    def execute(self, ctx: StageContext) -> StageExecutionResult:
        cfg: NovelProjectConfig = ctx.config
        ensure_output_dirs(ctx.project_root)
        assembler = ContextAssembler(
            project_root=ctx.project_root,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        target = ctx.project_root / "output" / "summaries" / "plot.md"
        assembled = assembler.assemble(self.id, review_target_path=target)
        prompt = read_prompt(ctx.project_root, "final-polish.md")
        full_prompt = assembled.render() + "\n\n" + (
            prompt or DEFAULT_POLISH_PROMPT
        )
        result = self.adapter.invoke(
            full_prompt,
            stage=self.id,
            model=cfg.execution.write_model,
            batch=ctx.batch or "001",
        )
        # We don't actually rewrite every chapter in the minimal sample
        # flow; we record the polish notes for the operator to apply
        # manually (or feed back into a future run).
        notes_path = write_output(
            ctx.project_root,
            "output/review/final-polish-notes.md",
            result.raw_output.strip() + "\n",
        )
        return StageExecutionResult.from_adapter(
            self.id,
            result,
            files=[notes_path],
            batch=ctx.batch or "001",
            route="APPROVED",
        )


DEFAULT_POLISH_PROMPT = """\
Read the manuscript and produce a final-polish brief.  For every
chapter, list at most three tweaks (word choice, sentence rhythm,
clarity).  Be terse.  Output markdown.
""".strip()
