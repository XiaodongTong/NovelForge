"""Tests for ``novelforge init`` scaffolding (PR-1).

Covers spec §AC-1, AC-2, AC-3, AC-4, AC-5, AC-6, AC-12, AC-15
(see ``.cybervisor/artifacts/spec.md``).  PR-2 concerns (split mode,
new ``produces`` paths) live in ``test_templates.py`` /
``test_orchestrator.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novelforge import cli
from novelforge import templates as _templates


runner = CliRunner()


# --------------------------------------------------------------------------- #
# AC-1 / AC-3: complete scaffold (long-epic)
# --------------------------------------------------------------------------- #


LONG_EPIC_PROMPT_COUNT = 5  # 5 stages in long-epic template


def test_init_creates_yaml_prompts_seeds_and_dirs(tmp_path: Path) -> None:
    """AC-1 + AC-3: long-epic scaffold produces 1 yaml + 5 prompts +
    1 CLAUDE.md + 2 outline/seed files + 6 gitkeep markers."""

    project = tmp_path / "fresh"
    result = runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    assert result.exit_code == 0, result.output

    # 1 yaml
    yaml_path = project / "novel-project.yaml"
    assert yaml_path.exists()
    raw = yaml.safe_load(yaml_path.read_text())
    assert "pipeline" in raw
    assert "stages" in raw["pipeline"]

    # 5 prompts (long-epic has 5 stages)
    prompt_files = sorted((project / "prompts").glob("*.md"))
    assert len(prompt_files) == LONG_EPIC_PROMPT_COUNT

    # 1 CLAUDE.md (non-empty — AC-3)
    claude_path = project / "CLAUDE.md"
    assert claude_path.exists()
    assert claude_path.read_text(encoding="utf-8").strip(), (
        "CLAUDE.md must not be empty (AC-3)"
    )

    # 2 outline seeds (non-empty — AC-3)
    for seed in ("outline/premise.md", "outline/world.md"):
        path = project / seed
        assert path.exists(), f"missing seed {seed}"
        assert path.read_text(encoding="utf-8").strip(), (
            f"seed {seed} must not be empty (AC-3)"
        )

    # 6 runtime dirs with .gitkeep
    for rel in cli.RUNTIME_DIRS:
        keep = project / rel / ".gitkeep"
        assert keep.exists(), f"missing .gitkeep under {rel}/"


def test_init_creates_seed_files_non_empty(tmp_path: Path) -> None:
    """AC-3: every seed file ships with sectioned placeholder content
    so ``validate`` does not immediately complain about emptiness."""

    project = tmp_path / "fresh"
    runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    claude_body = (project / "CLAUDE.md").read_text(encoding="utf-8")
    # Section headings must appear so the user knows where to fill.
    assert "目录结构" in claude_body
    assert "写作约束" in claude_body

    premise = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    assert "Premise" in premise
    world = (project / "outline" / "world.md").read_text(encoding="utf-8")
    assert "World" in world


def test_init_short_story_prompt_count_is_two(tmp_path: Path) -> None:
    """AC-1 caveat: ``short-story`` template has 2 stages → 2 prompts."""

    project = tmp_path / "fresh"
    result = runner.invoke(
        cli.app,
        ["init", "--template", "short-story", "--dir", str(project)],
    )
    assert result.exit_code == 0, result.output
    prompt_files = sorted((project / "prompts").glob("*.md"))
    assert len(prompt_files) == 2


# --------------------------------------------------------------------------- #
# AC-5: seed skip / --force
# --------------------------------------------------------------------------- #


def test_init_seed_skip_if_exists(tmp_path: Path) -> None:
    """Pre-existing seed files are preserved when re-running init.

    Mechanism: the user removes the generated yaml between the two
    runs so init doesn't refuse at the yaml step; the seed step then
    sees an existing premise.md and skips it (no --force).  This is
    exactly the AC-5 contract.
    """

    project = tmp_path / "fresh"
    runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    marker = "USER_CUSTOM_CONTENT_42"
    (project / "outline" / "premise.md").write_text(marker + "\n", encoding="utf-8")
    # Remove the yaml that the first init created so the second run
    # does not refuse at the strict yaml step (AC-5 last paragraph).
    (project / "novel-project.yaml").unlink()

    result = runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    assert result.exit_code == 0, result.output
    body = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    assert marker in body, "seed must be preserved on re-run without --force"


def test_init_seed_force_overwrites_with_warning(tmp_path: Path) -> None:
    """AC-5: --force overwrites seed files and emits a WARN line."""

    project = tmp_path / "fresh"
    runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    marker = "USER_CUSTOM_CONTENT_42"
    (project / "outline" / "premise.md").write_text(marker + "\n", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "init",
            "--template",
            "long-epic",
            "--dir",
            str(project),
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    body = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    assert marker not in body, "seed must be overwritten under --force"
    # WARN line is printed on the stderr console (which Typer merges into
    # ``output`` for the runner).  The marker on the message itself is
    # sufficient — the user can grep it.
    assert "WARN" in result.output
    assert "premise.md" in result.output


def test_init_yaml_refuses_without_force(tmp_path: Path) -> None:
    """``novel-project.yaml`` is not a seed; it still uses the strict
    refuse-without-force policy (AC-5 last paragraph)."""

    project = tmp_path / "fresh"
    project.mkdir(parents=True, exist_ok=True)
    (project / "novel-project.yaml").write_text("old: true\n", encoding="utf-8")
    result = runner.invoke(
        cli.app,
        ["init", "--template", "short-story", "--dir", str(project)],
    )
    assert result.exit_code == 2
    assert "Refusing to overwrite" in result.output


# --------------------------------------------------------------------------- #
# AC-4 / AC-15: --skeleton-only
# --------------------------------------------------------------------------- #


def test_init_skeleton_only_skips_seeds(tmp_path: Path) -> None:
    """AC-4: ``--skeleton-only`` writes yaml + prompts + dirs but no
    ``outline/`` or ``CLAUDE.md``."""

    project = tmp_path / "fresh"
    result = runner.invoke(
        cli.app,
        [
            "init",
            "--template",
            "long-epic",
            "--dir",
            str(project),
            "--skeleton-only",
        ],
    )
    assert result.exit_code == 0, result.output

    assert (project / "novel-project.yaml").exists()
    assert sorted((project / "prompts").glob("*.md")) == sorted(
        (project / "prompts").glob("*.md")
    )
    assert len(sorted((project / "prompts").glob("*.md"))) == LONG_EPIC_PROMPT_COUNT

    # outline/ + CLAUDE.md are skipped.
    assert not (project / "outline").exists()
    assert not (project / "CLAUDE.md").exists()

    # runtime dirs + gitkeep still present.
    for rel in cli.RUNTIME_DIRS:
        assert (project / rel / ".gitkeep").exists()


def test_init_skeleton_only_with_force(tmp_path: Path) -> None:
    """AC-15: ``--skeleton-only --force`` overwrites yaml + prompts +
    refreshes gitkeep, but does not write the seed files (even if
    outline/ already exists from a prior non-skeleton run)."""

    project = tmp_path / "fresh"
    runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )

    # Now mangle the yaml + the gitkeep + the seed contents and re-run
    # with --skeleton-only --force.  Per AC-15 the seeds must be
    # untouched (skeleton-only branch is skipped) and the yaml +
    # gitkeep are force-overwritten.
    (project / "novel-project.yaml").write_text("old_yaml\n", encoding="utf-8")
    (project / "characters" / ".gitkeep").write_text("STALE_MARKER\n", encoding="utf-8")
    premise_before = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    claude_before = (project / "CLAUDE.md").read_text(encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "init",
            "--template",
            "long-epic",
            "--dir",
            str(project),
            "--skeleton-only",
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output

    # yaml replaced
    raw = yaml.safe_load((project / "novel-project.yaml").read_text())
    assert "pipeline" in raw

    # gitkeep refreshed (overwritten under --force)
    assert (
        (project / "characters" / ".gitkeep").read_text(encoding="utf-8") == ""
    )

    # Seed files NOT touched (skeleton-only branch was skipped).
    premise_after = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    claude_after = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert premise_after == premise_before
    assert claude_after == claude_before

    # WARN for overwrite visible in combined output.
    assert "WARN" in result.output


# --------------------------------------------------------------------------- #
# AC-6: idempotency
# --------------------------------------------------------------------------- #


def test_init_idempotent_preserves_user_content(tmp_path: Path) -> None:
    """AC-6: running init three times preserves any user-edited seed
    file.

    First init creates yaml + seeds + dirs.  Re-runs against the
    scaffolded project refuse on the yaml step (AC-5 strict policy),
    so the user's premise.md is never touched.  The test asserts the
    yaml-refuse happens **without** the seed step modifying the file.
    """

    project = tmp_path / "fresh"
    runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )

    marker = "KEEP_ME_FOREVER"
    (project / "outline" / "premise.md").write_text(marker + "\n", encoding="utf-8")

    for _ in range(2):
        result = runner.invoke(
            cli.app,
            ["init", "--template", "long-epic", "--dir", str(project)],
        )
        # Without --force, init refuses on the existing yaml (AC-5).
        assert result.exit_code == 2

    body = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    assert marker in body, "yaml refusal must not touch user seeds"


def test_init_repeat_without_force_refuses_on_yaml(tmp_path: Path) -> None:
    """AC-5 + AC-6 corollary: when the yaml already exists, a re-run
    without --force refuses (exit 2) but never overwrites any
    previously-written seed (the refusal happens before the seed step).
    This is the "no surprise overwrite" half of idempotency."""

    project = tmp_path / "fresh"
    runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )

    marker = "KEEP_ME"
    (project / "outline" / "premise.md").write_text(marker + "\n", encoding="utf-8")

    result = runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    assert result.exit_code == 2
    body = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    assert marker in body, "yaml refusal must not touch user seeds"


def test_init_force_refresh_then_seed_skip(tmp_path: Path) -> None:
    """After ``init --force``, subsequent ``init`` runs (without
    --force) refuse on the yaml but never overwrite seeds the user
    has hand-edited between calls."""

    project = tmp_path / "fresh"
    runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )

    marker = "I_EDITED_THIS"
    (project / "outline" / "premise.md").write_text(marker + "\n", encoding="utf-8")

    # Drop the yaml so init has work to do.
    (project / "novel-project.yaml").unlink()

    # Run init once more — must succeed (yaml was removed) and must
    # NOT touch premise.md (seed-skip).
    result = runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    assert result.exit_code == 0, result.output
    body = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    assert marker in body, "seed-skip must preserve user edits"

    # A second re-run (without --force) now sees the fresh yaml and
    # refuses — but the premise.md marker is still preserved.
    result2 = runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    assert result2.exit_code == 2
    body = (project / "outline" / "premise.md").read_text(encoding="utf-8")
    assert marker in body


# --------------------------------------------------------------------------- #
# AC-12: next-steps hint
# --------------------------------------------------------------------------- #


def test_init_prints_concrete_next_steps(tmp_path: Path) -> None:
    """AC-12: the post-init message names specific files."""

    project = tmp_path / "fresh"
    result = runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    assert result.exit_code == 0, result.output
    # The hint must reference the actual files the user must edit.
    assert "outline/premise.md" in result.output
    assert "outline/world.md" in result.output
    assert "CLAUDE.md" in result.output


def test_init_skeleton_only_hints_say_no_seeds(tmp_path: Path) -> None:
    """AC-15 corollary: skeleton-only message names the missing seeds."""

    project = tmp_path / "fresh"
    result = runner.invoke(
        cli.app,
        [
            "init",
            "--template",
            "long-epic",
            "--dir",
            str(project),
            "--skeleton-only",
        ],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    assert "skeleton-only" in output.lower() or "--skeleton-only" in output
    assert "outline/premise.md" in output


# --------------------------------------------------------------------------- #
# Helper coverage (smoke)
# --------------------------------------------------------------------------- #


def test_write_seed_skips_existing(tmp_path: Path) -> None:
    target = tmp_path / "seed.md"
    target.write_text("keep\n", encoding="utf-8")
    wrote = cli._write_seed(target, "new\n", force=False)
    assert wrote is False
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_write_seed_writes_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "seed.md"
    wrote = cli._write_seed(target, "fresh\n", force=False)
    assert wrote is True
    assert target.read_text(encoding="utf-8") == "fresh\n"


def test_ensure_gitkeep_creates_dir_and_marker(tmp_path: Path) -> None:
    d = tmp_path / "newdir"
    cli._ensure_gitkeep(d, force=False)
    assert d.is_dir()
    assert (d / ".gitkeep").exists()


def test_ensure_gitkeep_idempotent(tmp_path: Path) -> None:
    d = tmp_path / "newdir"
    cli._ensure_gitkeep(d, force=False)
    cli._ensure_gitkeep(d, force=False)
    assert (d / ".gitkeep").exists()


# --------------------------------------------------------------------------- #
# Template contract: CLAUDE_TEMPLATE must match the directory tree
# --------------------------------------------------------------------------- #


def test_claude_template_lists_all_runtime_dirs() -> None:
    """AC-13: CLAUDE.md scaffold explicitly enumerates every runtime
    dir so the user knows which tree they're editing."""

    body = _templates.CLAUDE_TEMPLATE
    # Every runtime dir's leaf name appears under the output/ tree
    # (the template renders `output/summaries` as `├── summaries/`).
    leaf_map = {
        "characters": "characters",
        "chapters-outline": "chapters-outline",
        "output/summaries": "summaries",
        "output/meta": "meta",
        "output/chapters": "chapters",
        "output/review": "review",
    }
    for rel, leaf in leaf_map.items():
        assert leaf in body, f"runtime dir {rel!r} (leaf {leaf!r}) missing"
    # Top-level 'output' parent is also explicit.
    assert "output/" in body or "├── output/" in body


