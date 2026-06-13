"""Tests for the v4 contract GenericStage (Phase 3.4).

Covers the transactional execution flow per spec §AC-1/AC-2/AC-3 and the
attempt_hint injection (AC-17).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from novelforge.artifact_registry import ArtifactRegistry
from novelforge.claude.adapter import (
    MockClaudeAdapter,
    MockResponse,
    StageResult,
)
from novelforge.config import (
    DoneWhenSpec,
    NovelProjectConfig,
    ProduceSpec,
    StageConfig,
)
from novelforge.errors import ConfigError, StageIncomplete, VerifyFailed
from novelforge.stages.base import StageContext
from novelforge.stages.generic import GenericStage
from novelforge.verify import CheckSpec, DEFAULT_COMPLETION_SIGNAL


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _min_cfg(tmp_path: Path) -> NovelProjectConfig:
    """A bare NovelProjectConfig for tests that don't actually load yaml."""
    from novelforge.config import (
        BatchSize,
        ContextSpec,
        ExecutionSpec,
        NovelSpec,
        PipelineSpec,
        RetrySpec,
    )

    return NovelProjectConfig(
        project_path=tmp_path / "novel-project.yaml",
        novel=NovelSpec(
            title="Test",
            genre="Test",
            target_chapters=1,
            words_per_chapter=(100, 200),
            style="x",
            seeds=(),
            constraints=(),
        ),
        pipeline=PipelineSpec(stages=()),
        execution=ExecutionSpec(
            batch_size=BatchSize(),
            context=ContextSpec(),
            retry=RetrySpec(),
            max_review_iterations=3,
            review_model="m",
            write_model="m",
        ),
    )


def _stage(
    *,
    id: str = "write_chapter",
    produces: tuple[ProduceSpec, ...] = (
        ProduceSpec(path="output/out.md", alias="out"),
    ),
    done_when: Optional[DoneWhenSpec] = None,
    consumes: Optional[tuple[str, ...]] = None,
    batch: int = 1,
    prompt: str = "Write a short passage.",
) -> StageConfig:
    return StageConfig(
        id=id,
        model="mock-model",
        prompt=prompt,
        produces=produces,
        done_when=done_when or DoneWhenSpec(),
        consumes=consumes,
        batch=batch,
    )


def _ctx(
    cfg: NovelProjectConfig,
    project_root: Path,
    *,
    stage: StageConfig,
    registry: Optional[ArtifactRegistry] = None,
    batch: Optional[str] = None,
    attempt: int = 1,
    last_failure: Optional[Mapping[str, Any]] = None,
) -> StageContext:
    # Note: ArtifactRegistry defines __len__ so an empty instance is
    # falsy; we must check ``is None`` rather than ``or`` to preserve
    # the caller's instance.
    extras: dict[str, Any] = {
        "stage_config": stage,
        "registry": registry if registry is not None else ArtifactRegistry(),
        "attempt": attempt,
    }
    if last_failure is not None:
        extras["last_failure"] = dict(last_failure)
    return StageContext(
        config=cfg,
        project_root=project_root,
        stage_id=stage.id,
        batch=batch,
        extras=extras,
    )


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


