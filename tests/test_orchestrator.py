"""M5 tests: orchestrator FSM, run / resume / pause behaviour."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pytest
import yaml

from novelforge.claude.adapter import MockClaudeAdapter, MockResponse
from novelforge.config import load_config
from novelforge.errors import FundamentIssue, SchemaInvalid
from novelforge.orchestrator import Orchestrator
from novelforge.review.gate import ReviewGate
from novelforge.state import StateStore


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
  retry: { max_retries: 1, backoff: "exponential", max_wait: 1 }
"""
    path = project_root / "novel-project.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _approved_review(stage: str) -> MockResponse:
    return MockResponse(
        output=json.dumps(
            {
                "passed": True,
                "route": "APPROVED",
                "findings": [],
                "required_changes": [],
                "summary": "looks good",
            }
        ),
        parsed={
            "passed": True,
            "route": "APPROVED",
            "findings": [],
            "required_changes": [],
        },
    )


def _outline_response() -> MockResponse:
    body = (
        "## Plot\n\nA hero rises.\n\n"
        "## Chapter 1 - The Summons\nA knock at the door.\n"
    )
    return MockResponse(output=body, input_tokens=10, output_tokens=20)


def _characters_response() -> MockResponse:
    return MockResponse(
        output=(
            "# Aria\nA young cultivator.\n\n# Master Hsu\n"
            "A cynical mentor."
        ),
        input_tokens=10,
        output_tokens=20,
    )


def _chapter_response() -> MockResponse:
    body = (
        "# Chapter 1 - The Summons\n\n"
        "It was a stormy night in the lower realm. " * 20
    )
    return MockResponse(output=body, input_tokens=10, output_tokens=200)


def _orchestrator(project_root: Path, use_mock: bool = True) -> Orchestrator:
    cfg_path = _write_yaml(project_root)
    cfg = load_config(cfg_path)
    return Orchestrator(
        config=cfg,
        config_path=cfg_path,
        project_root=project_root,
        use_mock=use_mock,
        skip_polish=True,
    )


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_run_writes_state_and_checkpoints(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock: MockClaudeAdapter = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    mock.set_response("review_outline", _approved_review("review_outline"))
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    assert summary["ok"], summary
    assert summary["status"] == "complete"
    assert summary["stages_run"] >= 5
    # state.yaml exists
    state_path = project_root / ".novelforge" / "state.yaml"
    assert state_path.exists()
    state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    assert state["current_stage"] is None or state["current_stage"] == "review_chapter"
    # Checkpoints exist for every stage
    ck_dir = project_root / ".novelforge" / "checkpoints"
    assert (ck_dir / "generate_outline-001.yaml").exists()
    assert (ck_dir / "review_outline-001.yaml").exists()
    assert (ck_dir / "write_chapter-001.yaml").exists()
    # Output files exist
    assert (project_root / "output" / "summaries" / "plot.md").exists()
    assert (project_root / "output" / "summaries" / "outline-tracking.md").exists()
    assert (project_root / "output" / "chapters").glob("*.md")


def test_run_records_token_usage(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    mock.set_response("review_outline", _approved_review("review_outline"))
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]
    orch.run(fresh=True)
    log_path = project_root / ".novelforge" / "logs" / "token-usage.log"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert all(json.loads(l) for l in lines)
    # at least one record per stage
    stages = {json.loads(l)["stage"] for l in lines}
    assert {"generate_outline", "write_chapter"}.issubset(stages)


# --------------------------------------------------------------------------- #
# Resume / pause
# --------------------------------------------------------------------------- #


def test_resume_skips_completed_stages(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    mock.set_response("review_outline", _approved_review("review_outline"))
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]

    # First run gets to design_characters, we "interrupt" by pausing
    # manually (set paused=True in state).
    orch.run(fresh=True)
    # Reset mock calls; second run should still complete from where we left off.
    mock.calls.clear()
    summary = orch.run(fresh=False)
    assert summary["ok"], summary
    # all stages already complete, so 0 new stages were run
    assert summary["stages_run"] == 0
    assert summary["status"] == "complete"


def test_paused_state_resumes_on_explicit_resume(project_root: Path) -> None:
    """Spec: "暂停状态可被 resume 命令拉起".

    When the user explicitly calls resume (fresh=False), the paused flag
    is cleared so the pipeline attempts to continue.  If the underlying
    issue persists the pipeline will pause again.
    """
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    mock.set_response("review_outline", _approved_review("review_outline"))
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]

    # Manually set paused state with a current_stage and a checkpoint
    # for the stage *before* write_chapter so recovery can find it.
    store = StateStore(project_root / ".novelforge")
    store.write(
        current_stage="write_chapter",
        paused=True,
        paused_reason="WriteFailure",
    )
    # Write a checkpoint for design_characters so recovery_plan finds it
    from novelforge.state import Checkpoint
    from novelforge.utils.fs import sha256_file

    (project_root / "output").mkdir(exist_ok=True)
    dummy = project_root / "output" / "dummy.txt"
    dummy.write_text("data", encoding="utf-8")
    store.write_checkpoint(
        Checkpoint(
            stage="design_characters",
            batch="001",
            files=[{"path": str(dummy.relative_to(project_root)), "sha256": sha256_file(dummy), "size": "4"}],
            timestamp="2026-06-06T00:00:00+0000",
        )
    )

    summary = orch.run(fresh=False)
    # The paused flag was cleared, and the pipeline should have continued.
    assert summary["ok"], summary
    assert summary["stages_run"] >= 1
    # Verify the paused flag was cleared in state
    final_state = store.load()
    assert final_state.paused is False


