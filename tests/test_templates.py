"""Tests for the v4 contract templates module (Phase 3.6)."""

from __future__ import annotations

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
