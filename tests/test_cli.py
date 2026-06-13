"""M0 tests: CLI scaffold registration."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from novelforge.cli import app


runner = CliRunner()


def test_help_lists_all_subcommands() -> None:
    """`novelforge --help` should describe run/resume/status/validate."""

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.stdout
    for cmd in ("run", "resume", "status", "validate"):
        assert cmd in result.stdout, f"missing subcommand {cmd!r} in --help output"


def test_version_flag_prints_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert re.match(r"^novelforge \d+\.\d+\.\d+", result.stdout.strip())


def test_each_subcommand_is_registered() -> None:
    """Each subcommand's --help should be reachable."""

    for cmd in ("run", "resume", "status", "validate"):
        result = runner.invoke(app, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help failed: {result.stdout!r}"


def test_validate_subcommand_present() -> None:
    result = runner.invoke(app, ["validate", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.stdout


def test_run_subcommand_accepts_overrides() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    for flag in ("--config", "--max-chapters", "--skip-polish", "--use-mock"):
        assert flag in result.stdout, f"missing {flag} on `run --help`"


def test_resume_subcommand_accepts_force_stage() -> None:
    result = runner.invoke(app, ["resume", "--help"])
    assert result.exit_code == 0
    assert "--force-stage" in result.stdout
