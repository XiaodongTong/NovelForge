"""Tests for the v4 contract Orchestrator (Phase 3.5).

Covers the dual-layer retry matrix, batch driving, state persistence,
and the deletion of v3 route branches per spec §AC-1..AC-3 / plan D14.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from novelforge.config import (
    DoneWhenSpec,
    ProduceSpec,
    StageConfig,
    load_config,
)
from novelforge.errors import StageIncomplete, VerifyFailed
from novelforge.orchestrator import Orchestrator, RunSummary


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #


def _write_yaml(project_root: Path, body: str) -> Path:
    cfg_path = project_root / "novel-project.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    return cfg_path


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("# Premise\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Style\n", encoding="utf-8")
    return tmp_path


SINGLE_STAGE_YAML = """
novel:
  title: T
  genre: T
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: x
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: write
      model: m
      prompt: Write the section.
      produces:
        - path: output/out.md
          alias: out
      done_when:
        max_attempts: 2
        checks:
          - kind: min_chars
            target: output/out.md
            value: 5
execution:
  retry:
    backoff: constant
    max_wait: 1
"""

TWO_STAGE_YAML = """
novel:
  title: T
  genre: T
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: x
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: generate
      model: m
      prompt: Generate.
      produces:
        - path: output/gen.md
          alias: gen
      done_when:
        max_attempts: 2
        checks:
          - kind: min_chars
            target: output/gen.md
            value: 5
    - id: review
      model: m
      prompt: Review the upstream {{upstream.generate.gen}}.
      consumes: [generate]
      produces:
        - path: output/review.md
          alias: review
      done_when:
        max_attempts: 2
        checks:
          - kind: min_chars
            target: output/review.md
            value: 5
execution:
  retry:
    backoff: constant
    max_wait: 1
