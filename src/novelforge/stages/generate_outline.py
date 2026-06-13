"""Stage: generate_outline.

The first stage of every pipeline.  It produces:

- ``output/summaries/plot.md``        — high-level plot arc
- ``output/summaries/outline-tracking.md`` — per-chapter beat list

The stage runs the configured prompt with the always_loaded context and
appends the result to the existing outline (if any) so subsequent
batches extend the arc rather than overwrite it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ._helpers import ensure_output_dirs, read_prompt, write_output
from .base import Stage, StageContext, StageContract, StageExecutionResult


class GenerateOutlineStage(Stage):
    id = "generate_outline"
    name = "Generate Outline"
    contract = StageContract(
        inputs=("outline/premise.md", "outline/world.md", "CLAUDE.md"),
        outputs=(
            "output/summaries/plot.md",
            "output/summaries/outline-tracking.md",
        ),
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
        assembled = assembler.assemble(self.id)
        prompt = read_prompt(ctx.project_root, "generate-outline.md")
        full_prompt = (
            assembled.render()
            + "\n\n"
            + (prompt or DEFAULT_OUTLINE_PROMPT)
        )
        result = self.adapter.invoke(
            full_prompt,
            stage=self.id,
            model=cfg.execution.write_model,
            batch=ctx.batch or "001",
        )
        files = self._persist_outputs(ctx.project_root, result.raw_output)
        return StageExecutionResult.from_adapter(
            self.id,
            result,
            files=files,
            batch=ctx.batch or "001",
            route="APPROVED",
            extras={"prompt": prompt},
        )

    @staticmethod
    def _persist_outputs(project_root: Path, raw: str) -> list[Path]:
        """Parse the model's output into plot.md and outline-tracking.md.

        The model is expected to return at least one ``## Chapter N``
        heading.  We split the output into:

        - everything before the first ``## Chapter N`` → plot.md
        - the rest → outline-tracking.md
        """

        plot_path = project_root / "output" / "summaries" / "plot.md"
        track_path = project_root / "output" / "summaries" / "outline-tracking.md"

        raw = raw.strip()
        # Find the first chapter heading.
        import re

        m = re.search(r"^##\s+chapter\s+\d+", raw, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            plot = raw[: m.start()].strip()
            tracking = raw[m.start() :].strip()
        else:
            plot = raw
            tracking = "# Outline Tracking\n\n(no chapter breakdown emitted)\n"

        files = [
            write_output(project_root, "output/summaries/plot.md", plot + "\n"),
            write_output(project_root, "output/summaries/outline-tracking.md", tracking + "\n"),
        ]
        return files


DEFAULT_OUTLINE_PROMPT = """\
You are a senior webnovel planner.  Produce:

1. A high-level plot arc (themes, antagonists, the protagonist's inner arc).
2. A chapter-by-chapter outline with one ``## Chapter N - <title>`` heading
   for every chapter in the target, each followed by 1-2 sentences
   describing the key beat.

Do not write prose.  Output markdown only.
""".strip()
