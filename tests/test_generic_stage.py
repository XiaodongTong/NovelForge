"""Tests for the v4 :class:`GenericStage`."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import pytest

from novelforge.claude.adapter import MockClaudeAdapter, MockResponse
from novelforge.config import (
    BatchSize,
    ContextSpec,
    ExecutionSpec,
    NovelProjectConfig,
    NovelSpec,
    PipelineSpec,
    RetrySpec,
    StageConfig,
)
from novelforge.stages import GenericStage, build_v4_stage, is_v4_config
from novelforge.stages.base import StageContext


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
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "summaries").mkdir()
    (tmp_path / "output" / "chapters").mkdir()
    return tmp_path


def _build_cfg(
    stages: list[StageConfig],
    *,
    raw: Optional[dict] = None,
) -> NovelProjectConfig:
    return NovelProjectConfig(
        project_path=Path("."),
        novel=NovelSpec(
            title="T",
            genre="x",
            target_chapters=1,
            words_per_chapter=(800, 1500),
            style="lean",
            seeds=("outline/premise.md", "outline/world.md"),
            constraints=("CLAUDE.md",),
        ),
        pipeline=PipelineSpec(stages=tuple(stages)),
        execution=ExecutionSpec(
            batch_size=BatchSize(),
            context=ContextSpec(
                total=200_000,
                context_reserve=2000,
                output_reserve=500,
                rolling_window=3,
                outline_range=10,
            ),
            retry=RetrySpec(),
            max_review_iterations=3,
            review_model="claude-sonnet-4-6",
            write_model="claude-opus-4-7",
            route_history_max=50,
        ),
        raw=raw or {},
    )


def _ctx(
    cfg: NovelProjectConfig,
    project_root: Path,
    stage_config: StageConfig,
    *,
    batch: str = "001",
) -> StageContext:
    return StageContext(
        config=cfg,
        project_root=project_root,
        stage_id=stage_config.id,
        batch=batch,
        extras={"stage_config": stage_config},
    )


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_build_v4_stage_returns_singleton() -> None:
    mock = MockClaudeAdapter()
    a = build_v4_stage(mock)
    b = build_v4_stage(mock)
    assert isinstance(a, GenericStage)


# --------------------------------------------------------------------------- #
# is_v4_config detection
# --------------------------------------------------------------------------- #


def test_is_v4_config_true_with_explicit_stages(project_root: Path) -> None:
    raw = {
        "novel": {},
        "pipeline": {
            "stages": [
                {"id": "x", "model": "m", "prompt": "p", "output": "o"}
            ]
        },
    }
    cfg = _build_cfg(
        [StageConfig(id="x", model="m", prompt="p", output="o")],
        raw=raw,
    )
    assert is_v4_config(cfg) is True


def test_is_v4_config_false_with_template_only(project_root: Path) -> None:
    raw = {"novel": {}, "pipeline": {"template": "long-epic"}}
    cfg = _build_cfg([], raw=raw)
    assert is_v4_config(cfg) is False


def test_is_v4_config_false_when_stages_missing(project_root: Path) -> None:
    raw = {"novel": {}, "pipeline": {"scaffold_from": "long-epic"}}
    cfg = _build_cfg([], raw=raw)
    assert is_v4_config(cfg) is False


# --------------------------------------------------------------------------- #
# Execution: text form
# --------------------------------------------------------------------------- #


def test_generic_stage_text_form_writes_raw_output(
    project_root: Path,
) -> None:
    cfg = _build_cfg(
        [
            StageConfig(
                id="outline",
                model="m",
                prompt="make outline",
                output="output/summaries/outline.md",
            )
        ]
    )
    mock = MockClaudeAdapter()
    mock.set_response(
        "outline",
        MockResponse(
            output="# My Outline\n\nstuff\n",
            input_tokens=10,
            output_tokens=20,
        ),
    )
    stage = GenericStage(adapter=mock)
    res = stage.execute(_ctx(cfg, project_root, cfg.pipeline.stages[0]))
    assert res.route == "APPROVED"
    target = project_root / "output" / "summaries" / "outline.md"
    assert target.exists()
    assert "My Outline" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Execution: JSON form
# --------------------------------------------------------------------------- #


def test_generic_stage_json_form_persists_payload(
    project_root: Path,
) -> None:
    cfg = _build_cfg(
        [
            StageConfig(
                id="review",
                model="m",
                prompt="review it",
                output="output/review/review.json",
            )
        ]
    )
    mock = MockClaudeAdapter()
    mock.set_response(
        "review",
        MockResponse(
            output=json.dumps({"route": "done", "findings": []}),
            input_tokens=10,
            output_tokens=20,
            parsed={"route": "done", "findings": []},
        ),
    )
    stage = GenericStage(adapter=mock)
    res = stage.execute(_ctx(cfg, project_root, cfg.pipeline.stages[0]))
    assert res.route == "done"
    target = project_root / "output" / "review" / "review.json"
    assert target.exists()
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == {"route": "done", "findings": []}


# --------------------------------------------------------------------------- #
# Execution: split form
# --------------------------------------------------------------------------- #


def test_generic_stage_split_form_writes_per_chapter_files(
    project_root: Path,
) -> None:
    cfg = _build_cfg(
        [
            StageConfig(
                id="write_chapter",
                model="m",
                prompt="write chapter",
                output="output/chapters/{{num:03d}}-{{title|slug}}.md",
                split=(
                    r"^#\s+Chapter\s+"
                    r"(?P<num>\d+)\s*[-–—:]?\s*"
                    r"(?P<title>.+?)\s*$"
                ),
            )
        ]
    )
    mock = MockClaudeAdapter()
    body = (
        "# Chapter 1 - The Summons\n\nIt was dark.\n\n"
        "# Chapter 2 - The Choice\n\nShe hesitated.\n"
    )
    mock.set_response(
        "write_chapter",
        MockResponse(output=body, input_tokens=10, output_tokens=20),
    )
    stage = GenericStage(adapter=mock)
    res = stage.execute(_ctx(cfg, project_root, cfg.pipeline.stages[0]))
    assert res.route == "APPROVED"
    chap_dir = project_root / "output" / "chapters"
    files = sorted(chap_dir.glob("*.md"))
    assert len(files) == 2
    assert files[0].name == "001-the-summons.md"
    assert files[1].name == "002-the-choice.md"


# --------------------------------------------------------------------------- #
# Execution: disabled stage is a no-op
# --------------------------------------------------------------------------- #


def test_generic_stage_disabled_skips_invoke(project_root: Path) -> None:
    cfg = _build_cfg(
        [
            StageConfig(
                id="outline",
                model="m",
                prompt="p",
                output="output/summaries/o.md",
                enabled=False,
            )
        ]
    )
    mock = MockClaudeAdapter()
    stage = GenericStage(adapter=mock)
    res = stage.execute(_ctx(cfg, project_root, cfg.pipeline.stages[0]))
    # No Claude call was made.
    assert not mock.calls
    assert res.route == "SKIPPED"
    assert res.raw_output == ""


# --------------------------------------------------------------------------- #
# Prompt resolution: file path vs inline
# --------------------------------------------------------------------------- #


def test_generic_stage_prompt_loaded_from_file_when_path_exists(
    project_root: Path,
) -> None:
    prompt_path = project_root / "outline.md"
    prompt_path.write_text("FILE-PROMPT", encoding="utf-8")
    cfg = _build_cfg(
        [
            StageConfig(
                id="outline",
                model="m",
                prompt="outline.md",  # no newline → file
                output="output/summaries/o.md",
            )
        ]
    )
    mock = MockClaudeAdapter()
    stage = GenericStage(adapter=mock)
    stage.execute(_ctx(cfg, project_root, cfg.pipeline.stages[0]))
    # Mock adapter records the prompt that was sent.
    sent_prompt = mock.calls[0]["prompt"]
    assert "FILE-PROMPT" in sent_prompt


def test_generic_stage_prompt_inline_when_newline(project_root: Path) -> None:
    cfg = _build_cfg(
        [
            StageConfig(
                id="outline",
                model="m",
                prompt="line1\nline2",  # newline → inline
                output="output/summaries/o.md",
            )
        ]
    )
    mock = MockClaudeAdapter()
    stage = GenericStage(adapter=mock)
    stage.execute(_ctx(cfg, project_root, cfg.pipeline.stages[0]))
    sent_prompt = mock.calls[0]["prompt"]
    assert "line1" in sent_prompt
    assert "line2" in sent_prompt


# --------------------------------------------------------------------------- #
# A9: .json stage with non-JSON model output → SchemaInvalid
# --------------------------------------------------------------------------- #


def test_generic_stage_json_form_rejects_non_json_payload(
    project_root: Path,
) -> None:
    """A9: a `.json` output stage must raise SchemaInvalid when the
    model returns a non-JSON payload, not silently write raw text."""

    from novelforge.errors import SchemaInvalid

    cfg = _build_cfg(
        [
            StageConfig(
                id="review",
                model="m",
                prompt="review it",
                output="output/review/review.json",
            )
        ]
    )
    mock = MockClaudeAdapter()
    mock.set_response(
        "review",
        MockResponse(
            output="totally not json at all",
            input_tokens=10,
            output_tokens=20,
            parsed=None,
        ),
    )
    stage = GenericStage(adapter=mock)
    with pytest.raises(SchemaInvalid, match="review"):
        stage.execute(_ctx(cfg, project_root, cfg.pipeline.stages[0]))
    # No product file should have been written.
    assert not (project_root / "output" / "review" / "review.json").exists()


def test_generic_stage_json_form_accepts_fenced_block(project_root: Path) -> None:
    """The JSON parser must accept fenced code blocks (mirrors
    output_parser behaviour)."""

    cfg = _build_cfg(
        [
            StageConfig(
                id="review",
                model="m",
                prompt="review it",
                output="output/review/review.json",
            )
        ]
    )
    mock = MockClaudeAdapter()
    payload = {"route": "done", "findings": []}
    fenced = "Here you go:\n\n```json\n" + json.dumps(payload) + "\n```\n"
    mock.set_response(
        "review",
        MockResponse(output=fenced, input_tokens=10, output_tokens=20, parsed=payload),
    )
    stage = GenericStage(adapter=mock)
    res = stage.execute(_ctx(cfg, project_root, cfg.pipeline.stages[0]))
    assert res.route == "done"
    on_disk = json.loads(
        (project_root / "output" / "review" / "review.json").read_text(
            encoding="utf-8"
        )
    )
    assert on_disk == payload