"""


def _run(tmp_path: Path, yaml_body: str, *, mock_env: dict[str, str] | None = None) -> tuple[Orchestrator, dict[str, Any]]:
    cfg_path = _write_yaml(tmp_path, yaml_body)
    cfg = load_config(cfg_path)
    orch = Orchestrator(
        config=cfg,
        config_path=cfg_path,
        project_root=tmp_path,
        use_mock=True,
    )
    # Apply env switches around the run.
    saved: dict[str, str] = {}
    if mock_env:
        for k, v in mock_env.items():
            saved[k] = os.environ.get(k, "")
            os.environ[k] = v
    try:
        summary = orch.run(fresh=True)
    finally:
        if mock_env:
            for k, orig in saved.items():
                if orig == "" and k not in os.environ:
                    continue
                if orig == "":
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = orig
    return orch, summary


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


def test_pipeline_completes_single_stage(project_root: Path) -> None:
    _orch, summary = _run(project_root, SINGLE_STAGE_YAML)
    assert summary["ok"] is True
    assert summary["status"] == "complete"
    assert (project_root / "output" / "out.md").exists()


def test_pipeline_two_stages_chains_via_upstream(project_root: Path) -> None:
    _orch, summary = _run(project_root, TWO_STAGE_YAML)
    assert summary["ok"] is True
    assert (project_root / "output" / "gen.md").exists()
    assert (project_root / "output" / "review.md").exists()


def test_registry_persisted_after_run(project_root: Path) -> None:
    orch, _summary = _run(project_root, SINGLE_STAGE_YAML)
    state_path = project_root / ".novelforge" / "state.yaml"
    raw = yaml.safe_load(state_path.read_text())
    artifacts = raw.get("extra", {}).get("artifacts", {})
    assert "write" in artifacts
    assert "out" in artifacts["write"]


def test_state_attempts_zero_after_success(project_root: Path) -> None:
    _orch, _summary = _run(project_root, SINGLE_STAGE_YAML)
    state_path = project_root / ".novelforge" / "state.yaml"
    raw = yaml.safe_load(state_path.read_text())
    attempts = raw.get("extra", {}).get("stage_attempts", {})
    assert attempts.get("write") == 0


# --------------------------------------------------------------------------- #
# C-tier retry (StageIncomplete / VerifyFailed)
# --------------------------------------------------------------------------- #


def test_stage_incomplete_recovers_on_second_attempt(project_root: Path) -> None:
    """NO_SIGNAL first invoke → StageIncomplete → second invoke recovers."""

    _orch, summary = _run(
        project_root,
        SINGLE_STAGE_YAML,
        mock_env={"NOVELFORGE_MOCK_NO_SIGNAL": "1"},
    )
    assert summary["ok"] is True, summary
    # The mock omits the signal exactly once, then recovers.
    state_path = project_root / ".novelforge" / "state.yaml"
    raw = yaml.safe_load(state_path.read_text())
    assert raw.get("extra", {}).get("stage_attempts", {}).get("write") == 0


def test_verify_failed_recovers_on_second_attempt(project_root: Path) -> None:
    """EMPTY first invoke → VerifyFailed → second invoke recovers."""

    _orch, summary = _run(
        project_root,
        SINGLE_STAGE_YAML,
        mock_env={"NOVELFORGE_MOCK_EMPTY": "1"},
    )
    assert summary["ok"] is True, summary


def test_max_attempts_exhausted_applies_on_failure_pause(project_root: Path) -> None:
    """ALWAYS_FAIL + max_attempts=2 → pause."""

    _orch, summary = _run(
        project_root,
        SINGLE_STAGE_YAML,
        mock_env={"NOVELFORGE_MOCK_ALWAYS_FAIL": "1"},
    )
    assert summary["paused"] is True
    state_path = project_root / ".novelforge" / "state.yaml"
    raw = yaml.safe_load(state_path.read_text())
    assert raw.get("extra", {}).get("stage_attempts", {}).get("write") == 2


# --------------------------------------------------------------------------- #
# on_failure dispositions
# --------------------------------------------------------------------------- #


SKIP_YAML = """
novel:
  title: T
  genre: T
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: x
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: a
      model: m
      prompt: A.
      produces:
        - path: output/a.md
          alias: a
      on_failure: skip
      done_when:
        max_attempts: 2
    - id: b
      model: m
      prompt: B.
      produces:
        - path: output/b.md
          alias: b
execution:
  retry:
    backoff: constant
    max_wait: 1
"""


def test_on_failure_skip_advances_to_next_stage(project_root: Path) -> None:
    """Stage 'a' always fails; with on_failure:skip, 'b' still runs and
    succeeds."""

    from novelforge.claude.adapter import MockClaudeAdapter, MockResponse

    cfg_path = _write_yaml(project_root, SKIP_YAML)
    cfg = load_config(cfg_path)
    orch = Orchestrator(
        config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True,
    )
    # Build a mock adapter with per-stage canned responses:
    # - stage 'a' omits the signal on every invoke → exhausts max_attempts.
    # - stage 'b' emits a normal body + signal.
    mock = MockClaudeAdapter()
    mock.set_response("a", MockResponse(output="always-fails", omit_signal=True))
    mock.set_response(
        "b", MockResponse(output="The chapter content unfolds with care. " * 5)
    )
    orch._adapter = mock  # type: ignore[attr-defined]

    summary = orch.run(fresh=True)
    assert summary["ok"] is True, summary
    assert (project_root / "output" / "b.md").exists()


# --------------------------------------------------------------------------- #
# Batch driving (D14)
# --------------------------------------------------------------------------- #


BATCH_YAML = """
novel:
  title: T
  genre: T
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: x
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: batched
      model: m
      prompt: Write.
      produces:
        - path: output/ch-{{num:03d}}.md
          alias: chapter
      batch: 3
      done_when:
        max_attempts: 2
        checks:
          - kind: min_chars
            target: output/ch-{{num:03d}}.md
            value: 5
execution:
  retry:
    backoff: constant
    max_wait: 1
