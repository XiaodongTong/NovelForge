"""End-to-end integration tests for the v4 contract pipeline (Phase 4.2).

Three independent sub-tests, each driving the full pipeline through
the Mock adapter and exercising one of the negative-switch scenarios
documented in ``plan.md`` §4.2:

- ``NOVELFORGE_MOCK_NO_SIGNAL=1`` — first invoke per (stage, batch)
  omits the completion signal; subsequent invokes recover.
- ``NOVELFORGE_MOCK_EMPTY=1``     — first invoke writes empty produces;
  subsequent invokes recover.
- ``NOVELFORGE_MOCK_ALWAYS_FAIL=1`` — every invoke both omits the signal
  and writes empty produces; used to drive ``max_attempts`` exhaustion.

Each sub-test cleans its own ``.novelforge/`` + ``output/`` directories
so they don't pollute each other.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from novelforge.config import load_config
from novelforge.orchestrator import Orchestrator


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


SAMPLE_YAML = """
novel:
  title: Sample
  genre: 玄幻修仙
  target_chapters: 1
  words_per_chapter: [100, 500]
  style: x
  seeds:
    - outline/premise.md
    - outline/world.md
  constraints:
    - CLAUDE.md
pipeline:
  stages:
    - id: generate
      model: m
      prompt: Generate the outline.
      produces:
        - path: output/summaries/plot.md
          alias: outline
      done_when:
        max_attempts: 3
        checks:
          - kind: min_chars
            target: output/summaries/plot.md
            value: 50
    - id: write
      model: m
      prompt: Review the upstream {{upstream.generate.outline}}.
      consumes: [generate]
      produces:
        - path: output/chapter.md
          alias: chapter
      done_when:
        max_attempts: 3
        checks:
          - kind: min_chars
            target: output/chapter.md
            value: 50
execution:
  retry:
    backoff: constant
    max_wait: 1
"""


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text(
        "# Premise\nA young hero rises. ", encoding="utf-8"
    )
    (tmp_path / "outline" / "world.md").write_text(
        "# World\nThe world is broken. ", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text("# Style\n", encoding="utf-8")
    cfg_path = tmp_path / "novel-project.yaml"
    cfg_path.write_text(SAMPLE_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def env_switch() -> Iterator[None]:
    """Ensure no stale env switches leak between tests."""

    keys = (
        "NOVELFORGE_MOCK_NO_SIGNAL",
        "NOVELFORGE_MOCK_EMPTY",
        "NOVELFORGE_MOCK_ALWAYS_FAIL",
    )
    saved = {k: os.environ.get(k, "") for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)


def _build_orchestrator(project_root: Path) -> Orchestrator:
    cfg_path = project_root / "novel-project.yaml"
    cfg = load_config(cfg_path)
    return Orchestrator(
        config=cfg,
        config_path=cfg_path,
        project_root=project_root,
        use_mock=True,
    )


def _clean_runtime(project_root: Path) -> None:
    for sub in (".novelforge", "output"):
        target = project_root / sub
        if target.exists():
            shutil.rmtree(target)


def _state_yaml(project_root: Path) -> dict[str, Any]:
    state_path = project_root / ".novelforge" / "state.yaml"
    if not state_path.exists():
        return {}
    return yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------- #
# Sub-test 1: NO_SIGNAL recovers on second attempt
# --------------------------------------------------------------------------- #


def test_no_signal_first_invoke_recovers(project_root: Path, env_switch) -> None:
    os.environ["NOVELFORGE_MOCK_NO_SIGNAL"] = "1"
    orch = _build_orchestrator(project_root)
    summary = orch.run(fresh=True)
    assert summary["ok"] is True, summary
    # Both stages completed.
    assert (project_root / "output" / "summaries" / "plot.md").exists()
    assert (project_root / "output" / "chapter.md").exists()
    # Attempts reset to 0 on success.
    extra = _state_yaml(project_root).get("extra", {})
    assert extra.get("stage_attempts", {}).get("generate") == 0
    assert extra.get("stage_attempts", {}).get("write") == 0
    # Artifacts persisted.
    assert "generate" in extra.get("artifacts", {})


# --------------------------------------------------------------------------- #
# Sub-test 2: EMPTY recovers on second attempt
# --------------------------------------------------------------------------- #


def test_empty_first_invoke_recovers(project_root: Path, env_switch) -> None:
    os.environ["NOVELFORGE_MOCK_EMPTY"] = "1"
    orch = _build_orchestrator(project_root)
    summary = orch.run(fresh=True)
    assert summary["ok"] is True, summary
    extra = _state_yaml(project_root).get("extra", {})
    assert extra.get("stage_attempts", {}).get("generate") == 0


# --------------------------------------------------------------------------- #
# Sub-test 3: ALWAYS_FAIL + max_attempts=2 → pause (no infinite loop)
# --------------------------------------------------------------------------- #


ALWAYS_FAIL_YAML = SAMPLE_YAML.replace(
    "max_attempts: 3",
    "max_attempts: 2",
)


def test_always_fail_exhausts_attempts_and_pauses(
    project_root: Path, env_switch
) -> None:
    # Tighten max_attempts to 2 so the test is fast.
    (project_root / "novel-project.yaml").write_text(
        ALWAYS_FAIL_YAML, encoding="utf-8"
    )
    os.environ["NOVELFORGE_MOCK_ALWAYS_FAIL"] = "1"
    orch = _build_orchestrator(project_root)
    summary = orch.run(fresh=True)
    assert summary["paused"] is True, summary
    # First stage exhausted at attempts=2.
    extra = _state_yaml(project_root).get("extra", {})
    assert extra.get("stage_attempts", {}).get("generate") == 2


# --------------------------------------------------------------------------- #
# {{upstream.*}} expansion reaches the prompt
# --------------------------------------------------------------------------- #


def test_upstream_placeholder_reaches_prompt(project_root: Path, env_switch) -> None:
    """The second stage's prompt must contain the upstream outline body."""

    orch = _build_orchestrator(project_root)
    # Inject a canned outline body so we can recognise it in the
    # rendered prompt.
    from novelforge.claude.adapter import MockClaudeAdapter, MockResponse

    mock = MockClaudeAdapter()
    mock.set_response(
        "generate",
        MockResponse(
            output=(
                "# Outline\n\n## Chapter 1 - The Hero Awakens\n"
                "A young hero discovers a hidden power."
            ),
        ),
    )
    mock.set_response(
        "write", MockResponse(output="The chapter prose unfolds carefully. " * 5),
    )
    orch._adapter = mock  # type: ignore[attr-defined]
    summary = orch.run(fresh=True)
    assert summary["ok"] is True

    # The 'write' stage prompt must include the outline body (so
    # {{upstream.*}} was actually expanded).
    write_calls = [c for c in mock.calls if c["stage"] == "write"]
    assert write_calls, "no write-stage invoke recorded"
    write_prompt = write_calls[0]["prompt"]
    assert "## Chapter 1 - The Hero Awakens" in write_prompt, (
        f"upstream body missing from prompt; got: {write_prompt[:200]!r}"
    )


