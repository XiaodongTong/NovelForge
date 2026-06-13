"""v4 end-to-end test — migrate the bundled sample, then run it.

This is the spec A2 / A13 / A14 happy path: ``init`` produces a runnable
project, ``migrate`` upgrades the v3 sample, and the engine still
produces the same category of outputs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novelforge.cli import app


runner = CliRunner()
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples" / "minimal-novel"


def _clean(project_root: Path) -> None:
    for d in (".novelforge", "output", "prompts"):
        target = project_root / d
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()


def _load_mock_adapter(orch) -> None:
    """Wire a MockClaudeAdapter with v4-friendly responses."""

    from novelforge.claude.adapter import MockClaudeAdapter, MockResponse

    mock = orch._build_adapter()  # type: ignore[assignment]

    mock.set_response(
        "generate_outline",
        MockResponse(
            output=(
                "## Plot\n\nA young cultivator discovers a forbidden manual.\n\n"
                "## Chapter 1 - The Discovery\n"
                "The cultivator finds the manual in a collapsed temple.\n"
            ),
            input_tokens=50,
            output_tokens=80,
        ),
    )
    approved_output = json.dumps(
        {"passed": True, "findings": [], "summary": "ok"}
    )
    approved_parsed = {"passed": True, "findings": []}
    for stage in (
        "review_outline",
        "review_characters",
        "review_simulation",
        "review_chapter",
    ):
        mock.set_response(
            stage,
            MockResponse(
                output=approved_output,
                parsed=approved_parsed,
                input_tokens=10,
                output_tokens=10,
            ),
        )
    mock.set_response(
        "design_characters",
        MockResponse(
            output="# Wei Lin\nA young cultivator.\n\n# Master Chen\nA mentor.",
            input_tokens=20,
            output_tokens=30,
        ),
    )
    mock.set_response(
        "simulate_plot",
        MockResponse(
            output="# Plot Simulation\n\nEscalation curve looks solid.",
            input_tokens=20,
            output_tokens=30,
        ),
    )
    mock.set_response(
        "write_chapter",
        MockResponse(
            output=(
                "# Chapter 1 - The Discovery\n\n"
                "风起苍岚，少年魏林独坐山巅，目光穿透云雾。\n"
                + ("那手札上的字迹时隐时现。" * 80)
                + "\n"
            ),
            input_tokens=60,
            output_tokens=200,
        ),
    )
    mock.set_response(
        "full_consistency_check",
        MockResponse(
            output="# Consistency Report\n\nNo issues found.",
            input_tokens=40,
            output_tokens=50,
        ),
    )
    mock.set_response(
        "final_polish",
        MockResponse(
            output="# Polish Notes\n\nMinor tweaks.",
            input_tokens=40,
            output_tokens=50,
        ),
    )
    orch._adapter = mock  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# A13: init --template long-epic produces a runnable project
# --------------------------------------------------------------------------- #


def test_init_then_run_long_epic_succeeds(tmp_path: Path) -> None:
    project_root = tmp_path / "init_then_run"
    # init the project
    result = runner.invoke(
        app, ["init", "--template", "long-epic", "--dir", str(project_root)]
    )
    assert result.exit_code == 0, result.stdout
    # supply the seeds the v4 yaml expects
    (project_root / "outline").mkdir()
    (project_root / "outline" / "premise.md").write_text("p", encoding="utf-8")
    (project_root / "outline" / "world.md").write_text("w", encoding="utf-8")
    (project_root / "CLAUDE.md").write_text("c", encoding="utf-8")

    # Patch the orchestrator to use a MockClaudeAdapter with stage-specific
    # responses that satisfy the v4 form requirements (write_chapter
    # needs a chapter heading so the split regex matches).
    from novelforge.orchestrator import Orchestrator

    cfg_path = project_root / "novel-project.yaml"
    from novelforge.config import load_config

    cfg = load_config(cfg_path)
    orch = Orchestrator(
        config=cfg, config_path=cfg_path,
        project_root=project_root, use_mock=True, skip_polish=True,
    )
    _load_mock_adapter(orch)
    summary = orch.run(fresh=True)
    assert summary["ok"], summary

    # Output files were created
    output_dir = project_root / "output"
    assert output_dir.exists()
    assert (output_dir / "summaries" / "plot.md").exists()
    assert list((output_dir / "chapters").glob("*.md"))
    # Review output (json)
    assert (output_dir / "review" / "chapter-review.json").exists()

    _clean(project_root)


# --------------------------------------------------------------------------- #
# A14: migrate --out produces a v4 yaml; --write in-place with backup
# --------------------------------------------------------------------------- #


def test_migrate_out_then_run_v4_succeeds(tmp_path: Path) -> None:
    project_root = tmp_path / "migrated"
    shutil.copytree(SAMPLE_DIR, project_root)
    _clean(project_root)

    cfg_path = project_root / "novel-project.yaml"
    out_path = project_root / "migrated.yaml"

    result = runner.invoke(
        app,
        [
            "migrate",
            "--config",
            str(cfg_path),
            "--out",
            str(out_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert out_path.exists()
    assert (project_root / "prompts").exists()

    # Run the migrated v4 yaml (mock).
    from novelforge.config import load_config
    from novelforge.orchestrator import Orchestrator

    cfg = load_config(out_path)
    orch = Orchestrator(
        config=cfg, config_path=out_path,
        project_root=project_root, use_mock=True, skip_polish=True,
    )
    _load_mock_adapter(orch)
    summary = orch.run(fresh=True)
    assert summary["ok"], summary

    # Output files exist
    output_dir = project_root / "output"
    assert (output_dir / "summaries" / "plot.md").exists()
    assert list((output_dir / "chapters").glob("*.md"))

    _clean(project_root)


def test_migrate_write_creates_backup(tmp_path: Path) -> None:
    project_root = tmp_path / "write_test"
    shutil.copytree(SAMPLE_DIR, project_root)
    _clean(project_root)

    cfg_path = project_root / "novel-project.yaml"
    bak_path = cfg_path.with_suffix(cfg_path.suffix + ".bak")
    assert not bak_path.exists()

    result = runner.invoke(
        app, ["migrate", "--config", str(cfg_path), "--write"]
    )
    assert result.exit_code == 0, result.stdout
    assert bak_path.exists()
    # Source file was overwritten with v4 form
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "stages" in raw["pipeline"]
    # The backup is the old v3 form
    bak_raw = yaml.safe_load(bak_path.read_text(encoding="utf-8"))
    assert "template" in bak_raw["pipeline"]

    _clean(project_root)
