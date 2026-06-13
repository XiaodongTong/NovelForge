"""Stage: review_chapter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ..review.gate import ReviewGate
from ._helpers import ensure_output_dirs, read_prompt, write_json_output
from .base import Stage, StageContext, StageContract, StageExecutionResult

CHAPTER_FILE_RE = re.compile(r"^(\d{3,})-[a-z0-9][a-z0-9\-_]*\.md$", re.IGNORECASE)


class ReviewChapterStage(Stage):
    id = "review_chapter"
    name = "Review Chapter"
    contract = StageContract(
        inputs=("output/chapters",),
        outputs=("output/review/chapter-review.json",),
    )

    def __init__(self, adapter: ClaudeAdapter, gate: ReviewGate) -> None:
        self.adapter = adapter
        self.gate = gate

    def execute(self, ctx: StageContext) -> StageExecutionResult:
        cfg: NovelProjectConfig = ctx.config
        ensure_output_dirs(ctx.project_root)
        assembler = ContextAssembler(
            project_root=ctx.project_root,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        # Pick the most recent chapter to review.
        chap_dir = ctx.project_root / "output" / "chapters"
        targets = sorted(chap_dir.glob("*.md"))
        target = targets[-1] if targets else None

        assembled = assembler.assemble(
            self.id,
            chapter_index=self._chapter_index_of(target),
            review_target_path=target,
        )
        prompt = read_prompt(ctx.project_root, "review-chapter.md")
        full_prompt = assembled.render() + "\n\n" + (
            prompt or "Review the chapter above and return JSON."
        )
        result = self.adapter.invoke(
            full_prompt,
            stage=self.id,
            model=cfg.execution.review_model,
            batch=ctx.batch or "001",
        )
        decision = self.gate.run(
            result.raw_output,
            cfg=cfg,
            project_root=ctx.project_root,
            auto_check_targets=[target] if target else None,
        )
        out = write_json_output(
            ctx.project_root,
            "output/review/chapter-review.json",
            {
                "stage": self.id,
                "batch": ctx.batch or "001",
                "decision": decision.to_dict(),
            },
        )
        return StageExecutionResult(
            stage_id=self.id,
            raw_output=result.raw_output,
            files=[out],
            batch=ctx.batch or "001",
            route=decision.route,
            findings=decision.findings,
            token_usage_in=result.token_usage.input_tokens,
            token_usage_out=result.token_usage.output_tokens,
            duration=result.duration,
        )

    @staticmethod
    def _chapter_index_of(path: Optional[Path]) -> Optional[int]:
        if path is None:
            return None
        m = CHAPTER_FILE_RE.match(path.name)
        if m:
            return int(m.group(1))
        return None
