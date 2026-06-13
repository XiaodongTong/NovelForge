"""Tests for the runtime ArtifactRegistry (Phase 1.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novelforge.artifact_registry import ArtifactRegistry


def _p(*parts: str) -> Path:
    return Path(*parts)


def test_register_single_path() -> None:
    reg = ArtifactRegistry()
    reg.register("write_chapter", "chapter", _p("output", "chapters", "001.md"))
    assert reg.get_one("write_chapter", "chapter") == _p(
        "output", "chapters", "001.md"
    )


def test_register_list_path() -> None:
    reg = ArtifactRegistry()
    reg.register(
        "write_chapter",
        "chapter",
        [_p("001.md"), _p("002.md"), _p("003.md")],
    )
    assert reg.get_list("write_chapter", "chapter") == [
        _p("001.md"), _p("002.md"), _p("003.md")
    ]


def test_get_one_rejects_list_value() -> None:
    reg = ArtifactRegistry()
    reg.register("s", "a", [_p("x")])
    with pytest.raises(TypeError, match=r"\[\*\]"):
        reg.get_one("s", "a")


def test_get_list_rejects_single_value() -> None:
    reg = ArtifactRegistry()
    reg.register("s", "a", _p("x"))
    with pytest.raises(TypeError, match=r"\[\*\]"):
        reg.get_list("s", "a")


def test_unknown_stage_raises() -> None:
    reg = ArtifactRegistry()
    with pytest.raises(KeyError, match="unknown upstream stage_id"):
        reg.get("missing", "x")


def test_unknown_alias_raises() -> None:
    reg = ArtifactRegistry()
    reg.register("s", "a", _p("x"))
    with pytest.raises(KeyError, match="unknown upstream alias"):
        reg.get("s", "missing")


def test_alias_can_be_overwritten_on_rerun() -> None:
    """A stage that is retried overwrites its previous produce."""

    reg = ArtifactRegistry()
    reg.register("s", "a", _p("first.md"))
    reg.register("s", "a", _p("second.md"))
    assert reg.get_one("s", "a") == _p("second.md")


def test_to_dict_single_and_list() -> None:
    reg = ArtifactRegistry()
    reg.register("single", "a", _p("a.md"))
    reg.register("batch", "a", [_p("a.md"), _p("b.md")])
    rendered = reg.to_dict()
    assert rendered == {
        "single": {"a": "a.md"},
        "batch": {"a": ["a.md", "b.md"]},
    }


def test_round_trip_to_dict_from_dict() -> None:
    reg = ArtifactRegistry()
    reg.register("single", "a", _p("a.md"))
    reg.register("batch", "a", [_p("a.md"), _p("b.md")])
    serialised = reg.to_dict()
    restored = ArtifactRegistry.from_dict(serialised)
    assert restored.get_one("single", "a") == _p("a.md")
    assert restored.get_list("batch", "a") == [_p("a.md"), _p("b.md")]


def test_register_rejects_bad_types() -> None:
    reg = ArtifactRegistry()
    with pytest.raises(TypeError):
        reg.register("s", "a", "not a path")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        reg.register("s", "a", [Path("ok"), "bad"])  # type: ignore[list-item]


def test_register_rejects_empty_keys() -> None:
    reg = ArtifactRegistry()
    with pytest.raises(ValueError):
        reg.register("", "a", _p("a"))
    with pytest.raises(ValueError):
        reg.register("s", "", _p("a"))


def test_has_and_aliases() -> None:
    reg = ArtifactRegistry()
    reg.register("s", "a", _p("a"))
    reg.register("s", "b", [_p("b")])
    assert reg.has("s", "a")
    assert not reg.has("s", "c")
    assert reg.aliases("s") == ["a", "b"]
    assert reg.stages() == ["s"]


def test_is_list() -> None:
    reg = ArtifactRegistry()
    reg.register("s", "single", _p("a"))
    reg.register("s", "list", [_p("a")])
    assert reg.is_list("s", "single") is False
    assert reg.is_list("s", "list") is True
    assert reg.is_list("s", "missing") is False
