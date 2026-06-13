"""Tests for the v4 ``StageConfig`` dataclass + validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from novelforge.config import (
    ExecutionSpec,
    PipelineSpec,
    StageConfig,
    deprecation_warnings_for,
    load_config,
    stage_ids_for,
    validate_stage,
)
from novelforge.errors import ConfigError, SchemaInvalid


# --------------------------------------------------------------------------- #
# StageConfig dataclass
# --------------------------------------------------------------------------- #


def test_stage_config_defaults() -> None:
    s = StageConfig(
        id="x", model="m", prompt="p", output="output/x.md"
    )
    assert s.split is None
    assert s.batch == 1
    assert s.on_failure == "pause"
    assert s.enabled is True


def test_stage_config_to_dict_minimal() -> None:
    s = StageConfig(
        id="x", model="m", prompt="p", output="output/x.md"
    )
    d = s.to_dict()
    # Defaults are omitted from yaml render.
    assert d == {
        "id": "x",
        "model": "m",
        "prompt": "p",
        "output": "output/x.md",
    }


def test_stage_config_to_dict_with_overrides() -> None:
    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        output="output/c-{{num:03d}}.md",
        split=r"^# (?P<num>\d+)$",
        batch=3,
        on_failure="skip",
        enabled=False,
    )
    d = s.to_dict()
    assert d["split"] == r"^# (?P<num>\d+)$"
    assert d["batch"] == 3
    assert d["on_failure"] == "skip"
    assert d["enabled"] is False


def test_stage_config_rejects_empty_id() -> None:
    with pytest.raises(ConfigError, match="'id' must be a non-empty string"):
        StageConfig(id="", model="m", prompt="p", output="o")


def test_stage_config_rejects_bad_on_failure() -> None:
    with pytest.raises(ConfigError, match="on_failure"):
        StageConfig(
            id="x", model="m", prompt="p", output="o", on_failure="halt"
        )


def test_stage_config_rejects_zero_batch() -> None:
    with pytest.raises(ConfigError, match="batch"):
        StageConfig(
            id="x", model="m", prompt="p", output="o", batch=0
        )


# --------------------------------------------------------------------------- #
# validate_stage
# --------------------------------------------------------------------------- #


def test_validate_stage_ok_text() -> None:
    s = StageConfig(
        id="write_chapter",
        model="m",
        prompt="p",
        output="output/x.md",
    )
    assert validate_stage(s) == []


def test_validate_stage_ok_json() -> None:
    s = StageConfig(
        id="review",
        model="m",
        prompt="p",
        output="output/review.json",
    )
    assert validate_stage(s) == []


def test_validate_stage_split_missing_split_field() -> None:
    """A10: placeholders in output but no split regex."""

    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        output="output/c-{{num:03d}}.md",
    )
    errs = validate_stage(s)
    assert any("'split' is missing" in e or "`split` is missing" in e for e in errs)


def test_validate_stage_split_placeholder_no_capture() -> None:
    """The split regex must capture every placeholder in the output."""

    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        output="output/c-{{num:03d}}-{{title|slug}}.md",
        split=r"^#\s+(?P<num>\d+)\s*$",  # no `title` group
    )
    errs = validate_stage(s)
    assert any("title" in e for e in errs)


def test_validate_stage_split_invalid_regex() -> None:
    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        output="output/c-{{num:03d}}.md",
        split="(unclosed",
    )
    errs = validate_stage(s)
    assert any("invalid" in e for e in errs)


def test_validate_stage_json_with_placeholder() -> None:
    """A15: .json + placeholder is forbidden."""

    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        output="output/review/{{num}}.json",
        split=r"^#\s+(?P<num>\d+)$",
    )
    errs = validate_stage(s)
    assert any(".json" in e and "placeholder" in e for e in errs)


# --------------------------------------------------------------------------- #
# Pipeline parsing — v4
# --------------------------------------------------------------------------- #


def _write(project_root: Path, name: str, body: str) -> Path:
    p = project_root / name
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("p", encoding="utf-8")
    (tmp_path / "outline" / "world.md").write_text("w", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
    return tmp_path


def test_v4_explicit_stages_parsed(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  scaffold_from: "long-epic"
  stages:
    - id: outline
      model: m
      prompt: "do outline"
      output: "output/outline.md"
    - id: write
      model: m
      prompt: "write chapter"
      output: "output/c-{{num:03d}}.md"
      split: "^# Chapter (?P<num>[0-9]+)"
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.pipeline.template is None
    assert cfg.pipeline.scaffold_from == "long-epic"
    assert stage_ids_for(cfg) == ["outline", "write"]
    # Stage objects expose the full 8 fields.
    write_stage = cfg.pipeline.stages[1]
    assert write_stage.split is not None
    assert write_stage.batch == 1


def test_v4_stages_missing_required_field(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: outline
      prompt: "do outline"
      output: "output/outline.md"
"""
    with pytest.raises(SchemaInvalid, match="'model' is required"):
        load_config(_write(project_root, "novel-project.yaml", body))


def test_v4_stages_duplicate_id(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: x
      model: m
      prompt: p
      output: o
    - id: x
      model: m
      prompt: p
      output: o
"""
    with pytest.raises(SchemaInvalid, match="duplicate id"):
        load_config(_write(project_root, "novel-project.yaml", body))


def test_v4_template_synthesizes_stage_configs(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    # No explicit stages → 10 synthetic StageConfig records
    assert len(cfg.pipeline.stages) == 10
    assert cfg.pipeline.stages[0].id == "generate_outline"
    # Each stage has a model and a prompt.
    assert cfg.pipeline.stages[0].model == "claude-opus-4-7"
    assert cfg.pipeline.stages[0].prompt


# --------------------------------------------------------------------------- #
# Deprecation messages
# --------------------------------------------------------------------------- #


def test_deprecation_warnings_for_template(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    msgs = deprecation_warnings_for(cfg)
    assert any("template" in m for m in msgs)
    assert not any("stages_override" in m for m in msgs)


def test_deprecation_warnings_for_override(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
  stages_override: [generate_outline]
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    msgs = deprecation_warnings_for(cfg)
    assert len(msgs) == 2, msgs
    assert any("template" in m for m in msgs)
    assert any("stages_override" in m for m in msgs)


def test_no_deprecation_warnings_for_v4(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  scaffold_from: "long-epic"
  stages:
    - id: x
      model: m
      prompt: p
      output: o
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert deprecation_warnings_for(cfg) == []


def test_scaffold_from_with_unknown_template_no_warning(project_root: Path) -> None:
    """A16: scaffold_from is pure metadata, even unknown values are ignored."""

    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  scaffold_from: "not-a-real-template"
  stages:
    - id: x
      model: m
      prompt: p
      output: o
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.pipeline.scaffold_from == "not-a-real-template"
    # No deprecation / error
    assert deprecation_warnings_for(cfg) == []


# --------------------------------------------------------------------------- #
# ExecutionSpec.route_history_max
# --------------------------------------------------------------------------- #


def test_execution_route_history_max_default(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.execution.route_history_max == 50


def test_execution_route_history_max_override(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
execution:
  route_history_max: 100
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.execution.route_history_max == 100