def test_fundamental_issue_pauses(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    fund = MockResponse(
        output=json.dumps(
            {"passed": False, "route": "FUNDAMENTAL_ISSUE", "findings": ["x"]}
        ),
        parsed={"passed": False, "route": "FUNDAMENTAL_ISSUE", "findings": ["x"]},
    )
    mock.set_response("review_outline", fund)
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    assert summary["paused"]
    assert summary["paused_reason"] == "FUNDAMENTAL_ISSUE"
    # state reflects the pause
    state = StateStore(project_root / ".novelforge").load()
    assert state.paused is True
    assert state.paused_reason == "FUNDAMENTAL_ISSUE"


def test_needs_rewrite_loops_review(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    needs_rewrite = MockResponse(
        output=json.dumps(
            {
                "passed": False,
                "route": "NEEDS_REWRITE",
                "findings": ["tighten the hook"],
                "required_changes": ["add foreshadowing"],
            }
        ),
        parsed={
            "passed": False,
            "route": "NEEDS_REWRITE",
            "findings": ["tighten the hook"],
            "required_changes": ["add foreshadowing"],
        },
    )
    mock.set_response("review_outline", needs_rewrite)
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    # review_outline loops once with needs_rewrite, then we move on.
    assert summary["ok"], summary
    # the review_outline stage should have been called at least twice.
    review_calls = [c for c in mock.calls if c["stage"] == "review_outline"]
    assert len(review_calls) >= 2


# --------------------------------------------------------------------------- #
# Review-loop ceiling: bug regressions
# --------------------------------------------------------------------------- #


def test_review_loop_warning_records_review_stage_not_next(project_root: Path) -> None:
    """Regression: when ``review_X`` hits the loop ceiling the warning
    entry must record ``stage: review_X``, not the *next* stage that
    follows it in the pipeline (the prior implementation mis-attributed
    the warning to the next stage).
    """

    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    # review_outline always returns NEEDS_REWRITE; the configured
    # max_review_iterations is 2, so iterations 3, 4, … all hit the
    # loop ceiling and accept the current version.
    needs_rewrite = MockResponse(
        output=json.dumps(
            {"passed": False, "route": "NEEDS_REWRITE", "findings": ["x"]}
        ),
        parsed={"passed": False, "route": "NEEDS_REWRITE", "findings": ["x"]},
    )
    mock.set_response("review_outline", needs_rewrite)
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    assert summary["ok"], summary
    state = StateStore(project_root / ".novelforge").load()
    warnings = state.extra.get("review_loop_warnings", [])
    assert warnings, "expected a review_loop_warnings entry"
    # Every warning must point at the review stage that hit the loop,
    # not at the next stage.  Here only review_outline should appear.
    bad = [w for w in warnings if w["stage"] != "review_outline"]
    assert not bad, (
        f"review_loop_warnings stage mis-attributed: {warnings}"
    )


def test_last_review_iterations_survives_subsequent_stages(project_root: Path) -> None:
    """Regression: ``state.last_review_iterations`` must keep the
    iteration count from the most recent review even after a non-review
    stage (e.g. ``design_characters``) runs and overwrites it.
    """

    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    # review_outline approves on the first try (iter_count=1).
    mock.set_response("review_outline", _approved_review("review_outline"))
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    assert summary["ok"], summary
    state = StateStore(project_root / ".novelforge").load()
    # After review_outline approved at iteration 1, and subsequent
    # stages have run, the field should still read 1, not 0.
    assert state.last_review_iterations == 1, (
        f"last_review_iterations was clobbered; got {state.last_review_iterations}"
    )


def test_review_loop_ceiling_checkpoint_batch_is_not_decision_reason(
    project_root: Path,
) -> None:
    """Regression: the ReviewLoopExceeded branch previously wrote a
    checkpoint file with the *decision reason* (e.g. ``fresh_start``) as
    its batch suffix, producing junk files like
    ``review_chapter-fresh_start.yaml``.  The batch must be a real
    chapter/iteration index instead.
    """

    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    needs_rewrite = MockResponse(
        output=json.dumps(
            {"passed": False, "route": "NEEDS_REWRITE", "findings": ["x"]}
        ),
        parsed={"passed": False, "route": "NEEDS_REWRITE", "findings": ["x"]},
    )
    mock.set_response("review_outline", needs_rewrite)
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]
    orch.run(fresh=True)
    ck_dir = project_root / ".novelforge" / "checkpoints"
    # No checkpoint should use a non-numeric batch suffix.
    for ck in ck_dir.glob("review_outline-*.yaml"):
        stem = ck.stem  # e.g. "review_outline-001"
        suffix = stem.split("-", 1)[1] if "-" in stem else ""
        assert suffix.isdigit(), (
            f"checkpoint batch suffix must be numeric, got {ck.name}"
        )


# --------------------------------------------------------------------------- #
# Status command payload
# --------------------------------------------------------------------------- #


def test_status_payload_reflects_state(project_root: Path) -> None:
    orch = _orchestrator(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("generate_outline", _outline_response())
    mock.set_response("review_outline", _approved_review("review_outline"))
    mock.set_response("design_characters", _characters_response())
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response("review_chapter", _approved_review("review_chapter"))
    orch._adapter = mock  # type: ignore[assignment]
    orch.run(fresh=True)
    from novelforge.cli import status  # late import to avoid heavy deps

    from typer.testing import CliRunner

    from novelforge.cli import app

    cfg_path = project_root / "novel-project.yaml"
    runner = CliRunner()
    result = runner.invoke(app, ["status", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "current_stage" in payload
    assert payload["target_chapters"] == 1
    assert "progress" in payload
    assert "token_usage" in payload
    assert "last_checkpoint_at" in payload


def test_status_reads_actual_persisted_state(project_root: Path) -> None:
    """Regression: status command must load real state values, not defaults.

    Previously, ``StateStore`` was constructed with the state *file* path
    instead of the *directory* path, which caused ``load()`` to return
    a fresh default ``State()`` — silently hiding paused / token info.
    """

    from typer.testing import CliRunner

    from novelforge.cli import app

    # Create the config file so validate/load succeeds
    cfg_path = _write_yaml(project_root)

    # Write a non-trivial paused state directly
    store = StateStore(project_root / ".novelforge")
    store.write(
        current_stage="write_chapter",
        paused=True,
        paused_reason="WriteFailure exhausted",
    )
    s = store.load()
    s.token_usage["total_input"] = 9999
    s.token_usage["total_output"] = 42
    s.progress["chapters_written"] = 3
    store.save(s)

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)

    # These values MUST match what was persisted — not the defaults.
    assert payload["paused"] is True
    assert payload["paused_reason"] == "WriteFailure exhausted"
    assert payload["current_stage"] == "write_chapter"
    assert payload["token_usage"]["total_input"] == 9999
    assert payload["token_usage"]["total_output"] == 42
    assert payload["progress"]["chapters_written"] == 3