def test_claude_template_marks_user_owned_seeds() -> None:
    """AC-13: each seed file is labelled 用户填 or 引擎填 so the
    user-engine boundary is unambiguous."""

    body = _templates.CLAUDE_TEMPLATE
    assert "outline" in body
    assert "premise.md" in body
    assert "world.md" in body
    # At least one 用户填 marker near the outline/ tree.
    assert "用户填" in body
    assert "引擎填" in body


# --------------------------------------------------------------------------- #
# Optional: validate-after-init smoke (AC-2)
# --------------------------------------------------------------------------- #


def test_init_followed_by_validate_exits_zero(tmp_path: Path) -> None:
    """AC-2: ``validate`` after init exits 0 even though the seeds are
    placeholder text (validate only checks existence)."""

    project = tmp_path / "fresh"
    runner.invoke(
        cli.app,
        ["init", "--template", "long-epic", "--dir", str(project)],
    )
    result = runner.invoke(
        cli.app,
        ["validate", "--config", str(project / "novel-project.yaml")],
    )
    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- #
# Help-text surface
# --------------------------------------------------------------------------- #


def test_init_help_advertises_skeleton_only(tmp_path: Path) -> None:
    """``init --help`` must mention the new flag so users discover it."""

    result = runner.invoke(cli.app, ["init", "--help"])
    assert result.exit_code == 0
    assert "--skeleton-only" in result.output
