"""Stage: write_chapter — produces the actual chapter prose.

Generates one or more chapter files (depending on ``batch_size.chapter``)
per invocation.  The current batch index is encoded into the chapter
filename so a re-run overwrites the same file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ._helpers import ensure_output_dirs, read_prompt, safe_slug, write_output
from .base import Stage, StageContext, StageContract, StageExecutionResult

CHAPTER_HEADING_RE = re.compile(
    r"^#\s+chapter\s+(\d+)\s*[-–—:]\s*(.+?)$",
    re.IGNORECASE | re.MULTILINE,
)


class WriteChapterStage(Stage):
    id = "write_chapter"
    name = "Write Chapter"
    contract = StageContract(
        inputs=(
            "output/summaries/plot.md",
            "output/summaries/outline-tracking.md",
            "output/chapters",
        ),
        outputs=tuple(
            f"output/chapters/{n:03d}-<slug>.md" for n in (1,)
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
        # Determine the next chapter index from disk.
        next_chapter = ctx.chapter_index or self._next_chapter_index(ctx.project_root)
        assembled = assembler.assemble(
            self.id, chapter_index=next_chapter
        )
        prompt = read_prompt(ctx.project_root, "write-chapter.md")
        full_prompt = assembled.render() + "\n\n" + (
            prompt or DEFAULT_WRITE_PROMPT
        )
        result = self.adapter.invoke(
            full_prompt,
            stage=self.id,
            model=cfg.execution.write_model,
            batch=ctx.batch or f"{next_chapter:03d}",
        )
        files = self._persist_chapters(
            ctx.project_root, next_chapter, result.raw_output
        )
        return StageExecutionResult.from_adapter(
            self.id,
            result,
            files=files,
            batch=ctx.batch or f"{next_chapter:03d}",
            route="APPROVED",
            extras={"chapter_index": next_chapter},
        )

    @staticmethod
    def _next_chapter_index(project_root: Path) -> int:
        chap_dir = project_root / "output" / "chapters"
        if not chap_dir.exists():
            return 1
        numbers: list[int] = []
        for p in chap_dir.glob("*.md"):
            m = re.match(r"^(\d{3,})", p.name)
            if m:
                numbers.append(int(m.group(1)))
        if not numbers:
            return 1
        return max(numbers) + 1

    @staticmethod
    def _persist_chapters(
        project_root: Path,
        first_index: int,
        raw: str,
    ) -> list[Path]:
        """Write one or more chapter files.  Returns the file list."""

        chap_dir = project_root / "output" / "chapters"
        chap_dir.mkdir(parents=True, exist_ok=True)

        # Split the raw output on chapter headings.
        matches = list(CHAPTER_HEADING_RE.finditer(raw))
        if not matches:
            # Treat the whole output as a single chapter.
            slug = safe_slug(raw.splitlines()[0] if raw else "chapter")
            content = f"# Chapter {first_index}\n\n{raw.strip()}\n"
            target = write_output(project_root, f"output/chapters/{first_index:03d}-{slug}.md", content)
            return [target]

        files: list[Path] = []
        for i, m in enumerate(matches):
            num = int(m.group(1))
            title = m.group(2).strip()
            slug = safe_slug(title)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            body = raw[start:end].strip()
            content = f"# Chapter {num} - {title}\n\n{body}\n"
            target = write_output(project_root, f"output/chapters/{num:03d}-{slug}.md", content)
            files.append(target)
        return files


DEFAULT_WRITE_PROMPT = """\
Write the next chapter(s) of the novel.  Use the outline beat above as
your target.  Output ``# Chapter N - <Title>`` followed by 800-1500
Chinese characters of prose per chapter.  End on a small reversal.
""".strip()
