"""Tests for the v4 ``PromptRenderer`` (placeholder families)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novelforge.claude.context import PromptRenderer
from novelforge.config import ContextSpec, NovelSpec
from novelforge.errors import ConfigError


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text(
        "## Premise\n\nA young cultivator.\n", encoding="utf-8"
    )
    (tmp_path / "outline" / "world.md").write_text(
        "## World\n\nThree realms.\n", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "# Rules\n\n- Third person\n", encoding="utf-8"
    )
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "001-setup.md").write_text(
        "It was dark.\n", encoding="utf-8"
    )
    (tmp_path / "chapters" / "002-orbit.md").write_text(
        "The choice.\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def novel_spec() -> NovelSpec:
    return NovelSpec(
        title="Test",
        genre="x",
        target_chapters=12,
        words_per_chapter=(800, 1500),
        style="lean",
        seeds=("outline/premise.md", "outline/world.md"),
        constraints=("CLAUDE.md",),
    )


@pytest.fixture()
def context_spec() -> ContextSpec:
    return ContextSpec(
        total=200_000,
        context_reserve=2_000,
        output_reserve=500,
        rolling_window=3,
        outline_range=5,
    )


# --------------------------------------------------------------------------- #
# {{novel.*}}
# --------------------------------------------------------------------------- #


def test_novel_placeholder_substitutes_attributes(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    out = renderer.render(
        "Title is {{novel.title}}; target={{novel.target_chapters}}",
    )
    assert "Title is Test" in out.text
    assert "target=12" in out.text
    assert out.novel_expansions == 2


def test_novel_placeholder_words_per_chapter(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    out = renderer.render(
        "min={{novel.words_per_chapter_min}} max={{novel.words_per_chapter_max}}",
    )
    assert "min=800" in out.text
    assert "max=1500" in out.text


def test_novel_placeholder_unknown_raises(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    with pytest.raises(ConfigError, match="unknown novel attribute"):
        renderer.render("{{novel.nope}}")


# --------------------------------------------------------------------------- #
# {{ctx.*}}
# --------------------------------------------------------------------------- #


def test_ctx_placeholder_substitutes(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    out = renderer.render(
        "stage={{ctx.stage_id}} batch={{ctx.batch}}",
        ctx={"stage_id": "write_chapter", "batch": "003"},
    )
    assert "stage=write_chapter" in out.text
    assert "batch=003" in out.text
    assert out.ctx_expansions == 2


def test_ctx_placeholder_unknown_raises(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    with pytest.raises(ConfigError, match="unknown ctx attribute"):
        renderer.render("{{ctx.nope}}", ctx={"stage_id": "x"})


# --------------------------------------------------------------------------- #
# {{include: ...}}
# --------------------------------------------------------------------------- #


def test_include_single_file(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    out = renderer.render("{{include: CLAUDE.md}}")
    assert "Third person" in out.text
    assert "CLAUDE.md" in out.include_files


def test_include_glob(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    out = renderer.render("{{include: chapters/*.md}}")
    # Files are recorded as project-root-relative paths.
    assert any(p.endswith("001-setup.md") for p in out.include_files)
    assert any(p.endswith("002-orbit.md") for p in out.include_files)
    assert "It was dark." in out.text
    assert "The choice." in out.text


def test_include_no_match_warns(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    out = renderer.render("{{include: chapters/missing-*.md}}")
    assert out.text == ""
    assert any("did not match" in w for w in out.warnings)


def test_include_exceeds_budget_warns(
    project_root: Path,
    novel_spec: NovelSpec,
) -> None:
    # Tiny budget so even a single chapter file overflows it.
    tight = ContextSpec(
        total=200,
        context_reserve=10,
        output_reserve=50,
        rolling_window=3,
        outline_range=5,
    )
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=tight,
    )
    out = renderer.render("{{include: chapters/*.md}}")
    assert any("exceed context budget" in w for w in out.warnings)


# --------------------------------------------------------------------------- #
# Idempotence
# --------------------------------------------------------------------------- #


def test_render_passthrough_when_no_placeholders(
    project_root: Path,
    novel_spec: NovelSpec,
    context_spec: ContextSpec,
) -> None:
    renderer = PromptRenderer(
        project_root=project_root,
        novel=novel_spec,
        context_spec=context_spec,
    )
    out = renderer.render("Just a plain prompt body.")
    assert out.text == "Just a plain prompt body."
    assert out.novel_expansions == 0
    assert out.ctx_expansions == 0