# --------------------------------------------------------------------------- #
# Sub-test 4: EMPTY on a JSON produce recovers via Tier C (spec §4.3)
# --------------------------------------------------------------------------- #


JSON_PIPELINE_YAML = """
novel:
  title: Sample
  genre: 玄幻修仙
  target_chapters: 1
  words_per_chapter: [100, 500]
  style: x
  seeds:
    - outline/premise.md
    - outline/world.md
  constraints:
    - CLAUDE.md
pipeline:
  stages:
    - id: review_chapter
      model: m
      prompt: Produce a JSON review of the chapter.
      produces:
        - path: output/review.json
          alias: report
      done_when:
        max_attempts: 3
        checks:
          - kind: json_field
            target: output/review.json
            field: passed
execution:
  retry:
    backoff: constant
    max_wait: 1
"""


def test_empty_json_produce_recovers_via_tier_c(
    project_root: Path, env_switch
) -> None:
    """An empty first invoke on a JSON produce must hit Tier C (retry)
    rather than Tier B (immediate pause).  Spec §4.3: "没写产物 /
    写残 → 整轮重跑带 attempt_hint"."""

    (project_root / "novel-project.yaml").write_text(
        JSON_PIPELINE_YAML, encoding="utf-8"
    )
    os.environ["NOVELFORGE_MOCK_EMPTY"] = "1"
    orch = _build_orchestrator(project_root)
    summary = orch.run(fresh=True)
    assert summary["ok"] is True, summary
    # The JSON produce was written by the second (recovered) invoke.
    report_path = project_root / "output" / "review.json"
    assert report_path.exists()
    # Attempts reset to 0 on success (Tier C retry happened then cleared).
    extra = _state_yaml(project_root).get("extra", {})
    assert extra.get("stage_attempts", {}).get("review_chapter") == 0


# --------------------------------------------------------------------------- #
# Independent cleanup between sub-tests
# --------------------------------------------------------------------------- #


def test_subtests_do_not_pollute_each_other(
    project_root: Path, env_switch
) -> None:
    """Run sub-test 1 then sub-test 3 — the second must still pause."""

    # Sub-test 1: NO_SIGNAL — recovers.
    _clean_runtime(project_root)
    os.environ["NOVELFORGE_MOCK_NO_SIGNAL"] = "1"
    orch1 = _build_orchestrator(project_root)
    s1 = orch1.run(fresh=True)
    assert s1["ok"] is True
    # Reset env + clean runtime before ALWAYS_FAIL run.
    _clean_runtime(project_root)
    del os.environ["NOVELFORGE_MOCK_NO_SIGNAL"]
    os.environ["NOVELFORGE_MOCK_ALWAYS_FAIL"] = "1"
    (project_root / "novel-project.yaml").write_text(
        ALWAYS_FAIL_YAML, encoding="utf-8"
    )
    orch2 = _build_orchestrator(project_root)
    s2 = orch2.run(fresh=True)
    assert s2["paused"] is True
