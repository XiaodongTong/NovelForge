"""Unit tests for the ContextAssembler + PromptRenderer (Phase 5 backfill).

Covers the contract model's context layer:

- :func:`estimate_tokens` — token heuristic
- :func:`expand_upstream_placeholders` — AC-4 placeholder family
- :class:`ContextAssembler` — consumes resolution, slices, budget
- :class:`PromptRenderer` — ``{{novel.*}}`` / ``{{ctx.*}}`` / ``{{include:}}``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from novelforge.artifact_registry import ArtifactRegistry
from novelforge.claude.context import (
    AssembledContext,
    ContextAssembler,
    ContextSlice,
    PromptRenderer,
    estimate_tokens,
    expand_upstream_placeholders,
)
from novelforge.config import (
    ContextSpec,
    ExecutionSpec,
    NovelProjectConfig,
    NovelSpec,
    PipelineSpec,
    RetrySpec,
)
from novelforge.errors import ConfigError, ContextOverflow


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _novel_spec(seeds: list[str], constraints: list[str]) -> NovelSpec:
    return NovelSpec(
        title="Test",
        genre="玄幻",
        target_chapters=10,
        words_per_chapter=(100, 200),
        style="demo",
        seeds=tuple(seeds),
        constraints=tuple(constraints),
    )


def _ctx_spec(reserve: int = 60_000) -> ContextSpec:
    return ContextSpec(total=200_000, context_reserve=reserve, output_reserve=12_000)


def _make_project(tmp_path: Path, seeds: list[str], constraints: list[str]) -> NovelProjectConfig:
    return NovelProjectConfig(
        project_path=tmp_path / "novel-project.yaml",
        novel=_novel_spec(seeds, constraints),
        pipeline=PipelineSpec(stages=()),
        execution=ExecutionSpec(
            batch_size={},
            max_review_iterations=1,
            review_model="",
            write_model="",
            context=_ctx_spec(),
            retry=RetrySpec(max_retries=3, backoff="constant", max_wait=10),
        ),
    )


# --------------------------------------------------------------------------- #
# estimate_tokens
# --------------------------------------------------------------------------- #


class TestEstimateTokens:
    def test_empty(self) -> None:
        assert estimate_tokens("") == 0

    def test_latin_text(self) -> None:
        # 3 words → ~3-4 tokens after safety margin.
        tokens = estimate_tokens("hello world foo")
        assert tokens >= 3
        assert tokens <= 6

    def test_cjk_text(self) -> None:
        # 3 CJK glyphs → ~3-4 tokens after safety margin.
        tokens = estimate_tokens("你好世界")
        assert tokens >= 4


# --------------------------------------------------------------------------- #
# expand_upstream_placeholders
# --------------------------------------------------------------------------- #


class TestExpandUpstream:
    def test_single_alias_to_content(self, tmp_path: Path) -> None:
        f = tmp_path / "outline.md"
        f.write_text("# Outline\nThe hero rises.", encoding="utf-8")
        reg = ArtifactRegistry()
        reg.register("generate", "outline", f)
        out = expand_upstream_placeholders(
            "see {{upstream.generate.outline}}", reg, stage_id="write"
        )
        assert "The hero rises." in out

    def test_single_alias_to_path(self, tmp_path: Path) -> None:
        f = tmp_path / "outline.md"
        f.write_text("x", encoding="utf-8")
        reg = ArtifactRegistry()
        reg.register("generate", "outline", f)
        out = expand_upstream_placeholders(
            "see {{upstream.generate.outline.path}}", reg, stage_id="write"
        )
        assert str(f) in out

    def test_batch_alias_to_multiline_content(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.md"
        f1.write_text("alpha", encoding="utf-8")
        f2 = tmp_path / "b.md"
        f2.write_text("beta", encoding="utf-8")
        reg = ArtifactRegistry()
        reg.register("write", "chapter", [f1, f2])
        out = expand_upstream_placeholders(
            "see {{upstream.write.chapter[*]}}", reg, stage_id="review"
        )
        assert "alpha" in out
        assert "beta" in out

    def test_batch_alias_to_path_list(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.md"
        f1.write_text("alpha", encoding="utf-8")
        f2 = tmp_path / "b.md"
        f2.write_text("beta", encoding="utf-8")
        reg = ArtifactRegistry()
        reg.register("write", "chapter", [f1, f2])
        out = expand_upstream_placeholders(
            "see {{upstream.write.chapter[*].path}}", reg, stage_id="review"
        )
        assert str(f1) in out
        assert str(f2) in out

    def test_single_alias_with_list_marker_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "outline.md"
        f.write_text("x", encoding="utf-8")
        reg = ArtifactRegistry()
        reg.register("generate", "outline", f)
        with pytest.raises(ConfigError, match=r"\[\*\] suffix is not allowed"):
            expand_upstream_placeholders(
                "see {{upstream.generate.outline[*]}}", reg, stage_id="write"
            )

    def test_batch_alias_without_list_marker_raises(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.md"
        f1.write_text("alpha", encoding="utf-8")
        reg = ArtifactRegistry()
        reg.register("write", "chapter", [f1])
        with pytest.raises(ConfigError, match=r"must use \[\*\] suffix"):
            expand_upstream_placeholders(
                "see {{upstream.write.chapter}}", reg, stage_id="review"
            )

    def test_unknown_upstream_raises(self, tmp_path: Path) -> None:
        reg = ArtifactRegistry()
        with pytest.raises(ConfigError, match="unknown upstream"):
            expand_upstream_placeholders(
                "see {{upstream.unknown.alias}}", reg, stage_id="x"
            )

    def test_no_placeholders_returns_text_unchanged(self) -> None:
        reg = ArtifactRegistry()
        out = expand_upstream_placeholders(
            "no placeholders here", reg, stage_id="x"
        )
        assert out == "no placeholders here"


# --------------------------------------------------------------------------- #
# ContextAssembler
# --------------------------------------------------------------------------- #


class TestContextAssembler:
    def test_assemble_with_default_consumes(self, tmp_path: Path) -> None:
        seed = tmp_path / "premise.md"
        seed.write_text("the world is broken", encoding="utf-8")
        cfg = _make_project(tmp_path, seeds=["premise.md"], constraints=[])
        reg = ArtifactRegistry()
        upstream_path = tmp_path / "outline.md"
        upstream_path.write_text("outline body", encoding="utf-8")
        reg.register("gen", "outline", upstream_path)

        asm = ContextAssembler(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=reg,
        )
        ctx = asm.assemble("write", consumes=None, executed_stages=["gen"])
        # Should include the seed slice + the upstream slice.
        names = [s.name for s in ctx.slices]
        assert any("seed" in n for n in names)
        assert any("upstream:gen" in n for n in names)

    def test_assemble_with_explicit_consumes(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        reg = ArtifactRegistry()
        a = tmp_path / "a.md"
        a.write_text("a", encoding="utf-8")
        b = tmp_path / "b.md"
        b.write_text("b", encoding="utf-8")
        reg.register("stage_a", "out", a)
        reg.register("stage_b", "out", b)

        asm = ContextAssembler(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=reg,
        )
        ctx = asm.assemble("c", consumes=["stage_a"], executed_stages=["stage_a", "stage_b"])
        names = [s.name for s in ctx.slices]
        assert any("stage_a" in n for n in names)
        assert not any("stage_b" in n for n in names)

    def test_assemble_with_empty_consumes(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        reg = ArtifactRegistry()
        a = tmp_path / "a.md"
        a.write_text("a", encoding="utf-8")
        reg.register("stage_a", "out", a)
        asm = ContextAssembler(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=reg,
        )
        ctx = asm.assemble("c", consumes=[], executed_stages=["stage_a"])
        # No upstream slices.
        names = [s.name for s in ctx.slices]
        assert not any("upstream" in n for n in names)

    def test_assemble_batch_upstream(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        reg = ArtifactRegistry()
        f1 = tmp_path / "1.md"
        f1.write_text("c1", encoding="utf-8")
        f2 = tmp_path / "2.md"
        f2.write_text("c2", encoding="utf-8")
        reg.register("write", "chapter", [f1, f2])
        asm = ContextAssembler(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=reg,
        )
        ctx = asm.assemble("review", consumes=["write"], executed_stages=["write"])
        names = [s.name for s in ctx.slices]
        # Both batch items become individual slices.
        assert any("chapter[1]" in n for n in names)
        assert any("chapter[2]" in n for n in names)

    def test_assemble_trims_when_over_budget(self, tmp_path: Path) -> None:
        # Tiny budget → must trim upstream slice.  Use CJK chars so each
        # glyph counts as ~1 token; 50 glyphs ≈ 60 tokens after margin.
        seed = tmp_path / "premise.md"
        seed.write_text("种子", encoding="utf-8")
        cfg = NovelProjectConfig(
            project_path=tmp_path / "novel-project.yaml",
            novel=_novel_spec(seeds=["premise.md"], constraints=[]),
            pipeline=PipelineSpec(stages=()),
            execution=ExecutionSpec(
                batch_size={},
                max_review_iterations=1,
                review_model="",
                write_model="",
                context=ContextSpec(total=200, context_reserve=10, output_reserve=12),
                retry=RetrySpec(max_retries=3, backoff="constant", max_wait=10),
            ),
        )
        reg = ArtifactRegistry()
        big = tmp_path / "big.md"
        big.write_text("字" * 100, encoding="utf-8")
        reg.register("gen", "big", big)
        asm = ContextAssembler(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=reg,
        )
        ctx = asm.assemble("write", consumes=["gen"], executed_stages=["gen"])
        # The upstream slice should be trimmed.
        upstream_slices = [s for s in ctx.slices if s.name.startswith("upstream:")]
        assert any(s.trimmed for s in upstream_slices)

    def test_assemble_raises_when_seeds_exceed_budget(self, tmp_path: Path) -> None:
        # Pathologically small budget + seeds that cannot fit → ContextOverflow.
        # Use CJK chars so 100 glyphs ≈ 120 tokens after margin; budget=5.
        seed = tmp_path / "premise.md"
        seed.write_text("字" * 100, encoding="utf-8")
        cfg = NovelProjectConfig(
            project_path=tmp_path / "novel-project.yaml",
            novel=_novel_spec(seeds=["premise.md"], constraints=[]),
            pipeline=PipelineSpec(stages=()),
            execution=ExecutionSpec(
                batch_size={},
                max_review_iterations=1,
                review_model="",
                write_model="",
                context=ContextSpec(total=200, context_reserve=5, output_reserve=12),
                retry=RetrySpec(max_retries=3, backoff="constant", max_wait=10),
            ),
        )
        reg = ArtifactRegistry()
        asm = ContextAssembler(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=reg,
        )
        with pytest.raises(ContextOverflow):
            asm.assemble("x", consumes=None, executed_stages=[])

    def test_render_template_passes_through(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        reg = ArtifactRegistry()
        a = tmp_path / "a.md"
        a.write_text("aaa", encoding="utf-8")
        reg.register("stage_a", "out", a)
        asm = ContextAssembler(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=reg,
        )
        out = asm.render_template("c", "see {{upstream.stage_a.out}}")
        assert "aaa" in out


# --------------------------------------------------------------------------- #
# AssembledContext
# --------------------------------------------------------------------------- #


class TestAssembledContext:
    def test_render_includes_non_empty_slices(self) -> None:
        ctx = AssembledContext(
            stage="x",
            slices=[
                ContextSlice(name="seed", content="hello", tokens=1),
                ContextSlice(name="empty", content="", tokens=0),
            ],
            prompt_template="do the work",
        )
        rendered = ctx.render()
        assert "hello" in rendered
        assert "do the work" in rendered
        # Empty slice content is skipped.
        assert "# empty" not in rendered

    def test_render_marks_trimmed_slices(self) -> None:
        ctx = AssembledContext(
            stage="x",
            slices=[
                ContextSlice(
                    name="trimmed_slice",
                    content="remaining",
                    tokens=1,
                    trimmed=True,
                ),
            ],
        )
        rendered = ctx.render()
        assert "trimmed_slice" in rendered
        assert "(trimmed)" in rendered

    def test_to_dict_round_trip(self) -> None:
        ctx = AssembledContext(
            stage="x",
            slices=[ContextSlice(name="s", content="c", tokens=2)],
            total_tokens=2,
            trim_log=["trimmed upstream:foo"],
        )
        d = ctx.to_dict()
        assert d["stage"] == "x"
        assert d["total_tokens"] == 2
        assert d["slices"][0]["name"] == "s"
        assert d["trim_log"] == ["trimmed upstream:foo"]


# --------------------------------------------------------------------------- #
# PromptRenderer
# --------------------------------------------------------------------------- #


class TestPromptRenderer:
    def test_novel_placeholder(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        result = renderer.render(
            "Title: {{novel.title}}, genre: {{novel.genre}}",
        )
        assert "Title: Test" in result.text
        assert "genre: 玄幻" in result.text
        assert result.novel_expansions == 2

    def test_novel_words_per_chapter(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        result = renderer.render(
            "min={{novel.words_per_chapter_min}} max={{novel.words_per_chapter_max}}"
        )
        assert "min=100" in result.text
        assert "max=200" in result.text

    def test_unknown_novel_attr_raises(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        with pytest.raises(ConfigError, match="unknown novel attribute"):
            renderer.render("{{novel.nonexistent}}")

    def test_ctx_placeholder(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        result = renderer.render(
            "stage={{ctx.stage_id}} batch={{ctx.batch}}",
            ctx={"stage_id": "write", "batch": "001"},
        )
        assert "stage=write" in result.text
        assert "batch=001" in result.text

    def test_unknown_ctx_attr_raises(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        with pytest.raises(ConfigError, match="unknown ctx attribute"):
            renderer.render("{{ctx.missing}}", ctx={})

    def test_include_placeholder_file(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        f = tmp_path / "snippet.md"
        f.write_text("# Snippet\nhello include", encoding="utf-8")
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        result = renderer.render("body: {{include: snippet.md}}")
        assert "hello include" in result.text
        assert "snippet.md" in result.include_files

    def test_include_placeholder_glob(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        (tmp_path / "a.md").write_text("aaa", encoding="utf-8")
        (tmp_path / "b.md").write_text("bbb", encoding="utf-8")
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        result = renderer.render("{{include: *.md}}")
        assert "aaa" in result.text
        assert "bbb" in result.text

    def test_include_no_match_warns(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        result = renderer.render("{{include: missing.md}}")
        assert any("did not match" in w for w in result.warnings)

    def test_upstream_placeholder_through_renderer(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        reg = ArtifactRegistry()
        f = tmp_path / "outline.md"
        f.write_text("outline content", encoding="utf-8")
        reg.register("gen", "outline", f)
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
            registry=reg,
        )
        result = renderer.render(
            "see {{upstream.gen.outline}}", stage_id="write"
        )
        assert "outline content" in result.text
        assert result.upstream_expansions == 1

    def test_no_placeholders_returns_text_unchanged(self, tmp_path: Path) -> None:
        cfg = _make_project(tmp_path, seeds=[], constraints=[])
        renderer = PromptRenderer(
            project_root=tmp_path,
            novel=cfg.novel,
            context_spec=cfg.execution.context,
        )
        result = renderer.render("just plain text")
        assert result.text == "just plain text"
        assert result.novel_expansions == 0
        assert result.ctx_expansions == 0
        assert result.upstream_expansions == 0
