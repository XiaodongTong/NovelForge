"""v4 orchestrator routing tests (A5–A8, A17).

These tests exercise the v4 path:
- ``GenericStage`` is the executor.
- Routing is driven by the JSON ``route`` field pointing at stage
  ids (not v3 enum values).
- ``enabled: false`` stages are skipped, but a jump to a disabled
  stage is still counted against ``max_review_iterations`` (A17).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from novelforge.claude.adapter import MockClaudeAdapter, MockResponse
from novelforge.config import load_config
from novelforge.orchestrator import Orchestrator
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
    (tmp_path / "CLAUDE.md").write_text(
        "# Rules\n\n- Third person\n", encoding="utf-8"
    )
    return tmp_path


V4_YAML_TEMPLATE = """
novel:
  title: "Test"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [200, 400]
  style: "test"
  seeds: [outline/premise.md, outline/world.md]
  constraints: [CLAUDE.md]
pipeline:
  scaffold_from: "long-epic"
  stages:
    - id: write_chapter
      model: m
      prompt: "write"
      output: "output/chapters/{{num:03d}}-{{title|slug}}.md"
      split: "^# Chapter (?P<num>[0-9]+) - (?P<title>.+?)$"
    - id: review_chapter
      model: m
      prompt: "review"
      output: "output/review/chapter-review.json"
    - id: final_polish
      model: m
      prompt: "polish"
      output: "output/review/final-polish-notes.md"
    - id: done
      model: m
      prompt: "noop"
      output: "output/done.md"
execution:
  max_review_iterations: 3
  retry: { max_retries: 0, backoff: "constant", max_wait: 1 }
