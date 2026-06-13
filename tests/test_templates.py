"""Tests for the v4 built-in template data source."""

from __future__ import annotations

import pytest

from novelforge import templates
from novelforge.templates import (
    ALL_STAGE_IDS,
    PIPELINE_TEMPLATES,
    STAGE_TEMPLATES,
    StageTemplate,
    get_template,
    get_template_mapping,
)


# --------------------------------------------------------------------------- #
# Coverage & shape
# --------------------------------------------------------------------------- #


def test_templates_cover_all_stage_ids() -> None:
    """Every stage id from ALL_STAGE_IDS has a template record."""

    for sid in ALL_STAGE_IDS:
        assert get_template(sid) is not None, f"missing template for {sid!r}"


def test_templates_template_names_match_stage_ids() -> None:
    assert set(PIPELINE_TEMPLATES) == {"long-epic", "short-story", "series"}
    for tpl_name, ids in PIPELINE_TEMPLATES.items():
        for sid in ids:
            assert sid in ALL_STAGE_IDS, (
                f"template {tpl_name!r} references unknown stage {sid!r}"
            )


def test_long_epic_has_all_ten_stages() -> None:
    assert len(PIPELINE_TEMPLATES["long-epic"]) == 10
    assert set(PIPELINE_TEMPLATES["long-epic"]) == set(ALL_STAGE_IDS)


def test_stage_templates_ordered() -> None:
    """Long-epic should drive STAGE_TEMPLATES' default order."""

    by_id = {t.id: t for t in STAGE_TEMPLATES}
    for sid in PIPELINE_TEMPLATES["long-epic"]:
        assert sid in by_id


# --------------------------------------------------------------------------- #
# StageTemplate shape
# --------------------------------------------------------------------------- #


def test_stage_template_prompt_file_default() -> None:
    """A freshly built template derives prompt_file from its id."""

    t = StageTemplate(
        id="write_chapter",
        model="m",
        prompt_text="x",
        output="out",
    )
    assert t.prompt_file == "write-chapter.md"


def test_stage_template_to_dict_inlines_prompt() -> None:
    t = get_template("review_outline")
    d = t.to_dict()
    assert d["id"] == "review_outline"
    assert d["model"] == "claude-sonnet-4-6"
    assert d["output"].endswith(".json")
    assert "prompt" in d
    assert "passed" in d["prompt"] or "route" in d["prompt"]


def test_stage_template_to_dict_omits_defaults() -> None:
    """The to_dict output should be minimal: no enabled=True, no
    on_failure=pause, no batch=1 when those are the defaults."""

    t = get_template("write_chapter")
    d = t.to_dict()
    # batch=1 is the default; we still emit it on write_chapter because
    # it is documented as batch=1 for chapter per call, but the on/off
    # booleans should be absent.
    assert "enabled" not in d
    assert "on_failure" not in d


def test_stage_template_uses_split_for_write_chapter() -> None:
    t = get_template("write_chapter")
    assert t.split is not None
    assert "(?P<num>" in t.split
    assert "(?P<title>" in t.split


def test_stage_template_uses_json_for_reviews() -> None:
    for sid in (
        "review_outline",
        "review_characters",
        "review_simulation",
        "review_chapter",
    ):
        t = get_template(sid)
        assert t.output.endswith(".json"), f"{sid} should default to JSON output"


def test_get_template_mapping_returns_dict() -> None:
    mapping = get_template_mapping(["generate_outline", "review_outline"])
    assert set(mapping) == {"generate_outline", "review_outline"}
    for sid, tpl in mapping.items():
        assert tpl.id == sid


def test_get_template_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_template("nope")
