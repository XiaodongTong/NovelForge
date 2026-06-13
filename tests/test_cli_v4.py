"""v4 CLI tests — init / migrate / validate upgrades."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novelforge.cli import app


runner = CliRunner()


# --------------------------------------------------------------------------- #
# help
# --------------------------------------------------------------------------- #


def test_help_lists_v4_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("init", "migrate", "validate", "run", "resume", "status"):
        assert cmd in result.stdout, f"missing {cmd!r} in --help"


def test_init_help_present() -> None:
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    assert "--template" in result.stdout
    assert "--dir" in result.stdout


def test_migrate_help_present() -> None:
    result = runner.invoke(app, ["migrate", "--help"])
    assert result.exit_code == 0
    assert "--out" in result.stdout
    assert "--write" in result.stdout


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #


def test_validate_legacy_yaml_succeeds_with_warning(tmp_path: Path) -> None:
    """A1: legacy yaml with ``template:`` should validate with a
    deprecation warning."""

    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("p", encoding="utf-8")
    (tmp_path / "outline" / "world.md").write_text("w", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
    body = """
novel:
  title: "T"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
"""
    cfg = tmp_path / "novel-project.yaml"
    cfg.write_text(body, encoding="utf-8")
    result = runner.invoke(app, ["validate", "--config", str(cfg)])
    assert result.exit_code == 0, result.stdout
    assert "Config OK" in result.stdout
    assert "template" in result.stdout
    assert "DeprecationWarning" in result.stdout


def test_validate_v4_yaml_uses_scaffold_from(tmp_path: Path) -> None:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("p", encoding="utf-8")
    (tmp_path / "outline" / "world.md").write_text("w", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
    body = """
novel:
  title: "T"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  scaffold_from: "long-epic"
  stages:
    - id: write
      model: m
      prompt: p
      output: o
"""
    cfg = tmp_path / "novel-project.yaml"
    cfg.write_text(body, encoding="utf-8")
    result = runner.invoke(app, ["validate", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "scaffold_from" in result.stdout
    # No deprecation warning for v4 yaml
    assert "DeprecationWarning" not in result.stdout


def test_validate_split_missing_field_reports(tmp_path: Path) -> None:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("p", encoding="utf-8")
    (tmp_path / "outline" / "world.md").write_text("w", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
    body = """
novel:
  title: "T"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: "x"
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: write
      model: m
      prompt: p
      output: "output/c-{{num:03d}}.md"
"""
    cfg = tmp_path / "novel-project.yaml"
    cfg.write_text(body, encoding="utf-8")
    result = runner.invoke(app, ["validate", "--config", str(cfg)])
    assert result.exit_code == 2
    combined = ((result.stdout or "") + (result.stderr or "")).lower()
    assert "validation failed" in combined


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def test_init_long_epic_scaffolds_yaml_and_prompts(tmp_path: Path) -> None:
    target = tmp_path / "project"
    result = runner.invoke(
        app, ["init", "--template", "long-epic", "--dir", str(target)]
    )
    assert result.exit_code == 0, result.stdout
    assert (target / "novel-project.yaml").exists()
    prompts_dir = target / "prompts"
    assert prompts_dir.exists()
    # 10 stage prompts should be materialised
    prompt_files = sorted(prompts_dir.glob("*.md"))
    assert len(prompt_files) == 10
    # scaffold_from is in the yaml
    yaml_text = (target / "novel-project.yaml").read_text(encoding="utf-8")
    assert "scaffold_from: long-epic" in yaml_text


def test_init_then_validate_without_seeds(tmp_path: Path) -> None:
    """A13 / spec §4.1: ``init`` followed by ``validate`` must succeed
    even when the user has not yet supplied outline/ + CLAUDE.md.  The
    engine surfaces missing files at the first {{include:}} stage.
    """

    target = tmp_path / "project"
    init_result = runner.invoke(
        app, ["init", "--template", "long-epic", "--dir", str(target)]
    )
    assert init_result.exit_code == 0, init_result.stdout
    cfg = target / "novel-project.yaml"
    validate_result = runner.invoke(app, ["validate", "--config", str(cfg)])
    assert validate_result.exit_code == 0, (
        validate_result.stdout + validate_result.stderr
    )
    assert "Config OK" in validate_result.stdout


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / "novel-project.yaml").write_text("novel: {}", encoding="utf-8")
    result = runner.invoke(
        app, ["init", "--template", "long-epic", "--dir", str(target)]
    )
    assert result.exit_code == 2
    combined = (result.stdout or "") + (result.stderr or "")
    assert "Refusing to overwrite" in combined or "Refusing" in combined


def test_init_force_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / "novel-project.yaml").write_text("novel: {}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "init",
            "--template",
            "long-epic",
            "--dir",
            str(target),
            "--force",
        ],
    )
    assert result.exit_code == 0, result.stdout


def test_init_unknown_template_errors(tmp_path: Path) -> None:
    target = tmp_path / "project"
    result = runner.invoke(
        app, ["init", "--template", "bogus", "--dir", str(target)]
    )
    assert result.exit_code == 2
    combined = ((result.stdout or "") + (result.stderr or "")).lower()
    assert "unknown template" in combined


# --------------------------------------------------------------------------- #
# migrate
# --------------------------------------------------------------------------- #


def _write_legacy(tmp_path: Path) -> Path:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("p", encoding="utf-8")
    (tmp_path / "outline" / "world.md").write_text("w", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
    body = """
