"""Tests for the verify module (Phase 1.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novelforge.errors import ConfigError, VerifyFailed
from novelforge.verify import (
    CheckSpec,
    DoneWhenSpec,
    run_check,
    run_done_when,
    substitute_placeholders,
    verify_or_raise,
)


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    return tmp_path


def _write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# CheckSpec / DoneWhenSpec validation
# --------------------------------------------------------------------------- #


def test_check_spec_rejects_unknown_kind() -> None:
    with pytest.raises(ConfigError, match="kind"):
        CheckSpec(kind="bogus", target="output/x.md")


def test_check_spec_rejects_empty_target() -> None:
    with pytest.raises(ConfigError, match="target"):
        CheckSpec(kind="exists", target="")


def test_check_spec_requires_pattern_for_regex_match() -> None:
    with pytest.raises(ConfigError, match="pattern"):
        CheckSpec(kind="regex_match", target="output/x.md")


def test_check_spec_requires_field_for_json_field() -> None:
    with pytest.raises(ConfigError, match="field"):
        CheckSpec(kind="json_field", target="output/x.json")


def test_check_spec_requires_int_value_for_min_chars() -> None:
    with pytest.raises(ConfigError, match="value"):
        CheckSpec(kind="min_chars", target="output/x.md", value="big")  # type: ignore[arg-type]


def test_check_spec_requires_callable_ref() -> None:
    with pytest.raises(ConfigError, match="callable"):
        CheckSpec(kind="callable", target="output/x.md")


def test_done_when_spec_defaults() -> None:
    dw = DoneWhenSpec()
    assert dw.max_attempts == 3
    assert dw.mode == "all"
    assert dw.checks == ()
    assert dw.completion_signal  # default marker


def test_done_when_spec_rejects_zero_max_attempts() -> None:
    with pytest.raises(ConfigError, match="max_attempts"):
        DoneWhenSpec(max_attempts=0)


def test_done_when_spec_rejects_bad_mode() -> None:
    with pytest.raises(ConfigError, match="mode"):
        DoneWhenSpec(mode="maybe")


# --------------------------------------------------------------------------- #
# Per-kind evaluators
# --------------------------------------------------------------------------- #


def test_check_exists_passes_when_file_present(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "hello")
    outcome = run_check(
        CheckSpec(kind="exists", target="output/x.md"),
        project_root,
        placeholders={},
    )
    assert outcome.passed


def test_check_exists_fails_when_file_missing(project_root: Path) -> None:
    outcome = run_check(
        CheckSpec(kind="exists", target="output/missing.md"),
        project_root,
        placeholders={},
    )
    assert not outcome.passed
    assert "missing" in outcome.reason


def test_check_min_chars_pass_and_fail(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "abcdefghij")  # 10 chars
    ok = run_check(
        CheckSpec(kind="min_chars", target="output/x.md", value=5),
        project_root,
        placeholders={},
    )
    bad = run_check(
        CheckSpec(kind="min_chars", target="output/x.md", value=100),
        project_root,
        placeholders={},
    )
    assert ok.passed
    assert ok.actual == 10
    assert not bad.passed
    assert bad.actual == 10


def test_check_min_bytes_pass_and_fail(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "x" * 50)
    ok = run_check(
        CheckSpec(kind="min_bytes", target="output/x.md", value=10),
        project_root,
        placeholders={},
    )
    bad = run_check(
        CheckSpec(kind="min_bytes", target="output/x.md", value=1000),
        project_root,
        placeholders={},
    )
    assert ok.passed
    assert not bad.passed


def test_check_regex_match_search_semantics(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "# Chapter 1 - The Beginning\n\nSome text.\n")
    ok = run_check(
        CheckSpec(
            kind="regex_match",
            target="output/x.md",
            pattern=r"^# Chapter \d+",
        ),
        project_root,
        placeholders={},
    )
    bad = run_check(
        CheckSpec(
            kind="regex_match",
            target="output/x.md",
            pattern=r"^## Nonexistent",
        ),
        project_root,
        placeholders={},
    )
    assert ok.passed
    assert not bad.passed


def test_check_regex_match_invalid_pattern(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "anything")
    with pytest.raises(ConfigError, match="regex"):
        run_check(
            CheckSpec(kind="regex_match", target="output/x.md", pattern="(unclosed"),
            project_root,
            placeholders={},
        )


def test_check_json_field_equals(project_root: Path) -> None:
    target = project_root / "output" / "x.json"
    _write(target, json.dumps({"passed": True, "findings": ["a", "b"]}))
    ok = run_check(
        CheckSpec(
            kind="json_field",
            target="output/x.json",
            field="passed",
            value=True,
        ),
        project_root,
        placeholders={},
    )
    bad = run_check(
        CheckSpec(
            kind="json_field",
            target="output/x.json",
            field="passed",
            value=False,
        ),
        project_root,
        placeholders={},
    )
    assert ok.passed
    assert not bad.passed


def test_check_json_field_subset_of_list(project_root: Path) -> None:
    target = project_root / "output" / "x.json"
    _write(target, json.dumps({"findings": ["a", "b", "c"]}))
    ok = run_check(
        CheckSpec(
            kind="json_field",
            target="output/x.json",
            field="findings",
            value=["a", "c"],
        ),
        project_root,
        placeholders={},
    )
    bad = run_check(
        CheckSpec(
            kind="json_field",
            target="output/x.json",
            field="findings",
            value=["missing"],
        ),
        project_root,
        placeholders={},
    )
    assert ok.passed
    assert not bad.passed


def test_check_json_field_missing_field(project_root: Path) -> None:
    target = project_root / "output" / "x.json"
    _write(target, json.dumps({"a": 1}))
    outcome = run_check(
        CheckSpec(
            kind="json_field",
            target="output/x.json",
            field="b",
        ),
        project_root,
        placeholders={},
    )
    assert not outcome.passed
    assert "missing" in outcome.reason


def test_check_json_field_not_json(project_root: Path) -> None:
    target = project_root / "output" / "x.json"
    _write(target, "not a json document")
    outcome = run_check(
        CheckSpec(
            kind="json_field",
            target="output/x.json",
            field="a",
        ),
        project_root,
        placeholders={},
    )
    assert not outcome.passed
    assert "not JSON" in outcome.reason


# --------------------------------------------------------------------------- #
# callable
# --------------------------------------------------------------------------- #


def _has_word(content: str, *, value: str) -> bool:
    return value in content


def test_check_callable_pass_and_fail(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "the quick brown fox")
    ok = run_check(
        CheckSpec(
            kind="callable",
            target="output/x.md",
            callable=f"{__name__}:_has_word",
            value="fox",
        ),
        project_root,
        placeholders={},
    )
    bad = run_check(
        CheckSpec(
            kind="callable",
            target="output/x.md",
            callable=f"{__name__}:_has_word",
            value="rabbit",
        ),
        project_root,
        placeholders={},
    )
    assert ok.passed
    assert not bad.passed


def test_check_callable_rejects_bad_spec(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "anything")
    with pytest.raises(ConfigError, match="callable spec"):
        run_check(
            CheckSpec(
                kind="callable",
                target="output/x.md",
                callable="no_colon_here",
            ),
            project_root,
            placeholders={},
        )


def test_check_callable_rejects_missing_module(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "anything")
    with pytest.raises(ConfigError, match="callable module"):
        run_check(
            CheckSpec(
                kind="callable",
                target="output/x.md",
                callable="nonexistent_module_xyz:func",
            ),
            project_root,
            placeholders={},
        )


# --------------------------------------------------------------------------- #
# Placeholders
# --------------------------------------------------------------------------- #


def test_substitute_placeholders_ok() -> None:
    assert (
        substitute_placeholders(
            "output/chapters/{{num}}.md", {"num": "005"}
        )
        == "output/chapters/005.md"
    )


def test_substitute_placeholders_unknown_raises() -> None:
    with pytest.raises(ConfigError, match="placeholder"):
        substitute_placeholders("{{missing}}", {})


# --------------------------------------------------------------------------- #
# run_done_when aggregate behaviour
# --------------------------------------------------------------------------- #


def test_run_done_when_all_mode_default_passes_when_all_pass(
    project_root: Path,
) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "x" * 50)
    spec = DoneWhenSpec(
        checks=(
            CheckSpec(kind="exists", target="output/x.md"),
            CheckSpec(kind="min_chars", target="output/x.md", value=10),
        ),
    )
    result = run_done_when(spec, [target], project_root, placeholders={})
    assert result.passed


def test_run_done_when_all_mode_fails_when_one_fails(
    project_root: Path,
) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "short")
    spec = DoneWhenSpec(
        checks=(
            CheckSpec(kind="exists", target="output/x.md"),
            CheckSpec(kind="min_chars", target="output/x.md", value=1000),
        ),
    )
    result = run_done_when(spec, [target], project_root, placeholders={})
    assert not result.passed
    failure = result.first_failure
    assert failure is not None
    assert failure.spec.kind == "min_chars"


def test_run_done_when_any_mode_passes_with_one_pass(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "short")
    spec = DoneWhenSpec(
        mode="any",
        checks=(
            CheckSpec(kind="exists", target="output/x.md"),
            CheckSpec(kind="min_chars", target="output/x.md", value=1000),
        ),
    )
    result = run_done_when(spec, [target], project_root, placeholders={})
    assert result.passed


def test_run_done_when_empty_checks_passes() -> None:
    """Checks disabled (empty list) → result is trivially True."""

    spec = DoneWhenSpec(checks=())
    result = run_done_when(spec, [], Path("/tmp"), placeholders={})
    assert result.passed
    assert result.outcomes == []


def test_run_done_with_placeholder_per_file(project_root: Path) -> None:
    """Each file in ``files`` gets its own evaluation pass.

    ``per_file_placeholders`` lets the orchestrator supply distinct
    ``{{num}}`` values for batch / split artefacts.
    """

    f1 = project_root / "output" / "chapters" / "001.md"
    f2 = project_root / "output" / "chapters" / "002.md"
    _write(f1, "x" * 50)
    _write(f2, "short")
    spec = DoneWhenSpec(
        checks=(
            CheckSpec(
                kind="min_chars", target="output/chapters/{{num}}.md", value=10
            ),
        ),
    )
    result = run_done_when(
        spec,
        [f1, f2],
        project_root,
        placeholders={"num": "001"},
        per_file_placeholders=[{"num": "001"}, {"num": "002"}],
    )
    # f2 has only 5 chars → the all-mode check fails.
    assert not result.passed
    targets = [o.target for o in result.outcomes]
    assert "output/chapters/001.md" in targets
    assert "output/chapters/002.md" in targets


def test_run_done_with_single_placeholder_set_applies_to_all_files(
    project_root: Path,
) -> None:
    """When no per-file placeholders are passed, all files share the same map."""

    f1 = project_root / "output" / "chapters" / "001.md"
    _write(f1, "x" * 50)
    spec = DoneWhenSpec(
        checks=(
            CheckSpec(
                kind="min_chars", target="output/chapters/{{num}}.md", value=10
            ),
        ),
    )
    result = run_done_when(
        spec,
        [f1],
        project_root,
        placeholders={"num": "001"},
    )
    assert result.passed


# --------------------------------------------------------------------------- #
# verify_or_raise
# --------------------------------------------------------------------------- #


def test_verify_or_raise_raises_verify_failed(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "x")
    spec = DoneWhenSpec(
        checks=(
            CheckSpec(kind="min_chars", target="output/x.md", value=1000),
        )
    )
    with pytest.raises(VerifyFailed) as exc_info:
        verify_or_raise(
            spec,
            [target],
            project_root,
            placeholders={},
            stage_id="write_chapter",
            attempt=1,
        )
    err = exc_info.value
    assert err.stage_id == "write_chapter"
    assert err.target == "output/x.md"
    assert err.kind == "min_chars"
    assert err.expected == 1000
    assert err.actual == 1


def test_verify_or_raise_returns_result_on_pass(project_root: Path) -> None:
    target = project_root / "output" / "x.md"
    _write(target, "x" * 50)
    spec = DoneWhenSpec(
        checks=(CheckSpec(kind="exists", target="output/x.md"),)
    )
    result = verify_or_raise(
        spec,
        [target],
        project_root,
        placeholders={},
        stage_id="write_chapter",
        attempt=1,
    )
    assert result.passed
