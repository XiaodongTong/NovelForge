"""Tests for the v4 contract config (Phase 2.1 / 2.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novelforge.config import (
    DoneWhenSpec,
    NovelProjectConfig,
    ProduceSpec,
    StageConfig,
    load_config,
    stage_ids_for,
    stages_for,
    validate_stage,
    with_max_chapters,
)
from novelforge.errors import ConfigError, SchemaInvalid
from novelforge.verify import CheckSpec


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """A directory with valid seeds & constraints."""

    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("# Premise\n", encoding="utf-8")
    (tmp_path / "outline" / "world.md").write_text("# World\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Style rules\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def base_yaml() -> str:
    return """
novel:
  title: "Test Novel"
  genre: "Sci-Fi"
  target_chapters: 12
  words_per_chapter: [2500, 3000]
  style: "lean, modern"
  seeds:
    - outline/premise.md
    - outline/world.md
  constraints:
    - CLAUDE.md

pipeline:
  stages:
    - id: write
      model: claude-opus-4-7
      prompt: "Write a chapter."
      produces:
        - path: "output/chapters/{{num:03d}}.md"
          alias: chapter
      batch: 3
      done_when:
        checks:
          - kind: min_chars
            target: "output/chapters/{{num:03d}}.md"
            value: 100

execution:
  batch_size:
    outline: 50
    chapter: 3
  max_review_iterations: 3
  review_model: "claude-sonnet-4-6"
  write_model: "claude-opus-4-7"
"""


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_load_config_happy_path(project_root: Path, base_yaml: str) -> None:
    cfg_path = _write(project_root, "novel-project.yaml", base_yaml)
    cfg = load_config(cfg_path)
    assert isinstance(cfg, NovelProjectConfig)
    assert cfg.novel.title == "Test Novel"
    assert cfg.novel.target_chapters == 12
    assert stage_ids_for(cfg) == ["write"]
    stage = cfg.pipeline.stages[0]
    assert stage.produces[0].alias == "chapter"
    assert stage.produces[0].path == "output/chapters/{{num:03d}}.md"
    assert stage.done_when.max_attempts == 3
    assert len(stage.done_when.checks) == 1
    assert stage.done_when.checks[0].kind == "min_chars"


# --------------------------------------------------------------------------- #
# v3 fields rejected
# --------------------------------------------------------------------------- #


def test_template_field_rejected(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="pipeline.template"):
        load_config(cfg_path)


def test_stages_override_field_rejected(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages: []
  stages_override: [generate_outline]
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="pipeline.stages_override"):
        load_config(cfg_path)


def test_scaffold_from_field_rejected(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  scaffold_from: long-epic
  stages:
    - id: x
      model: m
      prompt: p
      produces:
        - path: output/x.md
          alias: x
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="pipeline.scaffold_from"):
        load_config(cfg_path)


def test_missing_stages_rejected(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="pipeline.stages is required"):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# Required fields
# --------------------------------------------------------------------------- #


def test_missing_novel_section_raises(project_root: Path) -> None:
    body = """
pipeline:
  stages:
    - id: x
      model: m
      prompt: p
      produces:
        - path: o
          alias: o
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="novel"):
        load_config(cfg_path)


def test_target_chapters_must_be_positive(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 0
  words_per_chapter: [1, 2]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: x
      model: m
      prompt: p
      produces:
        - path: o
          alias: o
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="target_chapters"):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# StageConfig dataclass
# --------------------------------------------------------------------------- #


def test_stage_config_requires_produces() -> None:
    with pytest.raises(ConfigError, match="produces"):
        StageConfig(id="x", model="m", prompt="p", produces=())


def test_stage_config_alias_uniqueness() -> None:
    """AC-16: produces[].alias must be unique within a stage."""

    with pytest.raises(ConfigError, match="duplicate produces alias"):
        StageConfig(
            id="x",
            model="m",
            prompt="p",
            produces=(
                ProduceSpec(path="output/a.md", alias="out"),
                ProduceSpec(path="output/b.md", alias="out"),
            ),
        )


