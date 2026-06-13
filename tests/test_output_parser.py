"""Tests for the v4 output-form inference + parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novelforge.claude.output_parser import (
    ParseResult,
    infer_form,
    parse,
    render_path_template,
)
from novelforge.errors import OutputParseError, SchemaInvalid


# --------------------------------------------------------------------------- #
# infer_form
# --------------------------------------------------------------------------- #


def test_infer_form_text() -> None:
    assert infer_form("output/x.md") == "text"
    assert infer_form("output/summaries/plot.md") == "text"
    assert infer_form("foo.txt") == "text"


def test_infer_form_json() -> None:
    assert infer_form("output/review/chapter-review.json") == "json"
    assert infer_form("output/x.json") == "json"


def test_infer_form_split_with_placeholder() -> None:
    assert infer_form("output/x/{{num}}.md") == "split"
    assert infer_form("output/chapters/{{num:03d}}-{{title|slug}}.md") == "split"


def test_infer_form_rejects_json_with_placeholder() -> None:
    """A15: ``.json`` suffix + ``{{x}}`` placeholder is illegal."""

    with pytest.raises(ValueError, match="\\.json"):
        infer_form("output/review/chunks/{{num}}.json")


def test_infer_form_rejects_empty() -> None:
    with pytest.raises(ValueError):
        infer_form("")


# --------------------------------------------------------------------------- #
# render_path_template
# --------------------------------------------------------------------------- #


def test_render_path_template_passthrough() -> None:
    assert render_path_template("output/x.md") == "output/x.md"


def test_render_path_template_zero_pad() -> None:
    out = render_path_template("output/c-{{num:03d}}.md", {"num": 7})
    assert out == "output/c-007.md"


def test_render_path_template_slug_filter() -> None:
    out = render_path_template(
        "output/c-{{num}}-{{title|slug}}.md",
        {"num": 1, "title": "The Summons!?"},
    )
    assert out == "output/c-1-the-summons.md"


def test_render_path_template_missing_var_raises() -> None:
    with pytest.raises(OutputParseError, match="unknown variable"):
        render_path_template("output/{{num}}.md", {})


# --------------------------------------------------------------------------- #
# parse — text
# --------------------------------------------------------------------------- #


def test_parse_text_writes_raw_output(tmp_path: Path) -> None:
    result = parse(
        "hello world",
        form="text",
        output_template="output/x.md",
        split_regex=None,
        project_root=tmp_path,
        stage_id="s",
    )
    assert isinstance(result, ParseResult)
    assert result.form == "text"
    assert result.text == "hello world"
    assert result.written_path is not None
    assert result.written_path.read_text(encoding="utf-8") == "hello world"


# --------------------------------------------------------------------------- #
# parse — json
# --------------------------------------------------------------------------- #


def test_parse_json_writes_payload(tmp_path: Path) -> None:
    payload = json.dumps({"route": "done", "findings": []})
    result = parse(
        payload,
        form="json",
        output_template="output/review/x.json",
        split_regex=None,
        project_root=tmp_path,
        stage_id="s",
    )
    assert result.form == "json"
    assert result.data == {"route": "done", "findings": []}
    assert result.written_path is not None
    on_disk = json.loads(result.written_path.read_text(encoding="utf-8"))
    assert on_disk == {"route": "done", "findings": []}


def test_parse_json_rejects_non_json(tmp_path: Path) -> None:
    with pytest.raises(SchemaInvalid, match="non-JSON"):
        parse(
            "totally not json",
            form="json",
            output_template="output/x.json",
            split_regex=None,
            project_root=tmp_path,
            stage_id="write_review",
        )


def test_parse_json_handles_fenced_block(tmp_path: Path) -> None:
    raw = "Here you go:\n\n```json\n" + json.dumps({"route": "done"}) + "\n```\n"
    result = parse(
        raw,
        form="json",
        output_template="output/x.json",
        split_regex=None,
        project_root=tmp_path,
        stage_id="s",
    )
    assert result.data == {"route": "done"}


# --------------------------------------------------------------------------- #
# parse — split
# --------------------------------------------------------------------------- #


def test_parse_split_writes_one_file_per_segment(tmp_path: Path) -> None:
    raw = (
        "# Chapter 1 - The Summons\n\nIt was dark.\n\n"
        "# Chapter 2 - The Choice\n\nShe hesitated.\n"
    )
    result = parse(
        raw,
        form="split",
        output_template="output/c-{{num:03d}}-{{title|slug}}.md",
        split_regex=r"^#\s+Chapter\s+(?P<num>\d+)\s*[-–—:]?\s*(?P<title>.+?)$",
        project_root=tmp_path,
        stage_id="write_chapter",
    )
    assert result.form == "split"
    assert len(result.written_paths) == 2
    assert len(result.segments) == 2
    seg0 = result.segments[0]
    assert seg0["matches"]["num"] == "1"
    assert seg0["matches"]["title"] == "The Summons"
    # File names use slugged title
    assert result.written_paths[0].name == "c-001-the-summons.md"
    assert result.written_paths[1].name == "c-002-the-choice.md"
    # The body was trimmed into the file
    body0 = result.written_paths[0].read_text(encoding="utf-8")
    assert "It was dark" in body0


def test_parse_split_no_match_raises_output_parse_error(tmp_path: Path) -> None:
    with pytest.raises(OutputParseError, match="did not match"):
        parse(
            "no headings here at all",
            form="split",
            output_template="output/c-{{num:03d}}.md",
            split_regex=r"^#\s+Chapter\s+(?P<num>\d+)\s*$",
            project_root=tmp_path,
            stage_id="write_chapter",
        )


def test_parse_split_requires_regex(tmp_path: Path) -> None:
    with pytest.raises(OutputParseError, match="no `split` regex"):
        parse(
            "anything",
            form="split",
            output_template="output/c-{{num:03d}}.md",
            split_regex=None,
            project_root=tmp_path,
            stage_id="write_chapter",
        )


def test_parse_split_invalid_regex_raises_output_parse_error(tmp_path: Path) -> None:
    with pytest.raises(OutputParseError, match="invalid"):
        parse(
            "x",
            form="split",
            output_template="output/c-{{num}}.md",
            split_regex="(unclosed",
            project_root=tmp_path,
            stage_id="write_chapter",
        )
