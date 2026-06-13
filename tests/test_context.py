"""M4 tests: context manager slicing + budget + trimming."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from novelforge.claude.context import (
    AssembledContext,
    ContextAssembler,
    ContextSlice,
    estimate_tokens,
)
from novelforge.config import ContextSpec, NovelSpec
from novelforge.errors import ContextOverflow


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A minimal project tree with seeds, constraints, and a few outputs."""

    # seeds & constraints
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text(
        "## Premise\n\nA young cultivator learns memory has a price.\n",
        encoding="utf-8",
    )
    (tmp_path / "outline" / "world.md").write_text(
        "## World\n\nThree realms above a dying ocean.\n", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text(
        "# Rules\n\n- Third person\n- No modern slang\n", encoding="utf-8"
    )

    # output dirs
    (tmp_path / "output" / "summaries").mkdir(parents=True)
    (tmp_path / "output" / "meta").mkdir(parents=True)
    (tmp_path / "output" / "chapters").mkdir(parents=True)
    (tmp_path / "output" / "review").mkdir(parents=True)

    # outline & tracking
    (tmp_path / "output" / "summaries" / "plot.md").write_text(
        "Plot: a hero rises and falls.\n", encoding="utf-8"
    )
    tracking = []
    for i in range(1, 21):
        tracking.append(f"## {i:03d} - beat {i}\nA chapter happens.\n")
    (tmp_path / "output" / "summaries" / "outline-tracking.md").write_text(
        "".join(tracking), encoding="utf-8"
    )
    (tmp_path / "output" / "summaries" / "foreshadowing.md").write_text(
        "- F1: Hero hides the manual\n", encoding="utf-8"
    )

    # character dossiers
    (tmp_path / "output" / "meta" / "hero.md").write_text(
        "# Hero\nLoyal, mournful.\n", encoding="utf-8"
    )
    (tmp_path / "output" / "meta" / "mentor.md").write_text(
        "# Mentor\nCynical, kind.\n", encoding="utf-8"
    )

    # three history chapters
    for i in range(1, 4):
        (tmp_path / "output" / "chapters" / f"00{i}-setup.md").write_text(
            f"# Chapter {i}\n\nIt was a stormy night in the lower realm. " * 50 + "\n",
            encoding="utf-8",
        )
    return tmp_path


@pytest.fixture()
def novel_spec() -> NovelSpec:
    return NovelSpec(
        title="X",
        genre="X",
        target_chapters=5,
        words_per_chapter=(800, 1500),
        style="x",
        seeds=("outline/premise.md", "outline/world.md"),
        constraints=("CLAUDE.md",),
    )


@pytest.fixture()
def small_context() -> ContextSpec:
    """A small budget so the trimming path is easy to trigger."""

    return ContextSpec(
        total=4000,
        context_reserve=1500,
        output_reserve=500,
        rolling_window=3,
        outline_range=10,
    )


# --------------------------------------------------------------------------- #
# estimate_tokens
# --------------------------------------------------------------------------- #


def test_estimate_tokens_cjk_word_mix() -> None:
    text = "Hello world 这是一个测试"
    est = estimate_tokens(text)
    # We can't pin an exact number, but the estimate must be > 0 and reasonable.
    assert est > 5
    assert est < 50


def test_estimate_tokens_empty() -> None:
    assert estimate_tokens("") == 0


# --------------------------------------------------------------------------- #
# always_loaded
# --------------------------------------------------------------------------- #


def test_assemble_includes_always_loaded_for_every_stage(
    project: Path, novel_spec: NovelSpec, small_context: ContextSpec
) -> None:
    asm = ContextAssembler(project, novel_spec, small_context)
    for stage in (
        "generate_outline",
        "review_outline",
        "write_chapter",
        "review_chapter",
        "final_polish",
    ):
        ctx = asm.assemble(stage, chapter_index=5)
        names = {s.name for s in ctx.slices}
        assert any(n.startswith("seed:") for n in names), f"missing seeds for {stage}"
        assert any(n.startswith("constraint:") for n in names), f"missing constraints for {stage}"


def test_assemble_outline_stage_loads_outline(
    project: Path, novel_spec: NovelSpec, small_context: ContextSpec
) -> None:
    asm = ContextAssembler(project, novel_spec, small_context)
    ctx = asm.assemble("generate_outline")
    names = {s.name for s in ctx.slices}
    assert any(n.startswith("outline:") for n in names)


# --------------------------------------------------------------------------- #
# write_chapter window
# --------------------------------------------------------------------------- #


def test_assemble_chapter_window_loads_recent_and_outline_range(
    project: Path, novel_spec: NovelSpec, small_context: ContextSpec
) -> None:
    asm = ContextAssembler(project, novel_spec, small_context)
    ctx = asm.assemble("write_chapter", chapter_index=10)
    names = {s.name for s in ctx.slices}
    assert any(n.startswith("history:") for n in names)
    assert any(n.startswith("outline_window:") for n in names)
    assert any(n.startswith("character:") for n in names)
    # Foreshadowing is small, should also be there.
    assert "foreshadowing" in names


def test_assemble_chapter_window_omits_when_no_outputs(
    tmp_path: Path, novel_spec: NovelSpec
) -> None:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("p", encoding="utf-8")
    (tmp_path / "outline" / "world.md").write_text("w", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
    spec = ContextSpec(
        total=10000, context_reserve=8000, output_reserve=1000,
        rolling_window=3, outline_range=10,
    )
    asm = ContextAssembler(tmp_path, novel_spec, spec)
    ctx = asm.assemble("write_chapter", chapter_index=1)
    # No chapter files exist → no history slices.
    assert not any(s.name.startswith("history:") for s in ctx.slices)


# --------------------------------------------------------------------------- #
# Budget enforcement & trimming
# --------------------------------------------------------------------------- #


def test_assemble_logs_trim_when_budget_exceeded(
    project: Path,
    novel_spec: NovelSpec,
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = ContextSpec(
        total=4000,
        context_reserve=500,  # small enough to force trimming
        output_reserve=200,
        rolling_window=3,
        outline_range=10,
    )
    asm = ContextAssembler(project, novel_spec, spec)
    with caplog.at_level(logging.INFO, logger="novelforge.claude.context"):
        ctx = asm.assemble("write_chapter", chapter_index=10)
    assert any(s.trimmed for s in ctx.slices), "no slice was trimmed"
    # The trim message should be in the log.
    assert any("trimmed" in record.message for record in caplog.records)


def test_assemble_keeps_always_loaded_under_pressure(
    project: Path, novel_spec: NovelSpec
) -> None:
    spec = ContextSpec(
        total=4000,
        context_reserve=500,  # smaller than the raw payload but enough for trim
        output_reserve=200,
        rolling_window=3,
        outline_range=10,
    )
    asm = ContextAssembler(project, novel_spec, spec)
    ctx = asm.assemble("write_chapter", chapter_index=10)
    # every always_loaded slice must remain present (content non-empty)
    for s in ctx.slices:
        if s.name.startswith("seed:") or s.name.startswith("constraint:"):
            assert s.content != "", f"always_loaded {s.name} was emptied!"


def test_assemble_raises_context_overflow_if_trim_insufficient(
    project: Path, novel_spec: NovelSpec
) -> None:
    spec = ContextSpec(
        total=4000,
        context_reserve=10,  # impossibly small
        output_reserve=200,
        rolling_window=3,
        outline_range=10,
    )
    asm = ContextAssembler(project, novel_spec, spec)
    with pytest.raises(ContextOverflow):
        asm.assemble("write_chapter", chapter_index=10)


# --------------------------------------------------------------------------- #
# review stage targets
# --------------------------------------------------------------------------- #


def test_assemble_review_stage_includes_target_file(
    project: Path, novel_spec: NovelSpec, small_context: ContextSpec
) -> None:
    asm = ContextAssembler(project, novel_spec, small_context)
    target = project / "output" / "summaries" / "plot.md"
    ctx = asm.assemble("review_outline", review_target_path=target)
    names = {s.name for s in ctx.slices}
    assert any(n.startswith("review_target:") for n in names)


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def test_render_includes_all_slices_and_task_instructions(
    project: Path, novel_spec: NovelSpec, small_context: ContextSpec
) -> None:
    asm = ContextAssembler(project, novel_spec, small_context)
    ctx = asm.assemble("generate_outline")
    ctx.prompt_template = "Please generate an outline."
    rendered = ctx.render()
    assert "Task instructions" in rendered
    assert "Please generate an outline." in rendered
    for s in ctx.slices:
        if s.content:
            assert s.name.split(":", 1)[-1] in rendered or s.name in rendered


def test_to_dict_includes_trim_log(
    project: Path, novel_spec: NovelSpec
) -> None:
    spec = ContextSpec(
        total=4000,
        context_reserve=500,
        output_reserve=200,
        rolling_window=3,
        outline_range=10,
    )
    asm = ContextAssembler(project, novel_spec, spec)
    ctx = asm.assemble("write_chapter", chapter_index=10)
    blob = ctx.to_dict()
    assert blob["stage"] == "write_chapter"
    assert isinstance(blob["slices"], list)
    # at least one trimmed slice
    assert any(s["trimmed"] for s in blob["slices"])
