"""Stage: review_characters."""

from __future__ import annotations

from pathlib import Path

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ..review.gate import ReviewGate
from ._helpers import ensure_output_dirs, read_prompt, write_json_output
from .base import Stage, StageContext, StageContract, StageExecutionResult


class ReviewCharactersStage(Stage):
    id = "review_characters"
    name = "Review Characters"
    contract = StageContract(
        inputs=tuple(f"output/meta/{n}.md" for n in ("characters",)),
        outputs=("output/review/characters-review.json",),
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
        # Use the first character dossier as the review target.
        target = ctx.project_root / "output" / "meta"
        first = next(iter(sorted(target.glob("*.md"))), None)
        assembled = assembler.assemble(
            self.id, review_target_path=first
        )
        prompt = read_prompt(ctx.project_root, "review-characters.md")
        full_prompt = assembled.render() + "\n\n" + (
            prompt or "Review the character dossiers above and return JSON."
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
        )
        out = write_json_output(
            ctx.project_root,
            "output/review/characters-review.json",
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
