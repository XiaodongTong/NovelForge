"""M6 tests: review gate, JSON schema, auto-checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novelforge.config import load_config
from novelforge.errors import SchemaInvalid
from novelforge.review.gate import (
    CHAPTER_FILE_RE,
    ReviewGate,
    auto_check_chapter_file,
    auto_check_chapter_sequence,
    auto_check_foreshadowing_refs,
)
from novelforge.review.schema import (
    parse_review_payload,
    validate_review_payload,
)


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #


def test_validate_review_payload_accepts_valid() -> None:
    payload = {
        "passed": True,
        "route": "APPROVED",
        "findings": [],
        "required_changes": [],
    }
    assert validate_review_payload(payload) == []


def test_validate_review_payload_rejects_missing_required() -> None:
    payload = {"findings": []}
    errors = validate_review_payload(payload)
    assert errors  # at least one


def test_validate_review_payload_rejects_bad_route() -> None:
    payload = {"passed": True, "route": "MAYBE"}
    errors = validate_review_payload(payload)
    assert any("route" in e for e in errors)


def test_parse_review_payload_handles_fenced_block() -> None:
    raw = "Here is my review:\n\n```json\n" + json.dumps(
        {"passed": True, "route": "APPROVED", "findings": []}
    ) + "\n```\n"
    parsed, err = parse_review_payload(raw)
    assert err is None
    assert parsed == {"passed": True, "route": "APPROVED", "findings": []}


def test_parse_review_payload_handles_embedded_object() -> None:
    raw = "I think:\n" + json.dumps(
        {"passed": False, "route": "NEEDS_REWRITE", "findings": ["x"]}
    )
    parsed, err = parse_review_payload(raw)
    assert err is None
    assert parsed["route"] == "NEEDS_REWRITE"


def test_parse_review_payload_returns_error_on_garbage() -> None:
    parsed, err = parse_review_payload("not json at all")
    assert parsed is None
    assert err is not None


def test_parse_review_payload_empty() -> None:
    parsed, err = parse_review_payload("")
    assert parsed is None
    assert err is not None


# --------------------------------------------------------------------------- #
# Auto-checks
# --------------------------------------------------------------------------- #


@pytest.fixture()
def cfg() -> object:
    # Use the minimal sample project.
    project_root = Path(__file__).resolve().parent.parent / "samples" / "minimal-novel"
    return load_config(project_root / "novel-project.yaml")


def test_auto_check_chapter_file_accepts_good(tmp_path: Path, cfg: object) -> None:
    p = tmp_path / "001-test.md"
    body = "word " * 1000  # 1000 words
    p.write_text(f"# Chapter 1 - Test\n\n{body}\n", encoding="utf-8")
    findings = auto_check_chapter_file(p, cfg)  # type: ignore[arg-type]
    assert findings == []


def test_auto_check_chapter_file_accepts_good_cjk(tmp_path: Path, cfg: object) -> None:
    """CJK chapter with enough characters should pass word count check."""
    p = tmp_path / "001-test.md"
    # Generate enough CJK characters to exceed the 800 minimum.
    # Each repetition is 16 CJK chars; 55 * 16 = 880 > 800.
    body = "少年魏林独坐山巅，目光穿透云雾。" * 55
    p.write_text(f"# Chapter 1 - 测试\n\n{body}\n", encoding="utf-8")
    findings = auto_check_chapter_file(p, cfg)  # type: ignore[arg-type]
    assert findings == []


def test_auto_check_chapter_file_rejects_short_cjk(tmp_path: Path, cfg: object) -> None:
    """CJK chapter that is too short should be flagged."""
    p = tmp_path / "001-test.md"
    body = "太短了。"  # ~3 CJK chars
    p.write_text(f"# Chapter 1 - 测试\n\n{body}\n", encoding="utf-8")
    findings = auto_check_chapter_file(p, cfg)  # type: ignore[arg-type]
    assert any("below minimum" in f for f in findings)


def test_auto_check_chapter_file_rejects_short(tmp_path: Path, cfg: object) -> None:
    p = tmp_path / "001-test.md"
    p.write_text("# Chapter 1 - Test\n\ntoo short\n", encoding="utf-8")
    findings = auto_check_chapter_file(p, cfg)  # type: ignore[arg-type]
    assert any("below minimum" in f for f in findings)


def test_auto_check_chapter_file_rejects_bad_name(tmp_path: Path, cfg: object) -> None:
    p = tmp_path / "chapter-one.md"  # missing NNN prefix
    p.write_text("# Chapter 1\n\n" + ("word " * 2000), encoding="utf-8")
    findings = auto_check_chapter_file(p, cfg)  # type: ignore[arg-type]
    assert any("filename" in f for f in findings)


def test_auto_check_chapter_sequence_detects_gap(tmp_path: Path) -> None:
    files = [
        tmp_path / "001-a.md",
        tmp_path / "002-b.md",
        tmp_path / "004-d.md",  # gap
    ]
    for p in files:
        p.write_text("body", encoding="utf-8")
    findings = auto_check_chapter_sequence(files)
    assert any("gaps" in f for f in findings)


def test_auto_check_chapter_sequence_ok(tmp_path: Path) -> None:
    files = [tmp_path / f"{i:03d}-c.md" for i in range(1, 6)]
    for p in files:
        p.write_text("body", encoding="utf-8")
    assert auto_check_chapter_sequence(files) == []


def test_auto_check_foreshadowing_refs_resolves() -> None:
    text = "The hero remembers #f-abc123 and #f-deadbe."
    findings = auto_check_foreshadowing_refs(text, declared_hashes=["abc123", "deadbe"])
    assert findings == []


def test_auto_check_foreshadowing_refs_unresolved() -> None:
    text = "The hero remembers #f-ff00ff."
    findings = auto_check_foreshadowing_refs(text, declared_hashes=["abc123"])
    assert any("unresolved" in f for f in findings)


def test_chapter_file_re_matches_valid_names() -> None:
    assert CHAPTER_FILE_RE.match("001-foo.md")
    assert CHAPTER_FILE_RE.match("1234-foo_bar.md")
    assert not CHAPTER_FILE_RE.match("chapter-one.md")
    assert not CHAPTER_FILE_RE.match("001.md")


# --------------------------------------------------------------------------- #
# Review gate
# --------------------------------------------------------------------------- #


def test_review_gate_approved(tmp_path: Path, cfg: object) -> None:
    gate = ReviewGate()
    raw = json.dumps(
        {
            "passed": True,
            "route": "APPROVED",
            "findings": [],
            "required_changes": [],
        }
    )
    decision = gate.run(raw, cfg=cfg, project_root=tmp_path)  # type: ignore[arg-type]
    assert decision.route == "APPROVED"
    assert decision.passed is True


def test_review_gate_raises_on_schema_violation(tmp_path: Path, cfg: object) -> None:
    gate = ReviewGate()
    with pytest.raises(SchemaInvalid):
        gate.run("not json", cfg=cfg, project_root=tmp_path)  # type: ignore[arg-type]


def test_review_gate_downgrades_on_auto_check_failure(tmp_path: Path, cfg: object) -> None:
    """A passing model review is downgraded to NEEDS_REWRITE if the
    auto-checks flag real problems with the file.
    """

    bad = tmp_path / "output" / "chapters" / "001-foo.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("# Chapter 1 - Foo\n\ntoo short\n", encoding="utf-8")
    gate = ReviewGate()
    raw = json.dumps(
        {
            "passed": True,
            "route": "APPROVED",
            "findings": [],
            "required_changes": [],
        }
    )
    decision = gate.run(
        raw,
        cfg=cfg,
        project_root=tmp_path,
        auto_check_targets=[bad],
    )  # type: ignore[arg-type]
    assert decision.route == "NEEDS_REWRITE"
    assert decision.passed is False
    assert any("below minimum" in f for f in decision.findings)


def test_review_gate_preserves_fundamental_issue(tmp_path: Path, cfg: object) -> None:
    gate = ReviewGate()
    raw = json.dumps(
        {
            "passed": False,
            "route": "FUNDAMENTAL_ISSUE",
            "findings": ["x"],
        }
    )
    decision = gate.run(raw, cfg=cfg, project_root=tmp_path)  # type: ignore[arg-type]
    assert decision.route == "FUNDAMENTAL_ISSUE"


def test_review_gate_passes_through_needs_rewrite(tmp_path: Path, cfg: object) -> None:
    gate = ReviewGate()
    raw = json.dumps(
        {
            "passed": False,
            "route": "NEEDS_REWRITE",
            "findings": ["tighten the hook"],
            "required_changes": ["add foreshadowing"],
        }
    )
    decision = gate.run(raw, cfg=cfg, project_root=tmp_path)  # type: ignore[arg-type]
    assert decision.route == "NEEDS_REWRITE"
    assert decision.passed is False
    assert "tighten the hook" in decision.findings
    assert "add foreshadowing" in decision.required_changes


# --------------------------------------------------------------------------- #
# Foreshadowing auto-check integration
# --------------------------------------------------------------------------- #


def test_foreshadowing_check_integrated_in_review_gate(
    tmp_path: Path, cfg: object
) -> None:
    """Plan §4.6: foreshadowing hash refs are checked during review."""
    # Create a chapter with an unresolved foreshadowing reference.
    chap = tmp_path / "output" / "chapters" / "001-test.md"
    chap.parent.mkdir(parents=True, exist_ok=True)
    body = "word " * 1000  # enough words
    chap.write_text(
        f"# Chapter 1 - Test\n\n{body}\nHe remembers #f-deadbe.\n",
        encoding="utf-8",
    )

    # Create a foreshadowing file with a DIFFERENT hash.
    fs_path = tmp_path / "output" / "summaries" / "foreshadowing.md"
    fs_path.parent.mkdir(parents=True, exist_ok=True)
    fs_path.write_text("- F1: setup #f-abc123\n", encoding="utf-8")

    gate = ReviewGate()
    raw = json.dumps(
        {
            "passed": True,
            "route": "APPROVED",
            "findings": [],
            "required_changes": [],
        }
    )
    decision = gate.run(
        raw,
        cfg=cfg,
        project_root=tmp_path,
        auto_check_targets=[chap],
    )  # type: ignore[arg-type]
    # The unresolved reference should downgrade to NEEDS_REWRITE.
    assert decision.route == "NEEDS_REWRITE"
    assert any("unresolved" in f for f in decision.findings)


def test_foreshadowing_check_passes_when_refs_match(
    tmp_path: Path, cfg: object
) -> None:
    """Chapter with valid foreshadowing refs should pass the auto-check."""
    chap = tmp_path / "output" / "chapters" / "001-test.md"
    chap.parent.mkdir(parents=True, exist_ok=True)
    body = "word " * 1000
    chap.write_text(
        f"# Chapter 1 - Test\n\n{body}\nHe resolves #f-abc123.\n",
        encoding="utf-8",
    )

    fs_path = tmp_path / "output" / "summaries" / "foreshadowing.md"
    fs_path.parent.mkdir(parents=True, exist_ok=True)
    fs_path.write_text("- F1: setup #f-abc123\n", encoding="utf-8")

    gate = ReviewGate()
    raw = json.dumps(
        {
            "passed": True,
            "route": "APPROVED",
            "findings": [],
            "required_changes": [],
        }
    )
    decision = gate.run(
        raw,
        cfg=cfg,
        project_root=tmp_path,
        auto_check_targets=[chap],
    )  # type: ignore[arg-type]
    assert decision.route == "APPROVED"


def test_foreshadowing_check_no_file_is_noop(
    tmp_path: Path, cfg: object
) -> None:
    """When no foreshadowing file exists, the check is silently skipped."""
    chap = tmp_path / "output" / "chapters" / "001-test.md"
    chap.parent.mkdir(parents=True, exist_ok=True)
    body = "word " * 1000
    chap.write_text(
        f"# Chapter 1 - Test\n\n{body}\n", encoding="utf-8"
    )

    gate = ReviewGate()
    raw = json.dumps(
        {
            "passed": True,
            "route": "APPROVED",
            "findings": [],
            "required_changes": [],
        }
    )
    decision = gate.run(
        raw,
        cfg=cfg,
        project_root=tmp_path,
        auto_check_targets=[chap],
    )  # type: ignore[arg-type]
    assert decision.route == "APPROVED"