"""


def test_batch_driving_writes_one_file_per_item(project_root: Path) -> None:
    _orch, summary = _run(project_root, BATCH_YAML)
    assert summary["ok"] is True, summary
    for i in (1, 2, 3):
        assert (project_root / "output" / f"ch-{i:03d}.md").exists()


def test_batch_attempts_persisted_as_list(project_root: Path) -> None:
    _orch, _summary = _run(project_root, BATCH_YAML)
    state_path = project_root / ".novelforge" / "state.yaml"
    raw = yaml.safe_load(state_path.read_text())
    attempts = raw.get("extra", {}).get("stage_attempts", {}).get("batched")
    assert isinstance(attempts, list)
    assert len(attempts) == 3
    assert all(a == 0 for a in attempts)


MULTI_PRODUCE_BATCH_YAML = """
novel:
  title: T
  genre: T
  target_chapters: 1
  words_per_chapter: [100, 200]
  style: x
  seeds: [outline/premise.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: batched
      model: m
      prompt: Write.
      produces:
        - path: output/a-{{num:03d}}.md
          alias: alpha
        - path: output/b-{{num:03d}}.md
          alias: beta
      batch: 3
      done_when:
        max_attempts: 2
execution:
  retry:
    backoff: constant
    max_wait: 1
"""


def test_multi_produce_batch_registers_each_alias_as_list(
    project_root: Path,
) -> None:
    """AC-7: every produces[].alias of a batch stage must end up as a
    length-N list in the registry (not just ``produces[0]``)."""

    _orch, summary = _run(project_root, MULTI_PRODUCE_BATCH_YAML)
    assert summary["ok"] is True, summary
    state_path = project_root / ".novelforge" / "state.yaml"
    raw = yaml.safe_load(state_path.read_text())
    artifacts = raw.get("extra", {}).get("artifacts", {}).get("batched", {})
    alpha = artifacts.get("alpha")
    beta = artifacts.get("beta")
    assert isinstance(alpha, list) and len(alpha) == 3, (
        f"alpha should be a list of length 3; got {alpha!r}"
    )
    assert isinstance(beta, list) and len(beta) == 3, (
        f"beta should be a list of length 3; got {beta!r}"
    )
    # The two alias lists must not cross-contaminate.
    alpha_names = {Path(p).name for p in alpha}
    beta_names = {Path(p).name for p in beta}
    assert all(n.startswith("a-") for n in alpha_names), alpha_names
    assert all(n.startswith("b-") for n in beta_names), beta_names


def test_batch_corrupt_length_detected(tmp_path: Path) -> None:
    """A persisted attempts list whose length != batch:N is treated as
    a corrupt checkpoint and surfaces a StateError."""

    # Bootstrap a half-finished state.
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text("# Premise\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Style\n", encoding="utf-8")
    cfg_path = _write_yaml(tmp_path, BATCH_YAML)
    state_dir = tmp_path / ".novelforge"
    state_dir.mkdir()
    state_path = state_dir / "state.yaml"
    state_path.write_text(
        yaml.safe_dump(
            {
                "current_stage": "batched",
                "extra": {
                    "stage_attempts": {"batched": [0, 0]},  # wrong length
                },
            }
        )
    )
    cfg = load_config(cfg_path)
    orch = Orchestrator(
        config=cfg, config_path=cfg_path, project_root=tmp_path, use_mock=True,
    )
    summary = orch.run(fresh=False)
    # We don't enforce the exact error type at the public boundary; we
    # only require that the pipeline surfaces as paused (not crashed).
    assert summary["paused"] is True or summary["ok"] is False


# --------------------------------------------------------------------------- #
# Resume attempts reset (AC-10)
# --------------------------------------------------------------------------- #


def test_resume_resets_attempts_for_paused_stage(project_root: Path) -> None:
    """AC-10: after ``on_failure: pause`` triggers, the next
    ``novelforge run`` must reset the paused stage's attempt counter
    to 0 so the user gets a fresh ``max_attempts`` budget.

    Without the reset, the counter would start at ``max_attempts``
    and immediately re-trigger ``on_failure`` after one invoke.
    """

    cfg_path = _write_yaml(project_root, SINGLE_STAGE_YAML)
    cfg = load_config(cfg_path)
    saved = os.environ.get("NOVELFORGE_MOCK_ALWAYS_FAIL", "")
    os.environ["NOVELFORGE_MOCK_ALWAYS_FAIL"] = "1"
    try:
        orch1 = Orchestrator(
            config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True,
        )
        summary1 = orch1.run(fresh=True)
    finally:
        if saved:
            os.environ["NOVELFORGE_MOCK_ALWAYS_FAIL"] = saved
        else:
            os.environ.pop("NOVELFORGE_MOCK_ALWAYS_FAIL", None)
    assert summary1["paused"] is True
    state_path = project_root / ".novelforge" / "state.yaml"
    raw = yaml.safe_load(state_path.read_text())
    assert raw["extra"]["stage_attempts"]["write"] == 2

    # Resume with ALWAYS_FAIL still set.  Without the AC-10 reset the
    # counter would jump straight to 3 (2 + 1) and re-pause; with the
    # reset it re-accumulates from 0 to ``max_attempts`` (=2).
    os.environ["NOVELFORGE_MOCK_ALWAYS_FAIL"] = "1"
    try:
        orch2 = Orchestrator(
            config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True,
        )
        summary2 = orch2.run(fresh=False)
    finally:
        if saved:
            os.environ["NOVELFORGE_MOCK_ALWAYS_FAIL"] = saved
        else:
            os.environ.pop("NOVELFORGE_MOCK_ALWAYS_FAIL", None)
    assert summary2["paused"] is True
    raw2 = yaml.safe_load(state_path.read_text())
    assert raw2["extra"]["stage_attempts"]["write"] == 2, (
        f"AC-10 violation: resume should reset attempts to 0 then "
        f"re-accumulate to max_attempts=2; got "
        f"{raw2['extra']['stage_attempts']['write']}"
    )


def test_resume_succeeds_when_failure_cleared(project_root: Path) -> None:
    """AC-10 follow-up: after pause + resume, the stage can succeed
    within a fresh ``max_attempts`` budget (mock reverts to default
    behavior on the second run)."""

    cfg_path = _write_yaml(project_root, SINGLE_STAGE_YAML)
    cfg = load_config(cfg_path)
    saved = os.environ.get("NOVELFORGE_MOCK_ALWAYS_FAIL", "")
    os.environ["NOVELFORGE_MOCK_ALWAYS_FAIL"] = "1"
    try:
        orch1 = Orchestrator(
            config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True,
        )
        summary1 = orch1.run(fresh=True)
    finally:
        if saved:
            os.environ["NOVELFORGE_MOCK_ALWAYS_FAIL"] = saved
        else:
            os.environ.pop("NOVELFORGE_MOCK_ALWAYS_FAIL", None)
    assert summary1["paused"] is True

    # Resume without ALWAYS_FAIL — stage should now succeed and clear
    # the attempts counter.
    orch2 = Orchestrator(
        config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True,
    )
    summary2 = orch2.run(fresh=False)
    assert summary2["ok"] is True, summary2
    state_path = project_root / ".novelforge" / "state.yaml"
    raw2 = yaml.safe_load(state_path.read_text())
    assert raw2["extra"]["stage_attempts"]["write"] == 0


# --------------------------------------------------------------------------- #
# AC-18 / AC-15: no v3 routes
# --------------------------------------------------------------------------- #


def test_no_v3_route_strings_in_orchestrator_source() -> None:
    src_path = Path(__file__).resolve().parent.parent / "src" / "novelforge" / "orchestrator.py"
    text = src_path.read_text(encoding="utf-8")
    for forbidden in ("NEEDS_REWRITE", "FUNDAMENTAL_ISSUE", "APPROVED"):
        assert forbidden not in text, f"orchestrator.py still mentions {forbidden!r}"
