"""M2 tests: state store, atomic write, checkpoints, recovery_plan."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from novelforge.errors import CheckpointCorrupt, StateError
from novelforge.state import Checkpoint, State, StateStore
from novelforge.utils.fs import atomic_write, sha256_file


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".novelforge"
    d.mkdir()
    return d


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """A project root that holds a couple of fake output files."""

    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "plot.md").write_text("# Plot\n", encoding="utf-8")
    (tmp_path / "output" / "summary.md").write_text("summary", encoding="utf-8")
    return tmp_path


STAGES = [
    "generate_outline",
    "review_outline",
    "write_chapter",
    "review_chapter",
    "final_polish",
]


def _build_checkpoint(project_root: Path, stage: str, batch: str) -> Checkpoint:
    files = []
    for path in sorted((project_root / "output").glob("*.md")):
        files.append(
            {
                "path": str(path.relative_to(project_root)),
                "sha256": sha256_file(path),
                "size": str(path.stat().st_size),
            }
        )
    return Checkpoint(stage=stage, batch=batch, files=files, timestamp="2026-06-06T00:00:00+0000")


# --------------------------------------------------------------------------- #
# atomic_write
# --------------------------------------------------------------------------- #


def test_atomic_write_replaces_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "x.yaml"
    atomic_write(target, "hi")
    assert target.read_text(encoding="utf-8") == "hi"


def test_atomic_write_cleans_up_temp_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"

    class Boom(Exception):
        pass

    real_replace = os.replace

    def failing_replace(src, dst):
        raise Boom("simulated crash")

    with patch("os.replace", side_effect=failing_replace):
        with pytest.raises(Boom):
            atomic_write(target, "data")
    # ensure no leftover .tmp file
    leftovers = list(target.parent.glob(target.name + ".*.tmp"))
    assert leftovers == [], f"leftover tmp files: {leftovers}"
    # restore
    os.replace = real_replace  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# StateStore load/save roundtrip
# --------------------------------------------------------------------------- #


def test_state_store_load_returns_empty_when_missing(state_dir: Path) -> None:
    store = StateStore(state_dir)
    state = store.load()
    assert state.current_stage is None
    assert state.progress["chapters_written"] == 0
    assert state.paused is False


def test_state_store_save_then_load(state_dir: Path) -> None:
    store = StateStore(state_dir)
    state = State(
        current_stage="write_chapter",
        started_at="2026-06-06T00:00:00+0000",
    )
    state.progress["chapters_written"] = 12
    state.token_usage["total_input"] = 5000
    store.save(state)
    raw = yaml.safe_load((state_dir / "state.yaml").read_text(encoding="utf-8"))
    assert raw["current_stage"] == "write_chapter"
    assert raw["progress"]["chapters_written"] == 12
    assert raw["token_usage"]["total_input"] == 5000
    # round-trip
    loaded = store.load()
    assert loaded.current_stage == "write_chapter"
    assert loaded.progress["chapters_written"] == 12
    assert loaded.token_usage["total_input"] == 5000


def test_state_store_corrupt_yaml_raises(state_dir: Path) -> None:
    (state_dir / "state.yaml").write_text("not: valid: yaml: : :", encoding="utf-8")
    store = StateStore(state_dir)
    with pytest.raises(StateError):
        store.load()


def test_state_store_write_helper(state_dir: Path) -> None:
    store = StateStore(state_dir)
    new = store.write(current_stage="review_chapter", paused=True, paused_reason="test")
    assert new.current_stage == "review_chapter"
    assert new.paused is True
    assert new.paused_reason == "test"
    # persisted?
    assert store.load().current_stage == "review_chapter"


def test_state_extra_survives_save_load(state_dir: Path) -> None:
    """Regression: ``state.extra`` (e.g. ``review_iterations``) must
    survive a save/load round-trip — the orchestrator relies on it to
    enforce the review-loop ceiling across process restarts.
    """

    store = StateStore(state_dir)
    s = State(current_stage="review_outline")
    s.extra["review_iterations"] = {"review_outline": 4}
    s.extra["review_loop_warnings"] = [
        {"stage": "review_outline", "iterations": 4}
    ]
    store.save(s)
    loaded = store.load()
    assert loaded.extra.get("review_iterations") == {"review_outline": 4}
    assert loaded.extra.get("review_loop_warnings") == [
        {"stage": "review_outline", "iterations": 4}
    ]


# --------------------------------------------------------------------------- #
# Checkpoint write / verify
# --------------------------------------------------------------------------- #


def test_checkpoint_write_and_read(project_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    cp = _build_checkpoint(project_root, "generate_outline", "001")
    path = store.write_checkpoint(cp)
    assert path.exists()
    loaded = store.read_checkpoint(path)
    assert loaded.stage == "generate_outline"
    assert loaded.batch == "001"
    assert len(loaded.files) == 2


def test_checkpoint_verify_ok(project_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    cp = _build_checkpoint(project_root, "generate_outline", "001")
    path = store.write_checkpoint(cp)
    loaded = store.verify_checkpoint(path, project_root)
    assert loaded.stage == "generate_outline"


def test_checkpoint_verify_detects_tampering(project_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    cp = _build_checkpoint(project_root, "generate_outline", "001")
    path = store.write_checkpoint(cp)
    # Modify one of the watched files
    target = project_root / "output" / "plot.md"
    target.write_text("tampered", encoding="utf-8")
    with pytest.raises(CheckpointCorrupt, match="hash mismatch"):
        store.verify_checkpoint(path, project_root)


def test_checkpoint_verify_detects_missing_file(project_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    cp = _build_checkpoint(project_root, "generate_outline", "001")
    path = store.write_checkpoint(cp)
    (project_root / "output" / "plot.md").unlink()
    with pytest.raises(CheckpointCorrupt, match="file missing"):
        store.verify_checkpoint(path, project_root)


def test_latest_checkpoint_returns_most_recent(project_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    store.write_checkpoint(_build_checkpoint(project_root, "generate_outline", "001"))
    store.write_checkpoint(_build_checkpoint(project_root, "write_chapter", "1"))
    latest = store.latest_checkpoint()
    assert latest is not None
    assert latest.name.startswith("write_chapter")


# --------------------------------------------------------------------------- #
# recovery_plan branches
# --------------------------------------------------------------------------- #


def test_recovery_plan_fresh_start(state_dir: Path) -> None:
    store = StateStore(state_dir)
    plan = store.recovery_plan(project_root=state_dir.parent, stages=STAGES)
    assert plan.next_stage == "generate_outline"
    assert plan.reason == "fresh_start"
    assert not plan.paused


def test_recovery_plan_uses_latest_valid_checkpoint(
    project_root: Path, state_dir: Path
) -> None:
    store = StateStore(state_dir)
    # Need a state.yaml so the decision tree enters the checkpoint branch.
    store.write(current_stage="generate_outline")
    store.write_checkpoint(_build_checkpoint(project_root, "review_outline", "001"))
    plan = store.recovery_plan(project_root=project_root, stages=STAGES)
    assert plan.next_stage == "write_chapter"
    assert "resuming" in plan.reason


def test_recovery_plan_pauses_on_corrupt_checkpoint(
    project_root: Path, state_dir: Path
) -> None:
    store = StateStore(state_dir)
    store.write(current_stage="generate_outline")
    cp = _build_checkpoint(project_root, "review_outline", "001")
    store.write_checkpoint(cp)
    # tamper with the watched file
    (project_root / "output" / "plot.md").write_text("X", encoding="utf-8")
    plan = store.recovery_plan(project_root=project_root, stages=STAGES)
    assert plan.paused
    assert plan.paused_reason == "all_checkpoints_corrupt"
    assert "all_checkpoints_corrupt" in plan.reason


def test_recovery_plan_force_stage_overrides(project_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    store.write(current_stage="generate_outline")
    store.write_checkpoint(_build_checkpoint(project_root, "generate_outline", "001"))
    plan = store.recovery_plan(
        project_root=project_root,
        stages=STAGES,
        force_stage="review_chapter",
    )
    assert plan.next_stage == "review_chapter"
    assert plan.forced
    assert plan.forced_stage == "review_chapter"


def test_recovery_plan_force_stage_unknown(
    project_root: Path, state_dir: Path
) -> None:
    store = StateStore(state_dir)
    plan = store.recovery_plan(
        project_root=project_root, stages=STAGES, force_stage="nope"
    )
    # Unknown forced stage: fall back to stages[0], but the user knows we tried.
    assert plan.next_stage == "generate_outline"
    assert plan.forced
    assert "unknown" in plan.reason


def test_recovery_plan_resume_paused(project_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    store.write(current_stage="review_chapter", paused=True, paused_reason="WriteFailure")
    plan = store.recovery_plan(project_root=project_root, stages=STAGES)
    assert plan.next_stage == "review_chapter"
    assert plan.paused
    assert plan.paused_reason == "WriteFailure"


def test_recovery_plan_pipeline_complete(project_root: Path, state_dir: Path) -> None:
    store = StateStore(state_dir)
    store.write(current_stage="final_polish")
    store.write_checkpoint(_build_checkpoint(project_root, "final_polish", "001"))
    plan = store.recovery_plan(project_root=project_root, stages=STAGES)
    assert plan.next_stage is None
    assert plan.reason == "pipeline_complete"


def test_recovery_plan_finds_earlier_valid_checkpoint(
    project_root: Path, state_dir: Path
) -> None:
    """Per plan §4.2: recovery should iterate through checkpoints newest-to-oldest
    and use the first valid one, not give up on the first corrupt one.
    """
    store = StateStore(state_dir)
    store.write(current_stage="write_chapter")

    # File A exists from the fixture.
    file_a = project_root / "output" / "plot.md"

    # Checkpoint 1: tracks only file_a (valid).
    cp1 = Checkpoint(
        stage="generate_outline",
        batch="001",
        files=[
            {
                "path": str(file_a.relative_to(project_root)),
                "sha256": sha256_file(file_a),
                "size": str(file_a.stat().st_size),
            }
        ],
        timestamp="2026-06-06T00:00:00+0000",
    )
    store.write_checkpoint(cp1)

    # Add file B and build checkpoint 2 that tracks both.
    file_b = project_root / "output" / "extra.md"
    file_b.write_text("original extra content", encoding="utf-8")
    cp2 = Checkpoint(
        stage="review_outline",
        batch="001",
        files=[
            {
                "path": str(file_a.relative_to(project_root)),
                "sha256": sha256_file(file_a),
                "size": str(file_a.stat().st_size),
            },
            {
                "path": str(file_b.relative_to(project_root)),
                "sha256": sha256_file(file_b),
                "size": str(file_b.stat().st_size),
            },
        ],
        timestamp="2026-06-06T01:00:00+0000",
    )
    store.write_checkpoint(cp2)

    # Tamper only file_b — cp2 becomes corrupt, cp1 stays valid.
    file_b.write_text("tampered!", encoding="utf-8")

    plan = store.recovery_plan(project_root=project_root, stages=STAGES)
    # Should NOT pause — it falls back to the valid generate_outline checkpoint
    # and resumes from review_outline (the stage after generate_outline).
    assert not plan.paused, f"should not pause when earlier checkpoint is valid: {plan.reason}"
    assert plan.next_stage == "review_outline"
    assert "generate_outline" in plan.reason


def test_state_dir_isolated_from_cybervisor(state_dir: Path) -> None:
    """T2.8 — the .novelforge/ path should not collide with .cybervisor/."""

    assert state_dir.name == ".novelforge"
    assert not (state_dir / "checkpoints").exists()
    StateStore(state_dir).ensure_dirs()
    assert (state_dir / "checkpoints").exists()
    assert (state_dir / "logs").exists()
    assert (state_dir / "metrics").exists()
