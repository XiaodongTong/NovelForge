"""Tests for the v4 contract templates module (Phase 3.6)."""

from __future__ import annotations

import re

import pytest

from novelforge.config import StageConfig, validate_stage
from novelforge.errors import ConfigError
from novelforge.templates import (
    BUILTIN_TEMPLATES,
    ContractTemplate,
    VALID_TEMPLATES,
    get_template,
)


def test_builtin_templates_present() -> None:
    assert "long-epic" in BUILTIN_TEMPLATES
    assert "short-story" in BUILTIN_TEMPLATES
    assert VALID_TEMPLATES == frozenset(BUILTIN_TEMPLATES)


def test_get_template_unknown_raises() -> None:
    with pytest.raises(ConfigError, match="unknown template"):
        get_template("does-not-exist")


def test_long_epic_has_required_stages() -> None:
    t = get_template("long-epic")
    assert t.stage_ids() == [
        "generate_outline",
        "design_characters",
        "write_chapter",
        "review_chapter",
        "final_polish",
    ]


def test_each_stage_has_produces_and_done_when() -> None:
    for tpl in BUILTIN_TEMPLATES.values():
        for stage in tpl.stages:
            assert isinstance(stage, StageConfig)
            assert stage.produces, f"stage {stage.id} has no produces"
            assert stage.done_when is not None
            errs = validate_stage(stage)
            assert errs == [], f"stage {stage.id} invalid: {errs}"


def test_template_payload_round_trips_to_yaml_dict() -> None:
    t = get_template("short-story")
    payload = t.to_payload()
    assert isinstance(payload, list)
    assert len(payload) == 2
    # Every stage has the v4 contract fields.
    for entry in payload:
        assert "id" in entry
        assert "produces" in entry
        assert "done_when" in entry


def test_consumes_chain_is_consistent() -> None:
    """Every non-None consumes reference points at an earlier stage."""

    for tpl in BUILTIN_TEMPLATES.values():
        earlier: set[str] = set()
        for stage in tpl.stages:
            if stage.consumes is not None:
                for c in stage.consumes:
                    assert c in earlier, (
                        f"template {tpl.name}: stage {stage.id} consumes "
                        f"{c!r} which is not an earlier stage"
                    )
            earlier.add(stage.id)


def test_long_epic_uses_batch_for_write_chapter() -> None:
    t = get_template("long-epic")
    write = next(s for s in t.stages if s.id == "write_chapter")
    assert write.batch == 3
    # batch path must reference {{num}}
    assert any("{{num" in p.path for p in write.produces)


def test_short_story_write_chapter_is_single() -> None:
    t = get_template("short-story")
    write = next(s for s in t.stages if s.id == "write_chapter")
    assert write.batch == 1
    # No placeholders in the path (single produce).
    for p in write.produces:
        assert "{{" not in p.path


def test_template_rejects_empty_stage_list() -> None:
    with pytest.raises(ConfigError, match="at least one stage"):
        ContractTemplate(name="empty", description="x", stages=())


def test_prompts_cover_every_stage() -> None:
    for tpl in BUILTIN_TEMPLATES.values():
        for stage in tpl.stages:
            assert stage.id in tpl.prompts, (
                f"template {tpl.name}: stage {stage.id} has no prompt body"
            )


def test_no_v3_fields_in_template_payload() -> None:
    """Templates must not emit v3 fields (template / scaffold_from /
    stages_override / output / split at stage-level)."""

    for tpl in BUILTIN_TEMPLATES.values():
        for entry in tpl.to_payload():
            for forbidden in ("template", "scaffold_from", "stages_override", "output"):
                assert forbidden not in entry, (
                    f"template {tpl.name}: payload has v3 field {forbidden!r}"
                )


# --------------------------------------------------------------------------- #
# PR-2: split mode + new paths
# --------------------------------------------------------------------------- #


def test_generate_outline_output_path_is_chapters_outline() -> None:
    """AC-8: ``generate_outline.produces[0].path`` is
    ``chapters-outline/outline.md`` (not the legacy
    ``output/summaries/plot.md``)."""

    t = get_template("long-epic")
    outline = next(s for s in t.stages if s.id == "generate_outline")
    assert len(outline.produces) == 1
    assert outline.produces[0].path == "chapters-outline/outline.md"
    assert outline.produces[0].alias == "outline"
    # done_when target tracks the same path (per-file placeholder is
    # implicit — single-file produce).
    assert outline.done_when.checks[0].target == "chapters-outline/outline.md"


def test_design_characters_has_split_regex() -> None:
    """AC-7: ``design_characters.produces[0]`` declares a split regex
    with a slug capture group, and the path uses ``{{slug}}``."""

    t = get_template("long-epic")
    chars = next(s for s in t.stages if s.id == "design_characters")
    assert len(chars.produces) == 1
    produce = chars.produces[0]
    assert produce.split, "design_characters must declare split regex (AC-7)"
    # Path contains {{slug}} (no |default(...) per TD-2).
    assert "{{slug}}" in produce.path
    assert "|" not in produce.path
    assert produce.path == "characters/{{slug}}.md"


def test_design_characters_split_regex_is_ascii_slug_safe() -> None:
    """TD-7: split regex accepts only ``[A-Za-z0-9_-]+`` slugs; any
    non-conformant heading is silently dropped by the regex (and
    therefore surfaces as a downstream VerifyFailed rather than an
    unsafe filename)."""

    t = get_template("long-epic")
    chars = next(s for s in t.stages if s.id == "design_characters")
    regex = re.compile(chars.produces[0].split or "", re.MULTILINE)

    accepted = "# alice\nbody\n# bob_2\nbody\n# carol-9\nbody"
    rejected_spaces = "# Li Ming\nbody\n# alice\nbody"
    rejected_chinese = "# 李明\nbody\n# 张三\nbody"
    rejected_punct = "# mr.smith\nbody\n# alice\nbody"

    assert regex.findall(accepted) == ["alice", "bob_2", "carol-9"]
    # The regex must silently drop unsafe headings — only the ASCII
    # ones survive.
    assert regex.findall(rejected_spaces) == ["alice"]
    assert regex.findall(rejected_chinese) == []
    assert regex.findall(rejected_punct) == ["alice"]


def test_design_characters_done_when_uses_slug_placeholder() -> None:
    """AC-11: ``done_when.checks[0].target`` substitutes ``{{slug}}``
    per file so each split artefact is independently verified."""

    t = get_template("long-epic")
    chars = next(s for s in t.stages if s.id == "design_characters")
    target = chars.done_when.checks[0].target
    assert target == "characters/{{slug}}.md"


def test_no_legacy_paths_in_template_payload() -> None:
    """AC-10 (sample parity): the built-in templates no longer
    reference the legacy ``output/summaries/plot.md`` or
    ``output/meta/characters.md`` paths."""

    legacy = ("output/summaries/plot.md", "output/meta/characters.md")
    for tpl in BUILTIN_TEMPLATES.values():
        raw = yaml_safe_dump(tpl.to_payload())
        for path in legacy:
            assert path not in raw, (
                f"template {tpl.name} still emits legacy path {path!r}"
            )


def yaml_safe_dump(obj: object) -> str:
    """Local helper to keep this file from adding a top-level yaml import."""

    import yaml

    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)
