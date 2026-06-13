"""Stage: review_outline."""

from __future__ import annotations

from pathlib import Path

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ..errors import SchemaInvalid
from ..review.gate import ReviewGate
from ..review.schema import parse_review_payload
from ..utils.log import get_logger
from ._helpers import ensure_output_dirs, read_prompt, write_json_output
from .base import Stage, StageContext, StageContract, StageExecutionResult

log = get_logger("stages.review_outline")


class ReviewOutlineStage(Stage):
    id = "review_outline"
    name = "Review Outline"
    contract = StageContract(
        inputs=("output/summaries/plot.md", "output/summaries/outline-tracking.md"),
        outputs=("output/review/outline-review.json",),
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
        target = ctx.project_root / "output" / "summaries" / "plot.md"
        assembled = assembler.assemble(
            self.id, review_target_path=target
        )
        prompt = read_prompt(ctx.project_root, "review-outline.md")
        full_prompt = assembled.render() + "\n\n" + (
            prompt or DEFAULT_REVIEW_PROMPT
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
        review_payload = {
            "stage": self.id,
            "batch": ctx.batch or "001",
            "decision": decision.to_dict(),
        }
        out = write_json_output(
            ctx.project_root,
            "output/review/outline-review.json",
            review_payload,
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


DEFAULT_REVIEW_PROMPT = """\
You are a senior webnovel editor.  Review the outline above and return a
JSON object with:

- "passed": boolean
- "route": "APPROVED" | "NEEDS_REWRITE" | "FUNDAMENTAL_ISSUE"
- "findings": list of strings
- "required_changes": list of strings
- "summary": short paragraph

Be strict: any plot hole or pacing issue must be flagged.  Output only
the JSON object.
""".strip()