def test_stage_config_defaults_done_when() -> None:
    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        produces=(ProduceSpec(path="output/x.md", alias="x"),),
    )
    assert s.done_when.max_attempts == 3
    assert s.done_when.completion_signal  # default marker
    assert s.done_when.checks == ()
    assert s.consumes is None  # default: all upstreams
    assert s.batch == 1
    assert s.on_failure == "pause"
    assert s.enabled is True


def test_stage_config_rejects_zero_batch() -> None:
    with pytest.raises(ConfigError, match="batch"):
        StageConfig(
            id="x",
            model="m",
            prompt="p",
            produces=(ProduceSpec(path="o", alias="o"),),
            batch=0,
        )


def test_stage_config_rejects_bad_on_failure() -> None:
    with pytest.raises(ConfigError, match="on_failure"):
        StageConfig(
            id="x",
            model="m",
            prompt="p",
            produces=(ProduceSpec(path="o", alias="o"),),
            on_failure="halt",
        )


def test_produce_spec_rejects_bad_alias() -> None:
    with pytest.raises(ConfigError, match="alias"):
        ProduceSpec(path="output/x.md", alias="1bad")
    with pytest.raises(ConfigError, match="alias"):
        ProduceSpec(path="output/x.md", alias="has-dash")


# --------------------------------------------------------------------------- #
# Placeholder / split / batch validation (AC-16)
# --------------------------------------------------------------------------- #


def test_validate_batch_stage_uses_num_placeholder() -> None:
    """batch>1 requires {{num}}; no split."""

    s = StageConfig(
        id="write_chapter",
        model="m",
        prompt="p",
        produces=(
            ProduceSpec(path="output/chapters/{{num:03d}}.md", alias="chapter"),
        ),
        batch=3,
    )
    assert validate_stage(s) == []


def test_validate_batch_stage_requires_num_placeholder() -> None:
    s = StageConfig(
        id="write_chapter",
        model="m",
        prompt="p",
        produces=(
            ProduceSpec(path="output/chapters/x.md", alias="chapter"),
        ),
        batch=3,
    )
    errs = validate_stage(s)
    assert any("{{num}}" in e for e in errs)


def test_validate_split_stage_requires_capture_groups() -> None:
    """split regex must capture every {{name}} placeholder."""

    s = StageConfig(
        id="split_stage",
        model="m",
        prompt="p",
        produces=(
            ProduceSpec(
                path="output/c-{{num}}-{{title}}.md",
                alias="c",
                split=r"^# Chapter (?P<num>\d+)$",  # missing 'title' group
            ),
        ),
    )
    errs = validate_stage(s)
    assert any("title" in e for e in errs)


def test_validate_split_stage_capture_groups_match_path() -> None:
    s = StageConfig(
        id="split_stage",
        model="m",
        prompt="p",
        produces=(
            ProduceSpec(
                path="output/c-{{num}}-{{title}}.md",
                alias="c",
                split=(
                    r"^# Chapter (?P<num>\d+) - (?P<title>.+?)$"
                ),
            ),
        ),
    )
    assert validate_stage(s) == []


def test_validate_batch_plus_split_is_illegal() -> None:
    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        produces=(
            ProduceSpec(
                path="output/c-{{num}}.md",
                alias="c",
                split=r"^# (?P<num>\d+)$",
            ),
        ),
        batch=3,
    )
    errs = validate_stage(s)
    assert any("batch" in e and "split" in e for e in errs)


def test_validate_single_produce_rejects_placeholders() -> None:
    """A non-batch, non-split produce must not have placeholders."""

    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        produces=(
            ProduceSpec(path="output/c-{{num}}.md", alias="c"),
        ),
    )
    errs = validate_stage(s)
    assert any("placeholders" in e for e in errs)


def test_validate_split_invalid_regex() -> None:
    s = StageConfig(
        id="x",
        model="m",
        prompt="p",
        produces=(
            ProduceSpec(
                path="output/c-{{num}}.md",
                alias="c",
                split="(unclosed",
            ),
        ),
    )
    errs = validate_stage(s)
    assert any("invalid" in e.lower() or "regex" in e.lower() for e in errs)


