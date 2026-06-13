"""Stage: full_consistency_check — checks the entire written output for consistency."""

from __future__ import annotations

import json
from pathlib import Path

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ..utils.fs import list_files
from ._helpers import ensure_output_dirs, read_prompt, write_output
from .base import Stage, StageContext, StageContract, StageExecutionResult


class FullConsistencyCheckStage(Stage):
    id = "full_consistency_check"
    name = "Full Consistency Check"
    contract = StageContract(
        inputs=("output/chapters",),
        outputs=("output/review/consistency-report.md",),
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
        target = ctx.project_root / "output" / "summaries" / "outline-tracking.md"
        assembled = assembler.assemble(self.id, review_target_path=target)
        prompt = read_prompt(ctx.project_root, "consistency-check.md")
        full_prompt = assembled.render() + "\n\n" + (
            prompt or DEFAULT_CONSISTENCY_PROMPT
        )
        result = self.adapter.invoke(
            full_prompt,
            stage=self.id,
            model=cfg.execution.review_model,
            batch=ctx.batch or "001",
        )
        out = write_output(
            ctx.project_root,
            "output/review/consistency-report.md",
            result.raw_output.strip() + "\n",
        )
        return StageExecutionResult.from_adapter(
            self.id,
            result,
            files=[out],
            batch=ctx.batch or "001",
            route="APPROVED",
        )


DEFAULT_CONSISTENCY_PROMPT = """\
Read the outline and all chapter files.  Produce a consistency report
covering:

- Character name & trait consistency
- Foreshadowing payoff (every setup has a corresponding pay-off)
- Timeline continuity
- World rule violations

Be terse and list every issue with a chapter reference.  If nothing is
wrong, say so explicitly.
""".strip()
