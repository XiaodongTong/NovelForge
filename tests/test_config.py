"""M1 tests: config loading & validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from novelforge.config import (
    PIPELINE_TEMPLATES,
    NovelProjectConfig,
    load_config,
    stage_ids_for,
    stages_for,
    with_max_chapters,
)
from novelforge.errors import ConfigError


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """A directory with valid seeds & constraints and a baseline yaml."""

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
  template: "long-epic"

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
    assert cfg.novel.words_per_chapter == (2500, 3000)
    assert cfg.pipeline.template == "long-epic"
    assert stage_ids_for(cfg)[0] == "generate_outline"
    assert cfg.execution.retry.max_retries == 3
    # seeds/constraints resolved relative to project root
    assert str(cfg.project_path).endswith("novel-project.yaml")


def test_default_template_is_long_epic(project_root: Path) -> None:
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
    cfg_path = _write(project_root, "novel-project.yaml", body)
    cfg = load_config(cfg_path)
    assert cfg.pipeline.template == "long-epic"
    assert stage_ids_for(cfg) == list(PIPELINE_TEMPLATES["long-epic"])


# --------------------------------------------------------------------------- #
# Missing required fields
# --------------------------------------------------------------------------- #


def test_missing_novel_section_raises(project_root: Path) -> None:
    body = """
pipeline: { template: "long-epic" }
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="novel"):
        load_config(cfg_path)


def test_missing_title_raises(project_root: Path) -> None:
    body = """
novel:
  genre: "X"
  target_chapters: 1
  words_per_chapter: [1, 2]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="title"):
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
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="target_chapters"):
        load_config(cfg_path)


def test_words_per_chapter_length_must_be_two(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="words_per_chapter"):
        load_config(cfg_path)


def test_words_per_chapter_min_le_max(project_root: Path) -> None:
    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [3000, 2500]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="min"):
        load_config(cfg_path)


def test_missing_seed_file_warns_only(project_root: Path) -> None:
    """A13 / spec §4.1: validate must pass without outline/ seeds; the
    runtime surfaces missing files on the first {{include:}} stage."""

    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/does-not-exist.md]
  constraints: [CLAUDE.md]
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    cfg = load_config(cfg_path)
    assert cfg.novel.seeds == ("outline/does-not-exist.md",)


def test_missing_constraint_file_warns_only(project_root: Path) -> None:
    """A13 / spec §4.1: validate must pass without CLAUDE.md etc."""

    body = """
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [rules/missing.md]
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    cfg = load_config(cfg_path)
    assert cfg.novel.constraints == ("rules/missing.md",)


# --------------------------------------------------------------------------- #
# Templates & overrides
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("template", ["long-epic", "short-story", "series"])
def test_templates_resolve_to_stage_lists(project_root: Path, template: str) -> None:
    body = f"""
novel:
  title: "X"
  genre: "X"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "{template}"
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    cfg = load_config(cfg_path)
    assert stage_ids_for(cfg) == list(PIPELINE_TEMPLATES[template])


def test_stages_override_takes_precedence(project_root: Path) -> None:
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
  stages_override:
    - generate_outline
    - review_outline
    - write_chapter
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    cfg = load_config(cfg_path)
    assert stage_ids_for(cfg) == [
        "generate_outline",
        "review_outline",
        "write_chapter",
    ]
    assert cfg.pipeline.stages_override is not None


def test_unknown_template_raises(project_root: Path) -> None:
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
  template: "unknown"
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="unknown pipeline.template"):
        load_config(cfg_path)


def test_duplicate_stages_in_override_raises(project_root: Path) -> None:
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
  stages_override: [generate_outline, generate_outline]
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="duplicates"):
        load_config(cfg_path)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def test_execution_overrides_are_applied(project_root: Path) -> None:
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
    cfg_path = _write(project_root, "novel-project.yaml", body)
    cfg = load_config(cfg_path)
    assert cfg.execution.batch_size.outline == 25
    assert cfg.execution.batch_size.chapter == 2
    assert cfg.execution.context.total == 100_000
    assert cfg.execution.retry.max_retries == 5
    assert cfg.execution.retry.backoff == "linear"
    assert cfg.execution.max_review_iterations == 7


def test_invalid_backoff_raises(project_root: Path) -> None:
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
  retry: { backoff: "silly" }
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="backoff"):
        load_config(cfg_path)


def test_context_reserve_must_be_less_than_total(project_root: Path) -> None:
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
  context:
    total: 50000
    context_reserve: 40000
    output_reserve: 20000
"""
    cfg_path = _write(project_root, "novel-project.yaml", body)
    with pytest.raises(ConfigError, match="context_reserve"):
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
    # original is unchanged (frozen)
    assert cfg.novel.target_chapters == 12


def test_with_max_chapters_zero_raises(project_root: Path, base_yaml: str) -> None:
    cfg_path = _write(project_root, "novel-project.yaml", base_yaml)
    cfg = load_config(cfg_path)
    with pytest.raises(ConfigError):
        with_max_chapters(cfg, 0)


def test_yaml_parse_error_is_configerror(tmp_path: Path) -> None:
    p = tmp_path / "broken.yaml"
    p.write_text("novel: : :", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)


def test_missing_file_is_configerror(tmp_path: Path) -> None:
    p = tmp_path / "absent.yaml"
    with pytest.raises(ConfigError, match="not found"):
        load_config(p)
