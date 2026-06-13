"""Stage: simulate_plot — runs an internal "table read" against the plot.

Produces ``output/summaries/plot-simulation.md`` with notes on what
would happen if the outline were executed verbatim — escalation curves,
foreshadowing payoff, character motivation gaps, etc.
"""

from __future__ import annotations

from pathlib import Path

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ._helpers import ensure_output_dirs, read_prompt, write_output
from .base import Stage, StageContext, StageContract, StageExecutionResult


class SimulatePlotStage(Stage):
    id = "simulate_plot"
    name = "Simulate Plot"
    contract = StageContract(
        inputs=(
            "output/summaries/plot.md",
            "output/summaries/outline-tracking.md",
            "output/meta",
        ),
        outputs=("output/summaries/plot-simulation.md",),
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
        prompt = read_prompt(ctx.project_root, "simulate-plot.md")
        full_prompt = assembled.render() + "\n\n" + (
            prompt or DEFAULT_SIMULATE_PROMPT
        )
        result = self.adapter.invoke(
            full_prompt,
            stage=self.id,
            model=cfg.execution.write_model,
            batch=ctx.batch or "001",
        )
        out = write_output(
            ctx.project_root,
            "output/summaries/plot-simulation.md",
            (result.raw_output.strip() + "\n"),
        )
        return StageExecutionResult.from_adapter(
            self.id,
            result,
            files=[out],
            batch=ctx.batch or "001",
            route="APPROVED",
        )


DEFAULT_SIMULATE_PROMPT = """\
Act as a "table read" reviewer.  Walk through the outline chapter by
chapter and identify:

- escalation gaps (where the tension does not compound)
- foreshadowing payoffs (what was set up, when it lands)
- character motivation issues (where behaviour feels forced)
- chapter-level pacing notes

Output a single markdown file with one section per topic.
""".strip()
