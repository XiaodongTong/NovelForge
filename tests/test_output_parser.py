"""Unit tests for output_parser (Phase 5 coverage backfill).

Covers the parse / infer_form / render_path_template surface plus
internal helpers.  Integration coverage exists in test_generic_stage
(produces end-to-end) but the unit-level helpers need direct tests
to lift module coverage above the 80% gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novelforge.claude.output_parser import (
    ParseResult,
    ProduceParseResult,
    _parse_json_object,
    _slugify,
    _strip_completion_signal,
    infer_form,
    parse,
    render_path_template,
)
from novelforge.config import ProduceSpec
from novelforge.errors import OutputParseError, SchemaInvalid, VerifyFailed
from novelforge.verify import DEFAULT_COMPLETION_SIGNAL


# --------------------------------------------------------------------------- #
# infer_form
# --------------------------------------------------------------------------- #


class TestInferForm:
    def test_md_path_returns_text(self) -> None:
        assert infer_form("output/x.md") == "text"

    def test_txt_path_returns_text(self) -> None:
        assert infer_form("output/x.txt") == "text"

    def test_json_path_returns_json(self) -> None:
        assert infer_form("output/x.json") == "json"

    def test_json_with_spaces_returns_json(self) -> None:
        assert infer_form("output/x.json   ") == "json"

    def test_empty_path_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            infer_form("")

    def test_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            infer_form(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# _slugify
# --------------------------------------------------------------------------- #


class TestSlugify:
    def test_basic_slug(self) -> None:
        assert _slugify("Hello World!") == "hello-world"

    def test_chinese_kept_as_is(self) -> None:
        # Chinese chars satisfy [^A-Za-z0-9\-_] so they become dashes
        # unless explicitly handled elsewhere.
        result = _slugify("你好 world")
        assert "world" in result

    def test_collapses_dashes(self) -> None:
        assert _slugify("a---b") == "a-b"

    def test_strips_edges(self) -> None:
        assert _slugify("---abc---") == "abc"

    def test_empty_returns_untitled(self) -> None:
        assert _slugify("   ") == "untitled"

    def test_max_len(self) -> None:
        result = _slugify("a" * 100, max_len=10)
        assert len(result) <= 10


# --------------------------------------------------------------------------- #
# render_path_template
# --------------------------------------------------------------------------- #


class TestRenderPathTemplate:
    def test_no_placeholders(self) -> None:
        assert render_path_template("output/x.md") == "output/x.md"

    def test_simple_var(self) -> None:
        out = render_path_template("output/{{name}}.md", {"name": "alice"})
        assert out == "output/alice.md"

    def test_zero_pad_format(self) -> None:
        out = render_path_template("output/{{num:03d}}.md", {"num": 5})
        assert out == "output/005.md"

    def test_slug_filter(self) -> None:
        out = render_path_template("output/{{name|slug}}.md", {"name": "Hello World"})
        assert out == "output/hello-world.md"

    def test_unknown_var_raises(self) -> None:
        with pytest.raises(OutputParseError, match="unknown variable"):
            render_path_template("output/{{x}}.md", {})

    def test_zero_pad_with_non_int_raises(self) -> None:
        with pytest.raises(OutputParseError, match="expects an integer"):
            render_path_template("output/{{num:03d}}.md", {"num": "abc"})


# --------------------------------------------------------------------------- #
# _parse_json_object
# --------------------------------------------------------------------------- #


class TestParseJSONObject:
    def test_empty_returns_error(self) -> None:
        obj, err = _parse_json_object("")
        assert obj is None
        assert err is not None

    def test_simple_object(self) -> None:
        obj, err = _parse_json_object('{"a": 1}')
        assert obj == {"a": 1}
        assert err is None

    def test_with_surrounding_text(self) -> None:
        obj, err = _parse_json_object('prefix\n{"a": 1}\nsuffix')
        assert obj == {"a": 1}
        assert err is None

    def test_with_codefence(self) -> None:
        raw = '```json\n{"a": 1}\n```'
        obj, err = _parse_json_object(raw)
        assert obj == {"a": 1}
        assert err is None

    def test_array_returns_error(self) -> None:
        obj, err = _parse_json_object("[1, 2, 3]")
        assert obj is None
        assert err is not None
        assert "not an object" in err

    def test_invalid_json(self) -> None:
        obj, err = _parse_json_object("{not valid")
        assert obj is None
        assert err is not None


# --------------------------------------------------------------------------- #
# _strip_completion_signal
# --------------------------------------------------------------------------- #


class TestStripCompletionSignal:
    def test_no_signal_returns_unchanged(self) -> None:
        body = "some content"
        assert _strip_completion_signal(body, None) == body

    def test_signal_line_removed(self) -> None:
        body = f"some content\n{DEFAULT_COMPLETION_SIGNAL}\nmore"
        out = _strip_completion_signal(body, DEFAULT_COMPLETION_SIGNAL)
        assert DEFAULT_COMPLETION_SIGNAL not in out
        assert "some content" in out
        assert "more" in out


# --------------------------------------------------------------------------- #
# parse()
# --------------------------------------------------------------------------- #


class TestParse:
    def test_text_produce(self, tmp_path: Path) -> None:
        produce = ProduceSpec(path="output/x.md", alias="x")
        result = parse(
            "hello world",
            [produce],
            project_root=tmp_path,
            stage_id="test",
        )
        assert isinstance(result, ParseResult)
        assert len(result.produces) == 1
        assert result.produces[0].form == "text"
        assert result.produces[0].paths[0] == (tmp_path / "output" / "x.md").resolve()
        content = (tmp_path / "output" / "x.md").read_text(encoding="utf-8")
        assert "hello world" in content

    def test_json_produce(self, tmp_path: Path) -> None:
        produce = ProduceSpec(path="output/x.json", alias="x", form="json")
        result = parse(
            '{"passed": true, "findings": []}',
            [produce],
            project_root=tmp_path,
            stage_id="test",
        )
        assert result.produces[0].form == "json"
        assert result.produces[0].parsed == {"passed": True, "findings": []}
        # File should be written as pretty-printed JSON.
        written = (tmp_path / "output" / "x.json").read_text(encoding="utf-8")
        assert '"passed": true' in written

    def test_json_produce_invalid_raises(self, tmp_path: Path) -> None:
        produce = ProduceSpec(path="output/x.json", alias="x", form="json")
        with pytest.raises(SchemaInvalid):
            parse(
                "not json at all",
                [produce],
                project_root=tmp_path,
                stage_id="test",
            )

    def test_json_produce_empty_raises_verify_failed(self, tmp_path: Path) -> None:
        # Empty output is Tier C ("没写产物", spec §4.3) — routes to the
        # whole-stage retry loop instead of an immediate Tier B pause.
        produce = ProduceSpec(path="output/x.json", alias="x", form="json")
        with pytest.raises(VerifyFailed, match="empty"):
            parse(
                "",
                [produce],
                project_root=tmp_path,
                stage_id="test",
            )

    def test_json_produce_whitespace_only_raises_verify_failed(
        self, tmp_path: Path
    ) -> None:
        produce = ProduceSpec(path="output/x.json", alias="x", form="json")
        with pytest.raises(VerifyFailed):
            parse(
                "   \n\t ",
                [produce],
                project_root=tmp_path,
                stage_id="test",
            )

    def test_split_produce(self, tmp_path: Path) -> None:
        produce = ProduceSpec(
            path="output/{{num:03d}}.md",
            alias="chapters",
            split=r"^##\s+Chapter\s+(?P<num>\d+)",
        )
        raw = (
            "intro\n"
            "## Chapter 1\nfirst body\n"
            "## Chapter 2\nsecond body\n"
            "## Chapter 3\nthird body"
        )
        result = parse(
            raw,
            [produce],
            project_root=tmp_path,
            stage_id="test",
        )
        assert result.produces[0].form == "split"
        assert len(result.produces[0].paths) == 3
        # Each file should exist.
        for p in result.produces[0].paths:
            assert p.exists()

    def test_split_no_match_raises(self, tmp_path: Path) -> None:
        produce = ProduceSpec(
            path="output/{{num:03d}}.md",
            alias="chapters",
            split=r"^##\s+Chapter\s+(?P<num>\d+)",
        )
        with pytest.raises(OutputParseError, match="did not match"):
            parse(
                "no chapter headers here",
                [produce],
                project_root=tmp_path,
                stage_id="test",
            )

    def test_invalid_split_regex_raises(self, tmp_path: Path) -> None:
        produce = ProduceSpec(
            path="output/{{num:03d}}.md",
            alias="chapters",
            split=r"(",  # Invalid regex.
        )
        with pytest.raises(OutputParseError, match="invalid"):
            parse(
                "anything",
                [produce],
                project_root=tmp_path,
                stage_id="test",
            )

    def test_multiple_produces(self, tmp_path: Path) -> None:
        produces = [
            ProduceSpec(path="output/a.md", alias="a"),
            ProduceSpec(path="output/b.json", alias="b", form="json"),
        ]
        result = parse(
            '{"x": 1}',
            produces,
            project_root=tmp_path,
            stage_id="test",
        )
        assert len(result.produces) == 2
        assert result.produces[0].alias == "a"
        assert result.produces[1].alias == "b"
        assert result.produces[1].parsed == {"x": 1}
        assert len(result.all_paths) == 2

    def test_placeholder_substitution(self, tmp_path: Path) -> None:
        produce = ProduceSpec(path="output/{{num:03d}}.md", alias="x")
        result = parse(
            "content",
            [produce],
            project_root=tmp_path,
            stage_id="test",
            placeholder_values={"num": 7},
        )
        assert result.produces[0].paths[0] == (tmp_path / "output" / "007.md").resolve()

    def test_signal_stripped_before_writing(self, tmp_path: Path) -> None:
        produce = ProduceSpec(path="output/x.md", alias="x")
        # Signal on its own line — the stripper drops the whole line.
        parse(
            f"hello\n{DEFAULT_COMPLETION_SIGNAL}",
            [produce],
            project_root=tmp_path,
            stage_id="test",
            completion_signal=DEFAULT_COMPLETION_SIGNAL,
        )
        written = (tmp_path / "output" / "x.md").read_text(encoding="utf-8")
        assert DEFAULT_COMPLETION_SIGNAL not in written
        assert "hello" in written


# --------------------------------------------------------------------------- #
# ProduceParseResult / ParseResult
# --------------------------------------------------------------------------- #


class TestResultDataclasses:
    def test_all_paths_aggregates_across_produces(self) -> None:
        r = ParseResult(
            produces=[
                ProduceParseResult(alias="a", form="text", paths=[Path("a1"), Path("a2")]),
                ProduceParseResult(alias="b", form="text", paths=[Path("b1")]),
            ]
        )
        assert r.all_paths == [Path("a1"), Path("a2"), Path("b1")]

    def test_empty_result_all_paths(self) -> None:
        r = ParseResult()
        assert r.all_paths == []