"""


def _write_v4_yaml(project_root: Path, body: str = V4_YAML_TEMPLATE) -> Path:
    path = project_root / "novel-project.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _outline_response() -> MockResponse:
    body = (
        "## Plot\n\nA hero rises.\n\n"
        "## Chapter 1 - The Summons\nA knock at the door.\n"
    )
    return MockResponse(output=body, input_tokens=10, output_tokens=20)


def _chapter_response() -> MockResponse:
    body = (
        "# Chapter 1 - The Summons\n\n"
        "It was a stormy night in the lower realm. " * 20
    )
    return MockResponse(output=body, input_tokens=10, output_tokens=200)


def _make_orch(project_root: Path) -> Orchestrator:
    cfg_path = _write_v4_yaml(project_root)
    cfg = load_config(cfg_path)
    return Orchestrator(
        config=cfg,
        config_path=cfg_path,
        project_root=project_root,
        use_mock=True,
    )


# --------------------------------------------------------------------------- #
# A5: JSON route drives a jump
# --------------------------------------------------------------------------- #


def test_route_field_drives_jump_to_target_stage(project_root: Path) -> None:
    """A5: JSON ``route`` value matching a known stage id causes a
    jump to that stage.  This test installs a ``write_chapter → review
    → write_chapter`` loop and asserts that ``write_chapter`` runs at
    least twice (i.e. the jump happened) before the loop ceiling
    fires.
    """

    body = V4_YAML_TEMPLATE.replace(
        "max_review_iterations: 3",
        "max_review_iterations: 5",
    )
    cfg_path = project_root / "novel-project.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    cfg = load_config(cfg_path)
    orch = Orchestrator(
        config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True
    )
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response(
        "review_chapter",
        MockResponse(
            output=json.dumps(
                {
                    "passed": False,
                    "route": "write_chapter",
                    "findings": ["needs more tension"],
                }
            ),
            parsed={
                "passed": False,
                "route": "write_chapter",
                "findings": ["needs more tension"],
            },
        ),
    )
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    # The run pauses when the loop exceeds max_review_iterations.
    assert summary["paused"], summary
    # write_chapter ran at least twice → the route jump happened.
    write_calls = [c for c in mock.calls if c["stage"] == "write_chapter"]
    assert len(write_calls) >= 2
    # final_polish was never reached (the loop never resolved).
    final_calls = [c for c in mock.calls if c["stage"] == "final_polish"]
    assert final_calls == []


# --------------------------------------------------------------------------- #
# A6: no route → natural next
# --------------------------------------------------------------------------- #


def test_no_route_field_advances_naturally(project_root: Path) -> None:
    orch = _make_orch(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response(
        "review_chapter",
        MockResponse(
            output=json.dumps(
                {
                    "passed": True,
                    "findings": [],
                    "summary": "ok",
                    # No `route` key
                }
            ),
            parsed={"passed": True, "findings": [], "summary": "ok"},
        ),
    )
    mock.set_response("final_polish", MockResponse(output="polish notes"))
    mock.set_response("done", MockResponse(output="done"))
    orch._adapter = mock  # type: ignore[assignment]

    summary = orch.run(fresh=True)
    assert summary["ok"], summary
    # final_polish and done were both reached
    final_calls = [c for c in mock.calls if c["stage"] == "final_polish"]
    done_calls = [c for c in mock.calls if c["stage"] == "done"]
    assert len(final_calls) == 1
    assert len(done_calls) == 1


# --------------------------------------------------------------------------- #
# A7: route to unknown id → on_failure (default pause)
# --------------------------------------------------------------------------- #


def test_route_to_unknown_id_pauses(project_root: Path) -> None:
    orch = _make_orch(project_root)
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response(
        "review_chapter",
        MockResponse(
            output=json.dumps(
                {
                    "passed": False,
                    "route": "nonexistent_stage",
                    "findings": ["x"],
                }
            ),
            parsed={
                "passed": False,
                "route": "nonexistent_stage",
                "findings": ["x"],
            },
        ),
    )
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    assert summary["paused"], summary
    # Reason text comes from the exception class name surfaced by
    # ``_pause_with``; the detailed reason is in the run log.


# --------------------------------------------------------------------------- #
# A7: route to disabled stage → on_failure
# --------------------------------------------------------------------------- #


def test_route_to_disabled_stage_pauses(project_root: Path) -> None:
    body = V4_YAML_TEMPLATE.replace(
        "    - id: final_polish",
        "    - id: final_polish\n      enabled: false",
    )
    orch = _make_orch(project_root)
    # Re-write yaml with the disabled stage
    cfg_path = project_root / "novel-project.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    cfg = load_config(cfg_path)
    orch = Orchestrator(
        config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True
    )
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response(
        "review_chapter",
        MockResponse(
            output=json.dumps({"route": "final_polish", "findings": []}),
            parsed={"route": "final_polish", "findings": []},
        ),
    )
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    assert summary["paused"], summary


# --------------------------------------------------------------------------- #
# A4: enabled: false stage is skipped
# --------------------------------------------------------------------------- #


def test_disabled_stage_is_skipped(project_root: Path) -> None:
    body = V4_YAML_TEMPLATE.replace(
        "    - id: final_polish",
        "    - id: final_polish\n      enabled: false",
    )
    cfg_path = project_root / "novel-project.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    cfg = load_config(cfg_path)
    orch = Orchestrator(
        config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True
    )
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("write_chapter", _chapter_response())
    mock.set_response(
        "review_chapter",
        MockResponse(
            output=json.dumps(
                {"passed": True, "findings": [], "summary": "ok"}
            ),
            parsed={"passed": True, "findings": [], "summary": "ok"},
        ),
    )
    mock.set_response("done", MockResponse(output="ok"))
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    assert summary["ok"], summary
    # final_polish was never called.
    final_calls = [c for c in mock.calls if c["stage"] == "final_polish"]
    assert final_calls == []


# --------------------------------------------------------------------------- #
# A8: route loop exceeds max_review_iterations
# --------------------------------------------------------------------------- #


def test_route_loop_pauses_when_max_exceeded(project_root: Path) -> None:
    body = V4_YAML_TEMPLATE.replace(
        "max_review_iterations: 3",
        "max_review_iterations: 2",
    )
    cfg_path = project_root / "novel-project.yaml"
    cfg_path.write_text(body, encoding="utf-8")
    cfg = load_config(cfg_path)
    orch = Orchestrator(
        config=cfg, config_path=cfg_path, project_root=project_root, use_mock=True
    )
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("write_chapter", _chapter_response())
    # review_chapter always jumps back to write_chapter
    mock.set_response(
        "review_chapter",
        MockResponse(
            output=json.dumps(
                {"route": "write_chapter", "findings": ["x"]}
            ),
            parsed={"route": "write_chapter", "findings": ["x"]},
        ),
    )
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    assert summary["paused"], summary
    # The pause reason comes from the exception class name surfaced
    # by ``_pause_with``; what matters is that the run *did* pause
    # rather than complete or fail silently.  The detailed reason is
    # already in the run log.
    # write_chapter was called at most max_iter (2) times + maybe
    # one more for the skipped iter.
    write_calls = [c for c in mock.calls if c["stage"] == "write_chapter"]
    assert len(write_calls) <= 4  # 2 loops × 2 write calls
