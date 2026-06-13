"""Review gate — turns a model response into a route decision.

The gate:

1. Parses the model's output as JSON (best-effort).
2. Validates the payload against the schema.
3. Runs auto-checks that do not consume an LLM call.
4. Returns a :class:`RouteDecision` that the orchestrator can act on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..config import NovelProjectConfig
from ..errors import FundamentIssue, SchemaInvalid
from ..utils import count_words
from ..utils.log import get_logger
from .schema import parse_review_payload, validate_review_payload

log = get_logger("review.gate")


@dataclass
class RouteDecision:
    """The output of a review pass."""

    route: str  # APPROVED | NEEDS_REWRITE | FUNDAMENTAL_ISSUE
    passed: bool
    findings: list[str] = field(default_factory=list)
    required_changes: list[str] = field(default_factory=list)
    summary: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "passed": self.passed,
            "findings": list(self.findings),
            "required_changes": list(self.required_changes),
            "summary": self.summary,
            "scores": dict(self.scores),
        }


# --------------------------------------------------------------------------- #
# Auto-checks
# --------------------------------------------------------------------------- #


CHAPTER_FILE_RE = re.compile(r"^(\d{3,})-[a-z0-9][a-z0-9\-_]*\.md$", re.IGNORECASE)
MIN_CHAPTER_WORDS_DEFAULT = 800


def _chapter_word_range(cfg: NovelProjectConfig) -> tuple[int, int]:
    return cfg.novel.words_per_chapter


def auto_check_chapter_file(path: Path, cfg: NovelProjectConfig) -> list[str]:
    """Return a list of findings (empty if OK).

    Checks:

    - File name matches ``<NNN>-<slug>.md`` (3+ digit number).
    - Word count is in the configured range.
    """

    findings: list[str] = []
    name = path.name
    if not CHAPTER_FILE_RE.match(name):
        findings.append(f"filename does not match NNN-slug.md: {name}")
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.strip():
        findings.append("chapter body is empty")
        return findings
    lo, hi = _chapter_word_range(cfg)
    word_count = count_words(text)
    if word_count < lo:
        findings.append(
            f"word count {word_count} below minimum {lo} ({name})"
        )
    if word_count > hi * 2:
        # Out-of-range above; allow 2x as soft warning
        findings.append(
            f"word count {word_count} significantly above max {hi} ({name})"
        )
    return findings


def auto_check_chapter_sequence(
    chapter_files: Sequence[Path],
) -> list[str]:
    """Check that chapter numbers form a contiguous sequence from 1."""

    findings: list[str] = []
    numbers: list[int] = []
    for p in chapter_files:
        m = CHAPTER_FILE_RE.match(p.name)
        if not m:
            continue
        numbers.append(int(m.group(1)))
    if not numbers:
        return findings
    numbers = sorted(set(numbers))
    expected = list(range(1, numbers[-1] + 1))
    if numbers != expected:
        missing = [n for n in expected if n not in numbers]
        findings.append(
            f"chapter sequence has gaps; missing: {missing[:10]}{'...' if len(missing) > 10 else ''}"
        )
    return findings


def auto_check_foreshadowing_refs(
    text: str, declared_hashes: Iterable[str]
) -> list[str]:
    """Check that ``#f-<hash>`` references in ``text`` map to declared foreshadowings."""

    findings: list[str] = []
    refs = set(re.findall(r"#f-([a-f0-9]{6,})", text))
    declared = set(declared_hashes)
    unresolved = refs - declared
    if unresolved:
        findings.append(
            f"unresolved foreshadowing references: {sorted(unresolved)[:5]}"
        )
    return findings


# --------------------------------------------------------------------------- #
# Review gate
# --------------------------------------------------------------------------- #


class ReviewGate:
    """Combines the model's review JSON with auto-checks."""

    def run(
        self,
        raw_review_output: str,
        *,
        auto_check_targets: Optional[Sequence[Path]] = None,
        cfg: Optional[NovelProjectConfig] = None,
        project_root: Optional[Path] = None,
    ) -> RouteDecision:
        parsed, err = parse_review_payload(raw_review_output)
        if err is not None:
            # Spec: schema failure == FUNDAMENTAL_ISSUE
            log.warning("review schema invalid: %s", err)
            raise SchemaInvalid(f"review output schema invalid: {err}")

        assert parsed is not None  # parse_review_payload contract
        # Auto-checks layer.
        auto_findings: list[str] = []
        if cfg is not None and auto_check_targets:
            for target in auto_check_targets:
                auto_findings.extend(
                    auto_check_file(target, cfg, project_root=project_root)
                )
        # Combine findings.
        model_findings = list(parsed.get("findings", []) or [])
        combined_findings = model_findings + auto_findings
        decision = RouteDecision(
            route=parsed.get("route", "NEEDS_REWRITE"),
            passed=bool(parsed.get("passed", False)),
            findings=combined_findings,
            required_changes=list(parsed.get("required_changes", []) or []),
            summary=str(parsed.get("summary", "") or ""),
            scores=dict(parsed.get("scores", {}) or {}),
        )
        # Spec: if auto-checks produced blocking findings, downgrade.
        if auto_findings and decision.route == "APPROVED":
            decision.route = "NEEDS_REWRITE"
            decision.passed = False
            decision.findings = combined_findings
        return decision


def _load_declared_foreshadowing_hashes(project_root: Path) -> set[str]:
    """Extract ``#f-<hex>`` hashes from the foreshadowing tracking file.

    Returns an empty set when the file does not exist or cannot be read.
    """

    path = project_root / "output" / "summaries" / "foreshadowing.md"
    if not path.exists():
        return set()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    return set(re.findall(r"#f-([a-f0-9]{6,})", text))


def auto_check_file(
    path: Path,
    cfg: NovelProjectConfig,
    *,
    project_root: Optional[Path] = None,
) -> list[str]:
    """Dispatch auto-checks based on file name/contents."""

    name = path.name
    if CHAPTER_FILE_RE.match(name):
        findings = auto_check_chapter_file(path, cfg)
        # Plan §4.6: check foreshadowing hash references.
        if project_root is not None:
            declared = _load_declared_foreshadowing_hashes(project_root)
            if declared:
                text = path.read_text(encoding="utf-8", errors="ignore")
                findings.extend(
                    auto_check_foreshadowing_refs(text, declared)
                )
        return findings
    if name.startswith("plot") or name == "outline-tracking.md":
        return []  # not enforced at this stage
    if name.startswith("character") or path.parent.name == "meta":
        return []  # character dossiers are not auto-checked
    return []