def test_single_text_produce_writes_file_and_registers(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage()
    adapter = MockClaudeAdapter()
    adapter.set_response(
        "write_chapter",
        MockResponse(
            output="# Title\n\nThe chapter content unfolds with deliberate care. " * 10,
        ),
    )
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(cfg, tmp_path, stage=stage, registry=registry)
    result = gen.execute(ctx)

    out = tmp_path / "output" / "out.md"
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("# Title")
    assert result.files == [out]
    assert result.completion_signal is True
    assert registry.has("write_chapter", "out")
    assert registry.get_one("write_chapter", "out") == out


def test_multiple_produces_writes_each_file(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage(
        id="dual",
        produces=(
            ProduceSpec(path="output/a.md", alias="alpha"),
            ProduceSpec(path="output/b.md", alias="beta"),
        ),
    )
    body = "# alpha\n\nThe chapter content unfolds with deliberate care. " * 5
    adapter = MockClaudeAdapter()
    adapter.set_response("dual", MockResponse(output=body))
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(cfg, tmp_path, stage=stage, registry=registry)
    result = gen.execute(ctx)

    a = tmp_path / "output" / "a.md"
    b = tmp_path / "output" / "b.md"
    assert a.exists() and b.exists()
    # The completion signal is stripped from the file body before write
    # (output_parser._strip_completion_signal).  Both files contain the
    # prose body without the trailing signal line.
    assert a.read_text().startswith("# alpha")
    assert b.read_text().startswith("# alpha")
    assert registry.has("dual", "alpha")
    assert registry.has("dual", "beta")
    assert len(result.files) == 2


def test_json_produce_pretty_prints_payload(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage(
        id="review",
        produces=(ProduceSpec(path="output/r.json", alias="review"),),
    )
    adapter = MockClaudeAdapter()
    adapter.set_response(
        "review",
        MockResponse(
            output='{"passed": true, "findings": []}',
        ),
    )
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(cfg, tmp_path, stage=stage, registry=registry)
    gen.execute(ctx)

    import json
    payload = json.loads((tmp_path / "output" / "r.json").read_text())
    assert payload == {"passed": True, "findings": []}


def test_batch_produce_uses_num_placeholder(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage(
        id="batched",
        produces=(
            ProduceSpec(path="output/ch-{{num:03d}}.md", alias="chapter"),
        ),
        batch=3,
    )
    adapter = MockClaudeAdapter()
    adapter.set_response("batched", MockResponse(output="# Section\n\nText body. " * 5))
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(
        cfg, tmp_path, stage=stage, registry=registry, batch="002"
    )
    result = gen.execute(ctx)

    written = tmp_path / "output" / "ch-002.md"
    assert written.exists(), f"expected ch-002.md in {list((tmp_path / 'output').iterdir())}"
    assert result.files == [written]


def test_split_produce_writes_one_file_per_match(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage(
        id="split",
        produces=(
            ProduceSpec(
                path="output/c-{{num}}-{{title|slug}}.md",
                alias="chapter",
                split=r"^# Chapter (?P<num>\d+) - (?P<title>.+?)$",
            ),
        ),
    )
    body = (
        "# Chapter 1 - The Beginning\n\nFirst beat.\n\n"
        "# Chapter 2 - Continuation\n\nSecond beat.\n"
    )
    adapter = MockClaudeAdapter()
    adapter.set_response("split", MockResponse(output=body))
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(cfg, tmp_path, stage=stage, registry=registry)
    result = gen.execute(ctx)

    a = tmp_path / "output" / "c-1-the-beginning.md"
    b = tmp_path / "output" / "c-2-continuation.md"
    assert a.exists() and b.exists()
    assert "First beat." in a.read_text()
    assert "Second beat." in b.read_text()
    # split stages register as a list[Path]
    stored = registry.get_list("split", "chapter")
    assert set(stored) == {a, b}


# --------------------------------------------------------------------------- #
# First-layer completion-signal check
# --------------------------------------------------------------------------- #


def test_missing_completion_signal_raises_stage_incomplete(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage()
    adapter = MockClaudeAdapter()
    adapter.set_response(
        "write_chapter",
        MockResponse(output="prose without the signal", omit_signal=True),
    )
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(cfg, tmp_path, stage=stage, registry=registry)
    with pytest.raises(StageIncomplete, match="completion signal"):
        gen.execute(ctx)
    # The file must not have been written or registered.
    assert not (tmp_path / "output" / "out.md").exists()
    assert not registry.has("write_chapter", "out")


def test_completion_signal_disabled_skips_first_layer(tmp_path: Path) -> None:
    """When done_when.completion_signal is None, the first-layer check
    is bypassed: a body without any signal still completes the stage."""

    cfg = _min_cfg(tmp_path)
    stage = _stage(done_when=DoneWhenSpec(completion_signal=None))
    adapter = MockClaudeAdapter()
    adapter.set_response(
        "write_chapter",
        MockResponse(output="# Hello\n\nno signal anywhere", omit_signal=True),
    )
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(cfg, tmp_path, stage=stage, registry=registry)
    # Must NOT raise StageIncomplete even though the body has no signal.
    result = gen.execute(ctx)
    assert (tmp_path / "output" / "out.md").exists()
    # detect_completion_signal returns True when the expected marker is
    # empty; the contract here is "stage succeeded", not "signal detected".
    assert result.completion_signal is True


# --------------------------------------------------------------------------- #
# Second-layer done_when.checks
# --------------------------------------------------------------------------- #


def test_done_when_failure_raises_verify_failed(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage(
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="min_chars",
                    target="output/out.md",
                    value=10_000,
                ),
            ),
        ),
    )
    adapter = MockClaudeAdapter()
    adapter.set_response("write_chapter", MockResponse(output="short body"))
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(cfg, tmp_path, stage=stage, registry=registry)
    with pytest.raises(VerifyFailed) as excinfo:
        gen.execute(ctx)
    err = excinfo.value
    assert err.kind == "min_chars"
    assert err.target.endswith("output/out.md")
    assert err.expected == 10_000


def test_done_when_passes_when_check_satisfied(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage(
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(kind="exists", target="output/out.md"),
                CheckSpec(
                    kind="min_chars",
                    target="output/out.md",
                    value=10,
                ),
            ),
        ),
    )
    adapter = MockClaudeAdapter()
    adapter.set_response(
        "write_chapter", MockResponse(output="adequate length content"),
    )
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    ctx = _ctx(cfg, tmp_path, stage=stage, registry=registry)
    result = gen.execute(ctx)
    assert result.files


# --------------------------------------------------------------------------- #
# attempt_hint injection (AC-17)
# --------------------------------------------------------------------------- #


def test_attempt_hint_injected_on_retry(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage()
    adapter = MockClaudeAdapter()
    adapter.set_response("write_chapter", MockResponse(output="text"))
    gen = GenericStage(adapter)
    registry = ArtifactRegistry()
    last_failure: Mapping[str, Any] = {
        "type": "VerifyFailed",
        "detail": "min_chars target=output/out.md expected=1000 actual=12",
    }
    ctx = _ctx(
        cfg,
        tmp_path,
        stage=stage,
        registry=registry,
        attempt=2,
        last_failure=last_failure,
    )
    gen.execute(ctx)
    sent_prompt = adapter.calls[0]["prompt"]
    assert "Attempt: 2" in sent_prompt
    assert "VerifyFailed" in sent_prompt
    assert "min_chars" in sent_prompt


def test_attempt_hint_absent_on_first_call(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage()
    adapter = MockClaudeAdapter()
    adapter.set_response("write_chapter", MockResponse(output="text"))
    gen = GenericStage(adapter)
    ctx = _ctx(cfg, tmp_path, stage=stage, attempt=1)
    gen.execute(ctx)
    assert "Attempt: 1" not in adapter.calls[0]["prompt"]


# --------------------------------------------------------------------------- #
# No-retry invariant
# --------------------------------------------------------------------------- #


def test_generic_stage_does_not_retry_internally(tmp_path: Path) -> None:
    """A single execute() triggers at most one adapter invoke."""

    cfg = _min_cfg(tmp_path)
    stage = _stage()
    adapter = MockClaudeAdapter()
    adapter.set_response("write_chapter", MockResponse(output="ok content"))
    gen = GenericStage(adapter)
    ctx = _ctx(cfg, tmp_path, stage=stage)
    gen.execute(ctx)
    assert len(adapter.calls) == 1


# --------------------------------------------------------------------------- #
# Registry propagation
# --------------------------------------------------------------------------- #


def test_disabled_stage_short_circuits(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = StageConfig(
        id="disabled",
        model="m",
        prompt="noop",
        produces=(ProduceSpec(path="output/x.md", alias="x"),),
        enabled=False,
    )
    adapter = MockClaudeAdapter()
    gen = GenericStage(adapter)
    ctx = _ctx(cfg, tmp_path, stage=stage)
    result = gen.execute(ctx)
    assert result.files == []
    assert result.raw_output == ""
    assert adapter.calls == []


def test_missing_stage_config_in_extras_raises(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    ctx = StageContext(
        config=cfg,
        project_root=tmp_path,
        stage_id="x",
        extras={},
    )
    gen = GenericStage(MockClaudeAdapter())
    with pytest.raises(ConfigError, match="stage_config"):
        gen.execute(ctx)


def test_missing_registry_in_extras_raises(tmp_path: Path) -> None:
    cfg = _min_cfg(tmp_path)
    stage = _stage()
    ctx = StageContext(
        config=cfg,
        project_root=tmp_path,
        stage_id=stage.id,
        extras={"stage_config": stage},
    )
    gen = GenericStage(MockClaudeAdapter())
    with pytest.raises(ConfigError, match="registry"):
        gen.execute(ctx)
