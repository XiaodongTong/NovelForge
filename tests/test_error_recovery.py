"""M7 tests: error recovery, backoff, log rotation."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from novelforge.claude.adapter import MockClaudeAdapter, MockResponse
from novelforge.config import load_config
from novelforge.errors import (
    CheckpointCorrupt,
    CLIError,
    ContextOverflow,
    FundamentIssue,
    RateLimited,
    SchemaInvalid,
    WriteFailure,
)
from novelforge.orchestrator import Orchestrator
from novelforge.state import StateStore
from novelforge.utils.log import configure_logging, log_stage_enter, log_stage_exit


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text(
        "## Premise\nA cultivator.\n", encoding="utf-8"
    )
    (tmp_path / "outline" / "world.md").write_text(
        "## World\nRealms.\n", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
    return tmp_path


def _write_yaml(project_root: Path) -> Path:
    body = """
novel:
  title: "Test"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [200, 400]
  style: "test"
  seeds: [outline/premise.md, outline/world.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
  stages_override:
    - generate_outline
    - review_outline
    - design_characters
    - write_chapter
    - review_chapter
execution:
  batch_size: { outline: 5, chapter: 1 }
  max_review_iterations: 2
  retry: { max_retries: 2, backoff: "exponential", max_wait: 1 }
"""
    path = project_root / "novel-project.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _orchestrator(project_root: Path) -> Orchestrator:
    cfg_path = _write_yaml(project_root)
    cfg = load_config(cfg_path)
    return Orchestrator(
        config=cfg,
        config_path=cfg_path,
        project_root=project_root,
        use_mock=True,
        skip_polish=True,
    )


def _outline() -> MockResponse:
    return MockResponse(
        output="## Plot\n\nA hero rises.\n\n## Chapter 1 - X\nBeats.\n",
    )


def _approved() -> MockResponse:
    return MockResponse(
        output=json.dumps(
            {"passed": True, "route": "APPROVED", "findings": []}
        ),
        parsed={"passed": True, "route": "APPROVED", "findings": []},
    )


def _characters() -> MockResponse:
    return MockResponse(output="# A\nA character.\n")


def _chapter() -> MockResponse:
    return MockResponse(
        output="# Chapter 1 - X\n\n" + ("word " * 200 + "\n") * 2
    )


# --------------------------------------------------------------------------- #
# Retry / backoff
# --------------------------------------------------------------------------- #


def test_retry_then_succeed(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    calls = {"n": 0}

    real_invoke = mock.invoke

    def flaky_invoke(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise WriteFailure("transient")
        return real_invoke(*args, **kwargs)

    mock.invoke = flaky_invoke  # type: ignore[method-assign]
    mock.set_response("generate_outline", _outline())
    mock.set_response("review_outline", _approved())
    mock.set_response("design_characters", _characters())
    mock.set_response("write_chapter", _chapter())
    mock.set_response("review_chapter", _approved())
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    assert summary["ok"], summary
    # 2 retries on top of the initial call
    assert calls["n"] >= 3


def test_exhausted_retries_pause(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_failure("generate_outline", WriteFailure("always fails"))
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    assert summary["paused"]
    assert "WriteFailure" in summary["paused_reason"]


def test_rate_limited_pauses(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_failure("generate_outline", RateLimited("429"))
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    assert summary["paused"]
    assert summary["paused_reason"] == "RateLimited"


def test_context_overflow_pauses(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_failure("generate_outline", ContextOverflow("ctx too big"))
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    assert summary["paused"]
    assert summary["paused_reason"] == "ContextOverflow"


def test_schema_invalid_pauses(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_failure("generate_outline", SchemaInvalid("bad json"))
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    assert summary["paused"]
    assert summary["paused_reason"] == "SchemaInvalid"


def test_checkpoint_corrupt_pauses(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline())
    mock.set_response("review_outline", _approved())
    mock.set_response("design_characters", _characters())
    mock.set_response("write_chapter", _chapter())
    mock.set_response("review_chapter", _approved())
    orch._adapter = mock  # type: ignore[assignment]

    orch.run(fresh=True)
    # Corrupt ALL checkpoints so recovery cannot find any valid one.
    ck_dir = (project_root / ".novelforge").joinpath("checkpoints")
    for ck in ck_dir.glob("*.yaml"):
        ck.write_text("garbage\n" + ck.read_text(encoding="utf-8"), encoding="utf-8")
    # Now try to resume
    summary = orch.run(fresh=False)
    # recovery_plan should pause when all checkpoints are corrupt
    assert summary["paused"]
    assert summary["paused_reason"] == "all_checkpoints_corrupt"


# --------------------------------------------------------------------------- #
# Backoff strategy
# --------------------------------------------------------------------------- #


def test_backoff_exponential_grows() -> None:
    s1 = Orchestrator._backoff_seconds("exponential", 0, 100)
    s2 = Orchestrator._backoff_seconds("exponential", 1, 100)
    s3 = Orchestrator._backoff_seconds("exponential", 2, 100)
    assert s1 < s2 < s3


def test_backoff_linear_grows() -> None:
    s1 = Orchestrator._backoff_seconds("linear", 0, 100)
    s2 = Orchestrator._backoff_seconds("linear", 1, 100)
    s3 = Orchestrator._backoff_seconds("linear", 2, 100)
    assert s1 < s2 < s3
    # linear grows by 1 each step
    assert s2 - s1 == pytest.approx(1.0)
    assert s3 - s2 == pytest.approx(1.0)


def test_backoff_clamped_to_max_wait() -> None:
    s = Orchestrator._backoff_seconds("exponential", 20, 10)
    assert s == 10


def test_backoff_constant_is_one_second() -> None:
    assert Orchestrator._backoff_seconds("constant", 0, 100) == 1.0


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def test_log_stage_enter_exit(tmp_path: Path) -> None:
    log_path = tmp_path / "pipeline.log"
    configure_logging(level="INFO", log_dir=tmp_path, console=False)
    log_stage_enter("generate_outline", batch="001")
    log_stage_exit("generate_outline", route="APPROVED", duration=0.5)
    contents = log_path.read_text(encoding="utf-8")
    assert "stage_enter generate_outline batch=001" in contents
    assert "stage_exit generate_outline route=APPROVED" in contents


def test_log_files_created(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(level="INFO", log_dir=log_dir, console=False)
    logging.getLogger("novelforge").info("hello")
    logging.getLogger("novelforge").error("oops")
    assert (log_dir / "pipeline.log").exists()
    assert (log_dir / "errors.log").exists()


def test_token_usage_log_appended_even_on_recovery(project_root: Path) -> None:
    """T7.6: JSONL usage log continues appending across retries."""
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    # Succeed after one failure
    real = mock.invoke
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WriteFailure("transient")
        return real(*args, **kwargs)

    mock.invoke = flaky  # type: ignore[method-assign]
    mock.set_response("generate_outline", _outline())
    mock.set_response("review_outline", _approved())
    mock.set_response("design_characters", _characters())
    mock.set_response("write_chapter", _chapter())
    mock.set_response("review_chapter", _approved())
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    assert summary["ok"]
    log_path = project_root / ".novelforge" / "logs" / "token-usage.log"
    assert log_path.exists()
    # We expect at least one entry per successful call; the retried call
    # left no entry because the failure happened before the call
    # returned.
    records = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) >= 1
    stages = {r["stage"] for r in records}
    assert "generate_outline" in stages
