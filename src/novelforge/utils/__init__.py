"""Utility helpers (filesystem, logging, text processing, etc.)."""

from __future__ import annotations

import re

# CJK Unified Ideographs range (same as claude/context.py)
_CJK_RE = re.compile(r"[一-鿿]")


def count_words(text: str) -> int:
    """Count words in mixed CJK / Latin text.

    Each CJK character counts as one word (matching the Chinese meaning of
    "字数").  Latin words (whitespace-delimited tokens) each count as one.
    """

    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    remainder = _CJK_RE.sub(" ", text)
    latin_words = [w for w in re.split(r"\s+", remainder) if w]
    return cjk_count + len(latin_words)
