"""Shared helpers for stage implementations."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from ..config import NovelProjectConfig
from ..utils.fs import atomic_write, ensure_dir
from ..utils.log import get_logger

log = get_logger("stages.helpers")


def prompt_path(project_root: Path, prompt_file: str) -> Path:
    """Resolve a ``prompts/<file>`` reference inside the project root.

    Falls back to looking inside the bundled prompts directory if the
    project doesn't override it.
    """

    candidate = project_root / "prompts" / prompt_file
    if candidate.exists():
        return candidate
    bundled = Path(__file__).resolve().parent.parent.parent / "prompts" / prompt_file
    return bundled


def read_prompt(project_root: Path, prompt_file: str) -> str:
    """Return the contents of a prompt file (project override first, bundled fallback)."""

    path = prompt_path(project_root, prompt_file)
    if not path.exists():
        # Empty prompt is acceptable; stages can still build a default.
        log.warning("prompt file missing: %s", path)
        return ""
    return path.read_text(encoding="utf-8")


def write_output(
    project_root: Path, rel_path: str, content: str
) -> Path:
    """Write a stage output file relative to the project root, atomically."""

    target = project_root / rel_path
    ensure_dir(target.parent)
    atomic_write(target, content)
    return target


def write_json_output(
    project_root: Path, rel_path: str, payload: Any
) -> Path:
    """Write a structured JSON file with stable formatting."""

    return write_output(
        project_root,
        rel_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
    )


def safe_slug(text: str, max_len: int = 60) -> str:
    """Convert arbitrary text to a filesystem-safe slug."""

    s = re.sub(r"[^A-Za-z0-9\-_]+", "-", text.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "untitled"
    return s[:max_len]


def ensure_output_dirs(project_root: Path) -> None:
    for sub in (
        "output",
        "output/summaries",
        "output/chapters",
        "output/meta",
        "output/review",
    ):
        ensure_dir(project_root / sub)