novel:
  title: "T"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [200, 400]
  style: "lean"
  seeds: [outline/premise.md, outline/world.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
execution:
  max_review_iterations: 5
"""
    cfg = tmp_path / "novel-project.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_migrate_dry_run_to_stdout(tmp_path: Path) -> None:
    cfg = _write_legacy(tmp_path)
    result = runner.invoke(app, ["migrate", "--config", str(cfg)])
    assert result.exit_code == 0, result.stdout
    # The output is the v4 yaml payload
    assert "scaffold_from: long-epic" in result.stdout
    assert "stages:" in result.stdout
    # The source file is untouched
    assert "template: \"long-epic\"" in cfg.read_text(encoding="utf-8")


def test_migrate_with_out_writes_to_new_file(tmp_path: Path) -> None:
    cfg = _write_legacy(tmp_path)
    out = tmp_path / "migrated.yaml"
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--out", str(out)]
    )
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    # prompts/ directory was also materialised
    assert (tmp_path / "prompts").exists()
    # Source yaml was NOT overwritten
    raw = cfg.read_text(encoding="utf-8")
    assert "template: \"long-epic\"" in raw
    # The migrated yaml has the v4 shape
    migrated = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert "stages" in migrated["pipeline"]
    assert migrated["pipeline"]["scaffold_from"] == "long-epic"
    # user-defined execution fields preserved
    assert migrated["execution"]["max_review_iterations"] == 5


def test_migrate_write_in_place_with_backup(tmp_path: Path) -> None:
    cfg = _write_legacy(tmp_path)
    result = runner.invoke(
        app, ["migrate", "--config", str(cfg), "--write"]
    )
    assert result.exit_code == 0, result.stdout
    bak = cfg.with_suffix(cfg.suffix + ".bak")
    assert bak.exists()
    # The yaml was overwritten with the v4 form
    raw = cfg.read_text(encoding="utf-8")
    assert "scaffold_from" in raw
    assert "stages:" in raw
    # The .bak file is the old content
    assert "template:" in bak.read_text(encoding="utf-8")


def test_migrate_out_and_write_are_mutually_exclusive(tmp_path: Path) -> None:
    cfg = _write_legacy(tmp_path)
    out = tmp_path / "new.yaml"
    result = runner.invoke(
        app,
        [
            "migrate",
            "--config",
            str(cfg),
            "--out",
            str(out),
            "--write",
        ],
    )
    assert result.exit_code == 2
    combined = ((result.stdout or "") + (result.stderr or "")).lower()
    assert "mutually exclusive" in combined
    # No files were created/modified
    assert not out.exists()
    assert not (tmp_path / "prompts").exists()


def test_migrate_missing_file_errors(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["migrate", "--config", str(tmp_path / "nope.yaml")]
    )
    assert result.exit_code == 2


# --------------------------------------------------------------------------- #
# Stage classes still importable (T15: deprecation path)
# ------------------------------------------------------------------------── #


def test_legacy_stage_classes_still_importable() -> None:
    import warnings

    from novelforge.stages import WriteChapterStage  # noqa: F401

    with warnings.catch_warnings():
        # We expect a DeprecationWarning here; record it.
        warnings.simplefilter("ignore", DeprecationWarning)
        from novelforge.stages import GenerateOutlineStage  # noqa: F401
