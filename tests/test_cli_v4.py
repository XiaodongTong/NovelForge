"""Tests for the v4 CLI surface (Phase 3.7)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novelforge import cli


runner = CliRunner()


# --------------------------------------------------------------------------- #
# Command surface
# --------------------------------------------------------------------------- #


def test_no_migrate_command_registered() -> None:
    """The v3 ``migrate`` command must not exist."""

    names = {cmd.name or cmd.callback.__name__.replace("_", "-")
             for cmd in cli.app.registered_commands}
    # Some typer versions populate .name differently; fall back to the
    # callback name lookup.
    callbacks = [c.callback.__name__ for c in cli.app.registered_commands if c.callback]
    assert "migrate" not in names
    assert "migrate" not in callbacks


def test_no_v3_helpers_in_cli_source() -> None:
    """v3 helpers must be gone; comments and docstring mentions are OK."""

    src = (Path(__file__).resolve().parent.parent / "src" / "novelforge" / "cli.py").read_text()
    # Strip comments / docstrings (anything starting with # or between
    # triple quotes).  Crude but sufficient for this guard.
    import re
    cleaned = re.sub(r"#.*", "", src)
    cleaned = re.sub(r'""".*?"""', "", cleaned, flags=re.DOTALL)
    for forbidden in ("def migrate", "scaffold_from", "stages_override"):
        assert forbidden not in cleaned, (
            f"cli.py still references {forbidden!r} in code"
        )


# --------------------------------------------------------------------------- #
# init command
# --------------------------------------------------------------------------- #


def test_init_creates_yaml_and_prompts(tmp_path: Path) -> None:
    project = tmp_path / "fresh"
    result = runner.invoke(
        cli.app,
        ["init", "--template", "short-story", "--dir", str(project)],
    )
    assert result.exit_code == 0, result.output
    yaml_path = project / "novel-project.yaml"
    assert yaml_path.exists()
    raw = yaml.safe_load(yaml_path.read_text())
    assert "pipeline" in raw
    assert "stages" in raw["pipeline"]
    assert "template" not in raw["pipeline"]
    assert "scaffold_from" not in raw["pipeline"]
    # Every stage has the contract fields.
    for stage in raw["pipeline"]["stages"]:
        assert "produces" in stage
        assert "done_when" in stage
    # Prompts materialised.
    prompts_dir = project / "prompts"
    assert prompts_dir.is_dir()
    prompt_files = list(prompts_dir.glob("*.md"))
    assert prompt_files


def test_init_unknown_template_errors(tmp_path: Path) -> None:
    project = tmp_path / "fresh"
    result = runner.invoke(
        cli.app,
        ["init", "--template", "non-existent", "--dir", str(project)],
    )
    assert result.exit_code == 2
    assert "unknown template" in result.output.lower()


def test_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    project = tmp_path / "fresh"
    project.mkdir()
    (project / "novel-project.yaml").write_text("novel: x\n", encoding="utf-8")
    result = runner.invoke(
        cli.app,
        ["init", "--template", "short-story", "--dir", str(project)],
    )
    assert result.exit_code == 2
    assert "overwrite" in result.output.lower()


def test_init_force_overwrites(tmp_path: Path) -> None:
    project = tmp_path / "fresh"
    project.mkdir()
    (project / "novel-project.yaml").write_text("old\n", encoding="utf-8")
    result = runner.invoke(
        cli.app,
        ["init", "--template", "short-story", "--dir", str(project), "--force"],
    )
    assert result.exit_code == 0
    raw = yaml.safe_load((project / "novel-project.yaml").read_text())
    assert "pipeline" in raw


# --------------------------------------------------------------------------- #
# validate command
# --------------------------------------------------------------------------- #


_VALID_YAML = """
novel:
  title: T
  genre: T
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: x
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: a
      model: m
      prompt: A.
      produces:
        - path: output/a.md
          alias: a
"""


def test_validate_accepts_v4_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "novel-project.yaml"
    cfg.write_text(_VALID_YAML, encoding="utf-8")
    result = runner.invoke(cli.app, ["validate", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "Config OK" in result.output


def test_validate_rejects_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app, ["validate", "--config", str(tmp_path / "absent.yaml")]
    )
    assert result.exit_code == 2


def test_validate_rejects_v3_template_field(tmp_path: Path) -> None:
    cfg = tmp_path / "novel-project.yaml"
    cfg.write_text(
        "novel:\n  title: x\npipeline:\n  template: long-epic\n",
        encoding="utf-8",
    )
    result = runner.invoke(cli.app, ["validate", "--config", str(cfg)])
    assert result.exit_code == 2
