"""M8 end-to-end tests using the mock Claude adapter.

These tests exercise the full pipeline (config → orchestrator → stages →
state → checkpoint) against a minimal sample project without hitting the
real Claude API.  Each test cleans up ``.novelforge/`` before and after
to avoid cross-contamination.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from novelforge.claude.adapter import MockClaudeAdapter, MockResponse
from novelforge.config import load_config
from novelforge.errors import WriteFailure
from novelforge.orchestrator import Orchestrator
from novelforge.state import StateStore


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples" / "minimal-novel"


def _clean_novelforge(project_root: Path) -> None:
    """Remove .novelforge/ and output/ to start fresh."""
    for d in (".novelforge", "output"):
        target = project_root / d
        if target.exists():
            shutil.rmtree(target)


def _load_mock_adapter(orch: Orchestrator) -> MockClaudeAdapter:
    """Build and wire a MockClaudeAdapter with standard responses."""
    mock: MockClaudeAdapter = orch._build_adapter()  # type: ignore[assignment]

    # --- Outline ---
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

    # --- Review responses (APPROVED) ---
    approved_output = json.dumps(
        {
            "passed": True,
            "route": "APPROVED",
            "findings": [],
            "required_changes": [],
            "summary": "Looks good.",
        }
    )
    approved_parsed = {
        "passed": True,
        "route": "APPROVED",
        "findings": [],
    }
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
                input_tokens=30,
                output_tokens=40,
            ),
        )

    # --- Characters ---
    mock.set_response(
        "design_characters",
        MockResponse(
            output="# Wei Lin\nA young cultivator.\n\n# Master Chen\nA cynical mentor.",
            input_tokens=20,
            output_tokens=30,
        ),
    )

    # --- Simulate plot ---
    mock.set_response(
        "simulate_plot",
        MockResponse(
            output="# Plot Simulation\n\nEscalation curve looks solid.",
            input_tokens=20,
            output_tokens=30,
        ),
    )

    # --- Write chapter ---
    mock.set_response(
        "write_chapter",
        MockResponse(
            output=(
                "# Chapter 1 - The Discovery\n\n"
                "风起苍岚，少年魏林独坐山巅，目光穿透云雾。"
                "他在一座坍塌的古刹中发现了一卷泛黄的手札。\n"
                + ("那手札上的字迹时隐时现，仿佛有生命一般。" * 50)
                + "\n"
            ),
            input_tokens=60,
            output_tokens=200,
        ),
    )

    # --- Full consistency check ---
    mock.set_response(
        "full_consistency_check",
        MockResponse(
            output="# Consistency Report\n\nNo issues found.",
            input_tokens=40,
            output_tokens=50,
        ),
    )

    # --- Final polish ---
    mock.set_response(
        "final_polish",
        MockResponse(
            output="# Polish Notes\n\nMinor word-choice tweaks for chapter 1.",
            input_tokens=40,
            output_tokens=50,
        ),
    )

    orch._adapter = mock  # type: ignore[assignment]
    return mock


def _make_orchestrator(
    project_root: Path,
    *,
    stages_override: list[str] | None = None,
    max_review_iterations: int = 3,
) -> Orchestrator:
    """Create an orchestrator pointed at the sample project."""
    cfg_path = project_root / "novel-project.yaml"

    # Optionally patch stages_override into the config file.
    if stages_override is not None:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        raw.setdefault("pipeline", {})["stages_override"] = stages_override
        cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    cfg = load_config(cfg_path)
    return Orchestrator(
        config=cfg,
        config_path=cfg_path,
        project_root=project_root,
        use_mock=True,
        skip_polish=False,
    )


# --------------------------------------------------------------------------- #
# T8.3 — E2E 1: run produces at least 1 stage output
# --------------------------------------------------------------------------- #


class TestE2ERun:
    """T8.3: run on the sample project produces stage outputs."""

    def test_run_produces_output_files(self, tmp_path: Path) -> None:
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        orch = _make_orchestrator(
            project_root,
            stages_override=[
                "generate_outline",
                "review_outline",
                "design_characters",
                "review_characters",
                "simulate_plot",
                "review_simulation",
                "write_chapter",
                "review_chapter",
                "full_consistency_check",
                "final_polish",
            ],
        )
        mock = _load_mock_adapter(orch)

        summary = orch.run(fresh=True)
        assert summary["ok"], summary
        assert summary["stages_run"] >= 1

        # At least one output file exists
        output_dir = project_root / "output"
        assert output_dir.exists()
        plot = output_dir / "summaries" / "plot.md"
        assert plot.exists()
        outline = output_dir / "summaries" / "outline-tracking.md"
        assert outline.exists()

        # A chapter was written
        chapters = list((output_dir / "chapters").glob("*.md"))
        assert len(chapters) >= 1

        # state.yaml exists with correct fields
        state_path = project_root / ".novelforge" / "state.yaml"
        assert state_path.exists()
        state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        assert "current_stage" in state
        assert "token_usage" in state

        # Checkpoints exist
        ck_dir = project_root / ".novelforge" / "checkpoints"
        assert ck_dir.exists()
        cks = list(ck_dir.glob("*.yaml"))
        assert len(cks) >= 1

        _clean_novelforge(project_root)

    def test_run_exits_zero(self, tmp_path: Path) -> None:
        """The pipeline completes without error on a clean sample."""
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        orch = _make_orchestrator(
            project_root,
            stages_override=[
                "generate_outline",
                "write_chapter",
            ],
        )
        _load_mock_adapter(orch)

        summary = orch.run(fresh=True)
        assert summary["ok"]
        assert summary["status"] == "complete"
        _clean_novelforge(project_root)


# --------------------------------------------------------------------------- #
# T8.4 — E2E 2: interrupt & resume (A2)
# --------------------------------------------------------------------------- #


class TestE2EResume:
    """T8.4: interruption → resume continues from the correct stage."""

    def test_resume_skips_completed_stages(self, tmp_path: Path) -> None:
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        stages = [
            "generate_outline",
            "review_outline",
            "design_characters",
            "review_characters",
            "write_chapter",
            "review_chapter",
        ]
        orch = _make_orchestrator(project_root, stages_override=stages)
        mock = _load_mock_adapter(orch)

        # Run to completion
        summary1 = orch.run(fresh=True)
        assert summary1["ok"], summary1

        # Record token usage from first run
        state1 = StateStore(project_root / ".novelforge").load()
        tokens_in_1 = state1.token_usage["total_input"]
        tokens_out_1 = state1.token_usage["total_output"]

        # Simulate "resume" — fresh=False, no changes
        mock.calls.clear()
        summary2 = orch.run(fresh=False)
        assert summary2["ok"]
        assert summary2["stages_run"] == 0  # nothing new to run

        # State is consistent
        state2 = StateStore(project_root / ".novelforge").load()
        assert state2.token_usage["total_input"] == tokens_in_1
        assert state2.token_usage["total_output"] == tokens_out_1

        _clean_novelforge(project_root)

    def test_resume_after_simulated_interrupt(self, tmp_path: Path) -> None:
        """Simulate interrupt: run first two stages, kill, then resume."""
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        stages = [
            "generate_outline",
            "review_outline",
            "design_characters",
            "review_characters",
            "write_chapter",
            "review_chapter",
        ]
        orch = _make_orchestrator(project_root, stages_override=stages)
        mock = _load_mock_adapter(orch)

        # Run to completion first to establish checkpoints
        summary1 = orch.run(fresh=True)
        assert summary1["ok"], summary1

        # Now delete checkpoints for later stages to simulate partial run
        ck_dir = project_root / ".novelforge" / "checkpoints"
        for f in ck_dir.glob("design_characters-*.yaml"):
            f.unlink()
        for f in ck_dir.glob("review_characters-*.yaml"):
            f.unlink()
        for f in ck_dir.glob("write_chapter-*.yaml"):
            f.unlink()
        for f in ck_dir.glob("review_chapter-*.yaml"):
            f.unlink()

        # Update state to reflect we were mid-run (paused after review_outline)
        store = StateStore(project_root / ".novelforge")
        s = store.load()
        s.paused = True
        s.paused_reason = "Simulated interrupt"
        s.current_stage = "design_characters"
        store.save(s)

        # Resume with force_stage to bypass the paused check
        mock.calls.clear()
        summary2 = orch.run(fresh=False, force_stage="design_characters")
        assert summary2["ok"], summary2
        ran_stages = [c["stage"] for c in mock.calls]
        assert "design_characters" in ran_stages
        assert "write_chapter" in ran_stages

        _clean_novelforge(project_root)


# --------------------------------------------------------------------------- #
# T8.5 — E2E 3: corrupt checkpoint → error + force-stage hint
# --------------------------------------------------------------------------- #


class TestE2ECorruptCheckpoint:
    """T8.5 / T8.5b: corrupt checkpoint detection and --force-stage."""

    def test_corrupt_checkpoint_pauses(self, tmp_path: Path) -> None:
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        stages = [
            "generate_outline",
            "review_outline",
            "design_characters",
            "review_characters",
            "write_chapter",
            "review_chapter",
        ]
        orch = _make_orchestrator(project_root, stages_override=stages)
        _load_mock_adapter(orch)

        # Run to create checkpoints
        summary = orch.run(fresh=True)
        assert summary["ok"], summary

        # Corrupt ALL checkpoints so no valid one remains
        ck_dir = project_root / ".novelforge" / "checkpoints"
        ck_files = sorted(ck_dir.glob("*.yaml"))
        assert ck_files, "no checkpoints found"
        for ck in ck_files:
            ck.write_text(
                "garbage\n" + ck.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        # Resume — should pause with corrupt reason
        orch2 = _make_orchestrator(project_root, stages_override=stages)
        _load_mock_adapter(orch2)
        summary2 = orch2.run(fresh=False)
        assert summary2["paused"]
        assert summary2["paused_reason"] == "all_checkpoints_corrupt"

        _clean_novelforge(project_root)

    def test_force_stage_bypasses_corrupt_checkpoint(self, tmp_path: Path) -> None:
        """T8.5b: --force-stage skips corrupt checkpoint and continues."""
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        stages = [
            "generate_outline",
            "review_outline",
            "design_characters",
            "review_characters",
            "write_chapter",
            "review_chapter",
        ]
        orch = _make_orchestrator(project_root, stages_override=stages)
        _load_mock_adapter(orch)

        # Run to completion
        summary = orch.run(fresh=True)
        assert summary["ok"], summary

        # Corrupt ALL checkpoints to ensure the paused state triggers
        ck_dir = project_root / ".novelforge" / "checkpoints"
        ck_files = sorted(ck_dir.glob("*.yaml"))
        assert ck_files
        for ck in ck_files:
            ck.write_text(
                "corrupted!\n" + ck.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        # Resume with --force-stage=review_chapter
        orch2 = _make_orchestrator(project_root, stages_override=stages)
        _load_mock_adapter(orch2)
        summary2 = orch2.run(fresh=False, force_stage="review_chapter")
        assert summary2["ok"], summary2
        assert summary2.get("decision_reason", "").startswith("forced-stage=")

        # Verify state recorded the decision AND cleared the paused flag
        state = StateStore(project_root / ".novelforge").load()
        assert state.forced_stage == "review_chapter"
        assert state.paused is False, (
            "paused flag must be cleared after successful forced-stage resume"
        )
        assert state.paused_reason is None

        _clean_novelforge(project_root)


# --------------------------------------------------------------------------- #
# T8.6 — E2E 4: full pipeline with target_chapters=1 (A1)
# --------------------------------------------------------------------------- #


class TestE2EFullPipeline:
    """T8.6: generate → review → write → review → done."""

    def test_full_pipeline_target_1(self, tmp_path: Path) -> None:
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        # Use the long-epic template with all stages
        orch = _make_orchestrator(
            project_root,
            stages_override=[
                "generate_outline",
                "review_outline",
                "design_characters",
                "review_characters",
                "simulate_plot",
                "review_simulation",
                "write_chapter",
                "review_chapter",
                "full_consistency_check",
                "final_polish",
            ],
        )
        _load_mock_adapter(orch)

        summary = orch.run(fresh=True)
        assert summary["ok"], summary
        assert summary["stages_run"] >= 10  # may exceed due to review loops

        # Verify key outputs
        assert (project_root / "output" / "summaries" / "plot.md").exists()
        assert (project_root / "output" / "summaries" / "outline-tracking.md").exists()
        assert list((project_root / "output" / "chapters").glob("*.md"))
        assert list((project_root / "output" / "meta").glob("*.md"))
        assert (project_root / "output" / "review" / "outline-review.json").exists()
        assert (project_root / "output" / "review" / "chapter-review.json").exists()

        # state.yaml: all progress markers complete
        state = StateStore(project_root / ".novelforge").load()
        assert state.progress["outline"] == "complete"
        assert state.progress["characters"] == "complete"
        assert state.progress["simulation"] == "complete"
        assert state.progress["chapters_written"] >= 1

        _clean_novelforge(project_root)


# --------------------------------------------------------------------------- #
# T8.7 — E2E 5: context trimming with large chapter (A7)
# --------------------------------------------------------------------------- #


class TestE2EContextTrimming:
    """T8.7: large chapter triggers context trimming and logs it."""

    def test_large_chapter_triggers_trimming(self, tmp_path: Path) -> None:
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        # Configure file logging so pipeline.log is created
        from novelforge.utils.log import configure_logging
        configure_logging(level="INFO", log_dir=project_root / ".novelforge" / "logs", console=False)

        stages = [
            "generate_outline",
            "review_outline",
            "design_characters",
            "review_characters",
            "write_chapter",
            "review_chapter",
        ]
        orch = _make_orchestrator(project_root, stages_override=stages)
        mock = _load_mock_adapter(orch)

        # Run once to produce chapters
        summary1 = orch.run(fresh=True)
        assert summary1["ok"], summary1

        # Expand the chapter to ~50k characters (hits "history" trim tier)
        chap_dir = project_root / "output" / "chapters"
        chap_files = sorted(chap_dir.glob("*.md"))
        assert chap_files
        target_chap = chap_files[0]
        original = target_chap.read_text(encoding="utf-8")
        # Pad to ~50k characters
        padding = "这是一段用于测试上下文裁剪的灌水内容。" * 5000
        target_chap.write_text(original + "\n" + padding, encoding="utf-8")

        # Verify the padded file is large
        padded_size = len(target_chap.read_text(encoding="utf-8"))
        assert padded_size > 30_000, f"expected large file, got {padded_size} chars"

        # Now clear state and re-run — the context assembler should see
        # the large chapter and trim it.
        for f in (project_root / ".novelforge" / "checkpoints").glob("*.yaml"):
            f.unlink()
        (project_root / ".novelforge" / "state.yaml").unlink(missing_ok=True)

        orch2 = _make_orchestrator(project_root, stages_override=stages)
        mock2 = _load_mock_adapter(orch2)
        summary2 = orch2.run(fresh=True)
        assert summary2["ok"], summary2

        # Check pipeline.log for trimming evidence (verify.md §9)
        log_path = project_root / ".novelforge" / "logs" / "pipeline.log"
        assert log_path.exists(), "pipeline.log must exist after run"
        log_content = log_path.read_text(encoding="utf-8")
        # Per verify.md §9: pipeline.log must contain trimming records
        # when context overflows the budget.
        assert "trimmed" in log_content.lower(), (
            "Expected trimming evidence in pipeline.log for a ~50k char chapter. "
            "The context assembler should have detected overflow and trimmed."
        )

        _clean_novelforge(project_root)


# --------------------------------------------------------------------------- #
# T8.8 — E2E 6: injected 5xx → exponential backoff + pause (A8)
# --------------------------------------------------------------------------- #


class TestE2EErrorInjection:
    """T8.8: 5xx injection triggers backoff and eventual pause."""

    def test_injected_failure_triggers_pause(self, tmp_path: Path) -> None:
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        stages = [
            "generate_outline",
            "review_outline",
            "write_chapter",
            "review_chapter",
        ]
        orch = _make_orchestrator(project_root, stages_override=stages)
        mock = _load_mock_adapter(orch)

        # Inject permanent failure on generate_outline
        mock.set_failure("generate_outline", WriteFailure("500 Internal Server Error"))

        summary = orch.run(fresh=True)
        assert summary["paused"]
        assert "WriteFailure" in summary["paused_reason"]

        # state.yaml reflects the pause
        state = StateStore(project_root / ".novelforge").load()
        assert state.paused is True
        assert "WriteFailure" in (state.paused_reason or "")

        _clean_novelforge(project_root)

    def test_transient_failure_recovers(self, tmp_path: Path) -> None:
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        stages = [
            "generate_outline",
            "review_outline",
            "write_chapter",
            "review_chapter",
        ]
        orch = _make_orchestrator(project_root, stages_override=stages)
        mock = _load_mock_adapter(orch)

        # Fail once, then succeed
        real_invoke = mock.invoke
        call_count = {"n": 0}

        def flaky_invoke(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise WriteFailure("transient 500")
            return real_invoke(*args, **kwargs)

        mock.invoke = flaky_invoke  # type: ignore[method-assign]

        summary = orch.run(fresh=True)
        assert summary["ok"], summary
        assert call_count["n"] >= 2  # at least 1 retry happened

        # Token log has records
        log_path = project_root / ".novelforge" / "logs" / "token-usage.log"
        assert log_path.exists()
        records = [
            json.loads(l)
            for l in log_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert len(records) >= 1
        # All records are valid JSON with required fields
        for r in records:
            assert "timestamp" in r
            assert "stage" in r
            assert "input_tokens" in r
            assert "output_tokens" in r

        _clean_novelforge(project_root)


# --------------------------------------------------------------------------- #
# CLI E2E — novelforge run --use-mock (no manual mock wiring)
# --------------------------------------------------------------------------- #


class TestCLIEndToEnd:
    """CLI-level e2e: invoke ``novelforge run --use-mock`` without manually
    configuring mock responses.  Validates A1 acceptance path — the mock
    adapter's built-in defaults must be sufficient for a full pipeline run.
    """

    def test_cli_run_use_mock_exits_zero(self, tmp_path: Path) -> None:
        """``novelforge run --config ... --use-mock`` completes with exit code 0."""
        from typer.testing import CliRunner

        from novelforge.cli import app

        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        cfg_path = project_root / "novel-project.yaml"

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["run", "--config", str(cfg_path), "--use-mock", "--skip-polish"],
        )
        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}\nstdout={result.stdout}\n"
            f"exception={result.exception}"
        )
        assert "Pipeline finished" in result.stdout

        # Verify output files were created
        assert (project_root / "output" / "summaries" / "plot.md").exists()
        assert list((project_root / "output" / "chapters").glob("*.md"))

        # Verify state.yaml exists
        state_path = project_root / ".novelforge" / "state.yaml"
        assert state_path.exists()
        state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        assert "current_stage" in state
        assert "token_usage" in state

        # Verify checkpoints
        ck_dir = project_root / ".novelforge" / "checkpoints"
        assert ck_dir.exists()
        assert list(ck_dir.glob("*.yaml"))

        # Verify token-usage.log is valid JSONL
        log_path = project_root / ".novelforge" / "logs" / "token-usage.log"
        assert log_path.exists()
        records = [
            json.loads(l)
            for l in log_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert len(records) >= 1
        for r in records:
            assert "timestamp" in r
            assert "stage" in r
            assert "input_tokens" in r
            assert "output_tokens" in r

        _clean_novelforge(project_root)

    def test_cli_run_use_mock_full_pipeline(self, tmp_path: Path) -> None:
        """Full pipeline (including final_polish) via CLI with --use-mock."""
        from typer.testing import CliRunner

        from novelforge.cli import app

        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        cfg_path = project_root / "novel-project.yaml"

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["run", "--config", str(cfg_path), "--use-mock"],
        )
        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}\nstdout={result.stdout}\n"
            f"exception={result.exception}"
        )

        # All 10 stages should have produced output
        assert (project_root / "output" / "summaries" / "plot.md").exists()
        assert (project_root / "output" / "summaries" / "outline-tracking.md").exists()
        assert list((project_root / "output" / "chapters").glob("*.md"))

        _clean_novelforge(project_root)

    def test_exponential_backoff_logged(self, tmp_path: Path) -> None:
        """Verify that retry warnings appear in the log."""
        project_root = tmp_path / "sample"
        shutil.copytree(SAMPLE_DIR, project_root)
        _clean_novelforge(project_root)

        # Configure file logging so pipeline.log is created
        from novelforge.utils.log import configure_logging
        configure_logging(level="INFO", log_dir=project_root / ".novelforge" / "logs", console=False)

        stages = ["generate_outline", "write_chapter"]
        orch = _make_orchestrator(project_root, stages_override=stages)
        mock = _load_mock_adapter(orch)

        real_invoke = mock.invoke
        call_count = {"n": 0}

        def flaky(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise WriteFailure("transient")
            return real_invoke(*args, **kwargs)

        mock.invoke = flaky  # type: ignore[method-assign]

        summary = orch.run(fresh=True)
        assert summary["ok"]
        assert call_count["n"] >= 3

        # Check log for retry messages
        log_path = project_root / ".novelforge" / "logs" / "pipeline.log"
        assert log_path.exists()
        log_content = log_path.read_text(encoding="utf-8")
        assert "failed" in log_content.lower() or "retry" in log_content.lower()

        _clean_novelforge(project_root)