# --------------------------------------------------------------------------- #
# Cross-stage alias uniqueness (AC-18)
# --------------------------------------------------------------------------- #


def test_cross_stage_alias_overlap_rejected(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: a
      model: m
      prompt: p
      produces:
        - path: output/a.md
          alias: out
    - id: b
      model: m
      prompt: p
      produces:
        - path: output/b.md
          alias: out
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(SchemaInvalid, match="cross-stage"):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# consumes three-state (AC-5, AC-6)
# --------------------------------------------------------------------------- #


def test_consumes_default_is_all_upstreams(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: a
      model: m
      prompt: p
      produces:
        - path: output/a.md
          alias: out
    - id: b
      model: m
      prompt: p
      produces:
        - path: output/b.md
          alias: out_b
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.pipeline.stages[0].consumes is None
    assert cfg.pipeline.stages[1].consumes is None


def test_consumes_explicit_list(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: a
      model: m
      prompt: p
      produces:
        - path: output/a.md
          alias: out_a
    - id: b
      model: m
      prompt: p
      produces:
        - path: output/b.md
          alias: out_b
      consumes: [a]
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.pipeline.stages[1].consumes == ("a",)


def test_consumes_explicit_empty_list(project_root: Path) -> None:
    """consumes: [] means no upstreams — distinct from None."""

    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: a
      model: m
      prompt: p
      produces:
        - path: output/a.md
          alias: out_a
    - id: b
      model: m
      prompt: p
      produces:
        - path: output/b.md
          alias: out_b
      consumes: []
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.pipeline.stages[1].consumes == ()


def test_consumes_explicit_null(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: a
      model: m
      prompt: p
      produces:
        - path: output/a.md
          alias: out_a
    - id: b
      model: m
      prompt: p
      produces:
        - path: output/b.md
          alias: out_b
      consumes: null
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.pipeline.stages[1].consumes is None


# --------------------------------------------------------------------------- #
# Execution block
# --------------------------------------------------------------------------- #


def test_execution_overrides_are_applied(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: x
      model: m
      prompt: p
      produces:
        - path: o
          alias: o
execution:
  batch_size:
    outline: 25
    chapter: 2
  context:
    total: 100000
    context_reserve: 30000
    output_reserve: 5000
  retry:
    max_retries: 5
    backoff: "linear"
    max_wait: 120
  max_review_iterations: 7
"""
    cfg = load_config(_write(project_root, "novel-project.yaml", body))
    assert cfg.execution.batch_size.outline == 25
    assert cfg.execution.context.total == 100_000
    assert cfg.execution.retry.max_retries == 5
    assert cfg.execution.max_review_iterations == 7


def test_invalid_backoff_raises(project_root: Path) -> None:
    body = """
novel:
  title: X
  genre: X
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: X
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: x
      model: m
      prompt: p
      produces:
        - path: o
          alias: o
execution:
  retry: { backoff: "silly" }
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="backoff"):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_with_max_chapters_overrides(project_root: Path, base_yaml: str) -> None:
    cfg_path = _write(project_root, "novel-project.yaml", base_yaml)
    cfg = load_config(cfg_path)
    assert cfg.novel.target_chapters == 12
    new_cfg = with_max_chapters(cfg, 1)
    assert new_cfg.novel.target_chapters == 1


def test_stages_for_returns_list(project_root: Path, base_yaml: str) -> None:
    cfg = load_config(_write(project_root, "novel-project.yaml", base_yaml))
    assert len(stages_for(cfg)) == 1


def test_yaml_parse_error_is_configerror(tmp_path: Path) -> None:
    p = tmp_path / "broken.yaml"
    p.write_text("novel: : :", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)


def test_missing_file_is_configerror(tmp_path: Path) -> None:
    p = tmp_path / "absent.yaml"
    with pytest.raises(ConfigError, match="not found"):
        load_config(p)
