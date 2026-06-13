"""Stage: design_characters.

Reads the outline and produces a dossier per named character
(``output/meta/<slug>.md``).  Each dossier is a self-contained markdown
file with the character's role, voice, relationships, and arc.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ..claude.adapter import ClaudeAdapter
from ..claude.context import ContextAssembler
from ..config import NovelProjectConfig
from ..utils.fs import list_files
from ._helpers import ensure_output_dirs, read_prompt, safe_slug, write_output
from .base import Stage, StageContext, StageContract, StageExecutionResult


CHAPTER_HEADING = re.compile(
    r"^##\s+chapter\s+(\d+)\s*[-–—:]\s*(.+?)$",
    re.IGNORECASE | re.MULTILINE,
)
CHARACTER_HEADER = re.compile(
    r"^#\s+(?:\*\*(.+?)\*\*|(.+?))\s*$", re.MULTILINE
)


class DesignCharactersStage(Stage):
    id = "design_characters"
    name = "Design Characters"
    contract = StageContract(
        inputs=("output/summaries/plot.md", "output/summaries/outline-tracking.md"),
        outputs=tuple(f"output/meta/{n}.md" for n in ("characters",)),
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
        prompt = read_prompt(ctx.project_root, "design-characters.md")
        full_prompt = assembled.render() + "\n\n" + (
            prompt or DEFAULT_DESIGN_PROMPT
        )
        result = self.adapter.invoke(
            full_prompt,
            stage=self.id,
            model=cfg.execution.write_model,
            batch=ctx.batch or "001",
        )
        files = self._persist_dossiers(ctx.project_root, result.raw_output)
        return StageExecutionResult.from_adapter(
            self.id,
            result,
            files=files,
            batch=ctx.batch or "001",
            route="APPROVED",
        )

    @staticmethod
    def _persist_dossiers(project_root: Path, raw: str) -> list[Path]:
        """Split the model output on ``# Name`` headers and write each as a dossier.

        Falls back to a single ``characters.md`` file when the model
        returns prose without per-character sections.
        """

        meta_dir = project_root / "output" / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)

        # Split on top-level # headers.
        chunks: list[tuple[str, str]] = []
        current_name: str | None = None
        buf: list[str] = []
        for line in raw.splitlines():
            m = CHARACTER_HEADER.match(line)
            if m and not line.startswith("##"):
                if current_name is not None:
                    chunks.append((current_name, "\n".join(buf).strip()))
                current_name = (m.group(1) or m.group(2) or "character").strip()
                buf = []
            else:
                buf.append(line)
        if current_name is not None:
            chunks.append((current_name, "\n".join(buf).strip()))

        files: list[Path] = []
        if not chunks:
            # Fallback: write everything as characters.md
            files.append(
                write_output(
                    project_root,
                    "output/meta/characters.md",
                    raw.strip() + "\n",
                )
            )
            return files

        for name, body in chunks:
            slug = safe_slug(name)
            content = f"# {name}\n\n{body.strip()}\n"
            target = write_output(
                project_root, f"output/meta/{slug}.md", content
            )
            files.append(target)
        return files


DEFAULT_DESIGN_PROMPT = """\
You are a character designer for a long-form webnovel.  Read the outline
above and produce a dossier for every named character.  Use the format:

# <Character Name>

**Role**: ...
**Voice**: ...
**Relationships**: ...
**Arc**: ...

One dossier per character, separated by a single blank line.  Output
only the dossiers.
""".strip()
