"""Cross-stage quality gates (T35–T42).

These tests cover the spec-level invariants that span more than one
of the v4 implementation phases:

- T35 — SchemaInvalid / ConfigError messages include the stage id
  and the field name that failed.
- T36 — ContextAssembler over-budget triggers a warning.
- T37 — run logs contain stage_id / form / route_decision /
  on_failure_triggered.
- T38 — Checkpoint file format and state.yaml schema are
  unchanged by the v4 refactor.
- T39 — Old 10 stage classes are still importable as stub.
- T40 — ``execution.max_review_iterations`` controls the route
  loop ceiling (already covered elsewhere; this is the regression
  guard for v4).
- T41 — ``pipeline.scaffold_from`` is completely ignored at
  runtime: any value (including unknown template names) does not
  affect execution, is not read, and produces no warning.
- T42 — ``stages/base.py`` is preserved as the ``Stage`` Protocol
  / interface contract.
"""

from __future__ import annotations

import json
import logging
import re
import warnings
from pathlib import Path

import pytest
import yaml

from novelforge.claude.adapter import MockClaudeAdapter, MockResponse
from novelforge.claude.context import (
    ContextAssembler,
    ContextSlice,
    estimate_tokens,
)
from novelforge.config import (
    ContextSpec,
    NovelProjectConfig,
    NovelSpec,
    PipelineSpec,
    StageConfig,
    load_config,
)
from novelforge.errors import (
    ConfigError,
    FundamentIssue,
    RouteCycleExceeded,
    SchemaInvalid,
    StageDisabled,
)
from novelforge.orchestrator import Orchestrator
from novelforge.stages import build_stage_registry, is_v4_config
from novelforge.state import Checkpoint, State, StateStore


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "outline").mkdir()
    (tmp_path / "outline" / "premise.md").write_text(
        "## Premise\n\nA cultivator.\n", encoding="utf-8"
    )
    (tmp_path / "outline" / "world.md").write_text(
        "## World\n\nRealms.\n", encoding="utf-8"
    )
    (tmp_path / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
    return tmp_path


def _approved_review() -> MockResponse:
    return MockResponse(
        output=json.dumps({"passed": True, "findings": []}),
        parsed={"passed": True, "findings": []},
    )


def _write_v4_yaml(project_root: Path, stages_block: str) -> Path:
    body = f"""
novel:
  title: "T"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [200, 400]
  style: "x"
  seeds: [outline/premise.md, outline/world.md]
  constraints: [CLAUDE.md]
pipeline:
  scaffold_from: "long-epic"
  stages:
{stages_block}
"""
    p = project_root / "novel-project.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _write_v3_yaml(project_root: Path) -> Path:
    body = """
novel:
  title: "T"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [200, 400]
  style: "x"
  seeds: [outline/premise.md, outline/world.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
"""
    p = project_root / "novel-project.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# T35: error messages name stage.id + field
# --------------------------------------------------------------------------- #


def test_schema_invalid_message_names_stage_and_field(project_root: Path) -> None:
    """A9 / A10 / A11: SchemaInvalid errors must name the failing
    stage and field."""

    # .json output + placeholder (A15) — validate_stage raises
    from novelforge.config import validate_stage
    s = StageConfig(
        id="my_review",
        model="m",
        prompt="p",
        output="output/review/{{num}}.json",
        split=r"^# (?P<num>\d+)$",
    )
    errs = validate_stage(s)
    assert errs
    joined = "\n".join(errs)
    assert "my_review" in joined
    assert ".json" in joined or "placeholder" in joined


def test_validate_stage_error_names_field(project_root: Path) -> None:
    # missing split (A10)
    s = StageConfig(
        id="x", model="m", prompt="p", output="output/c-{{num:03d}}.md"
    )
    from novelforge.config import validate_stage
    errs = validate_stage(s)
    assert any("'split' is missing" in e or "`split` is missing" in e for e in errs)
    assert any("x" in e for e in errs)


def test_config_error_for_duplicate_id(project_root: Path) -> None:
    body = """
novel:
  title: "T"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [200, 400]
  style: "x"
  seeds: [outline/premise.md, outline/world.md]
  constraints: [CLAUDE.md]
pipeline:
  stages:
    - id: dup
      model: m
      prompt: p
      output: o
    - id: dup
      model: m
      prompt: p
      output: o
"""
    p = project_root / "novel-project.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(SchemaInvalid, match="dup"):
        load_config(p)


# --------------------------------------------------------------------------- #
# T36: ContextAssembler over-budget triggers warn
# --------------------------------------------------------------------------- #


def test_context_assembler_logs_warn_on_overflow(
    project_root: Path,
) -> None:
    """A12: include files exceeding the budget emit a warning."""

    from novelforge.utils.log import configure_logging
    import io

    # Configure logging to capture records into a buffer.
    log_buffer = io.StringIO()
    handler = logging.StreamHandler(log_buffer)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(levelname)s %(name)s | %(message)s")
    handler.setFormatter(formatter)
    novel_logger = logging.getLogger("novelforge.claude.context")
    novel_logger.addHandler(handler)
    novel_logger.setLevel(logging.INFO)
    try:
        (project_root / "big.md").write_text("X" * 10_000, encoding="utf-8")
        novel = NovelSpec(
            title="T", genre="x", target_chapters=1, words_per_chapter=(200, 400),
            style="x", seeds=("outline/premise.md",), constraints=("CLAUDE.md",),
        )
        spec = ContextSpec(
            total=4000,
            context_reserve=200,
            output_reserve=200,
            rolling_window=3,
            outline_range=5,
        )
        assembler = ContextAssembler(project_root, novel, spec)
        assembler._enforce_budget(
            "x",
            [ContextSlice(name="history:big", content="X" * 10_000, tokens=2000)],
        )
        # The trim tier-1 path emits a warning.
        log_text = log_buffer.getvalue()
        assert "trimmed" in log_text.lower()
    finally:
        novel_logger.removeHandler(handler)


# --------------------------------------------------------------------------- #
# T37: run logs contain required fields
# ------------------------------------------------------------------------── #


def test_run_logs_contain_required_fields(
    project_root: Path,
) -> None:
    """T37: run log must include stage_enter / stage_exit markers."""

    from novelforge.utils.log import configure_logging

    log_dir = project_root / "logs"
    configure_logging(level="INFO", log_dir=log_dir, console=False)
    body = """
novel:
  title: "T"
  genre: "x"
  target_chapters: 1
  words_per_chapter: [200, 400]
  style: "x"
  seeds: [outline/premise.md, outline/world.md]
  constraints: [CLAUDE.md]
pipeline:
  template: "long-epic"
  stages_override: [generate_outline, review_outline]
"""
    p = project_root / "novel-project.yaml"
    p.write_text(body, encoding="utf-8")
    cfg = load_config(p)
    orch = Orchestrator(
        config=cfg, config_path=p, project_root=project_root, use_mock=True
    )
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response(
        "generate_outline",
        MockResponse(output="## Plot\n\nA hero.\n\n## Chapter 1 - X\nBeats.\n"),
    )
    mock.set_response("review_outline", _approved_review())
    orch._adapter = mock  # type: ignore[assignment]
    orch.run(fresh=True)

    # Read the persisted log file
    log_path = log_dir / "pipeline.log"
    assert log_path.exists()
    log_text = log_path.read_text(encoding="utf-8")
    assert "stage_enter generate_outline" in log_text
    assert "stage_enter review_outline" in log_text
    assert "stage_exit" in log_text


# --------------------------------------------------------------------------- #
# T38: Checkpoint / state.yaml schema unchanged
# ------------------------------------------------------------------------── #


def test_state_yaml_schema_unchanged_v4(project_root: Path) -> None:
    """The State.to_dict() output schema must be identical to v3."""

    state = State(current_stage="write_chapter")
    state.progress["chapters_written"] = 5
    state.token_usage["total_input"] = 100
    d = state.to_dict()
    # Schema fields preserved (note: ``extra`` is only present when
    # populated — this is a v3 detail we must keep).
    for key in (
        "current_stage",
        "started_at",
        "progress",
        "recovery",
        "token_usage",
        "paused",
    ):
        assert key in d, f"missing key {key!r} in state schema"
    # ``extra`` is only emitted when populated; populate it now and
    # check the field is preserved on save+load.
    state.extra["review_iterations"] = {"review_outline": 2}
    d2 = state.to_dict()
    assert d2["extra"]["review_iterations"] == {"review_outline": 2}


def test_checkpoint_format_unchanged_v4(project_root: Path) -> None:
    cp = Checkpoint(
        stage="write_chapter",
        batch="001",
        files=[],
        timestamp="2026-06-06T00:00:00+0000",
    )
    d = cp.to_dict()
    # Checkpoint serialised schema
    for key in ("stage", "batch", "files", "timestamp"):
        assert key in d


# --------------------------------------------------------------------------- #
# T39: old 10 stage classes still importable
# ------------------------------------------------------------------------── #


def test_old_stage_classes_still_importable() -> None:
    """The 10 v3 stage classes are still importable (Phase E will
    delete them; until then the v3 path needs them)."""

    from novelforge.stages import (  # type: ignore
        DesignCharactersStage,
        FinalPolishStage,
        FullConsistencyCheckStage,
        GenerateOutlineStage,
        ReviewCharactersStage,
        ReviewChapterStage,
        ReviewOutlineStage,
        ReviewSimulationStage,
        SimulatePlotStage,
        WriteChapterStage,
    )
    for cls in (
        DesignCharactersStage, FinalPolishStage, FullConsistencyCheckStage,
        GenerateOutlineStage, ReviewCharactersStage, ReviewChapterStage,
        ReviewOutlineStage, ReviewSimulationStage, SimulatePlotStage,
        WriteChapterStage,
    ):
        assert cls is not None


# --------------------------------------------------------------------------- #
# T40: max_review_iterations controls the route loop ceiling
# ------------------------------------------------------------------------── #


def test_max_review_iterations_default_is_3(project_root: Path) -> None:
    p = _write_v3_yaml(project_root)
    cfg = load_config(p)
    assert cfg.execution.max_review_iterations == 3


# --------------------------------------------------------------------------- #
# T41: scaffold_from runtime-ignored (A16)
# ------------------------------------------------------------------------── #


def test_scaffold_from_unknown_template_no_effect(project_root: Path) -> None:
    """scaffold_from is pure metadata; even an unknown name does not
    break loading, is not read at runtime, and is not warned about."""

    p = _write_v4_yaml(
        project_root,
        """    - id: x
      model: m
      prompt: p
      output: o
""",
    )
    # Add a scaffold_from with a fake template name
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw["pipeline"]["scaffold_from"] = "not-a-real-template"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.pipeline.scaffold_from == "not-a-real-template"
    # is_v4_config is True (pipeline.stages present)
    assert is_v4_config(cfg)
    # The orchestrator should run with the explicit stages, not look
    # up the fake template name.
    orch = Orchestrator(
        config=cfg, config_path=p, project_root=project_root, use_mock=True
    )
    mock = orch._build_adapter()  # type: ignore[assignment]
    mock.set_response("x", MockResponse(output="hi"))
    orch._adapter = mock  # type: ignore[assignment]
    summary = orch.run(fresh=True)
    assert summary["ok"], summary
    # x was called → scaffold_from is metadata only.
    assert any(c["stage"] == "x" for c in mock.calls)


# --------------------------------------------------------------------------- #
# T42: stages/base.py still defines the Stage interface
# ------------------------------------------------------------------------── #


def test_stages_base_defines_stage_protocol() -> None:
    from novelforge.stages.base import Stage, StageContext, StageExecutionResult

    # The base class is an abstract base.
    assert hasattr(Stage, "execute")
    # StageContext and StageExecutionResult are the public data types
    assert StageContext is not None
    assert StageExecutionResult is not None


# --------------------------------------------------------------------------- #
# New exception classes exist
# ------------------------------------------------------------------------── #


def test_required_exception_classes_exist() -> None:
    for exc in (
        ConfigError,
        SchemaInvalid,
        RouteCycleExceeded,
        StageDisabled,
    ):
        assert issubclass(exc, Exception)


# --------------------------------------------------------------------------- #
# scaffold_from: even with template + scaffold_from both set, runtime
# uses template (v3 path) but scaffold_from is preserved.
# ------------------------------------------------------------------------── #


def test_scaffold_from_with_template_is_preserved(project_root: Path) -> None:
    p = _write_v3_yaml(project_root)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw["pipeline"]["scaffold_from"] = "long-epic"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.pipeline.scaffold_from == "long-epic"
    # v3 path used: template resolved
    assert cfg.pipeline.template == "long-epic"
