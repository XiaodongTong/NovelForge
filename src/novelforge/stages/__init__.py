"""Stage package — v4 single-class stage registry.

v4 unifies the ten v3 stage classes behind :class:`GenericStage`
(spec §5.1 / T11–T14).  The legacy per-stage classes are still
importable from this package for backward compatibility (T15) but
they are deprecated and will be removed in Stage E.

The orchestrator dispatches to either the legacy v3 stage classes
(when the user supplied a v3 yaml) or :class:`GenericStage` (when the
user supplied a v4 ``pipeline.stages`` list).  The detection lives in
:func:`is_v4_config` so the orchestrator and any helper can share
the same rule.
"""

from __future__ import annotations

import warnings
from typing import Any, Mapping, Optional

from ..claude.adapter import ClaudeAdapter
from ..config import NovelProjectConfig
from ..review.gate import ReviewGate
from .base import Stage
from .design_characters import DesignCharactersStage
from .final_polish import FinalPolishStage
from .full_consistency_check import FullConsistencyCheckStage
from .generate_outline import GenerateOutlineStage
from .generic import GenericStage
from .review_characters import ReviewCharactersStage
from .review_chapter import ReviewChapterStage
from .review_outline import ReviewOutlineStage
from .review_simulation import ReviewSimulationStage
from .simulate_plot import SimulatePlotStage
from .write_chapter import WriteChapterStage


__deprecated_stage_classes__ = (
    "DesignCharactersStage",
    "FinalPolishStage",
    "FullConsistencyCheckStage",
    "GenerateOutlineStage",
    "ReviewCharactersStage",
    "ReviewChapterStage",
    "ReviewOutlineStage",
    "ReviewSimulationStage",
    "SimulatePlotStage",
    "WriteChapterStage",
)


def is_v4_config(cfg: NovelProjectConfig) -> bool:
    """Return ``True`` when ``cfg`` was loaded from a v4 yaml.

    Detection rule: the source yaml contained a ``pipeline.stages``
    list (so the StageConfig records are user-authored, not just
    synthesised from ``template:``).  Pure v3 yaml (``template`` only,
    with optional ``stages_override``) is treated as the legacy path.
    """

    raw = cfg.raw if isinstance(cfg.raw, Mapping) else {}
    pipeline = raw.get("pipeline") if isinstance(raw, Mapping) else None
    if not isinstance(pipeline, Mapping):
        return False
    return pipeline.get("stages") is not None


def build_stage_registry(
    adapter: ClaudeAdapter, gate: ReviewGate
) -> dict[str, Stage]:
    """Return the stage registry for the v3 (legacy) path.

    The orchestrator still calls this in the v3 path.  The returned
    dict is keyed by stage id so ``registry.get(stage_id)`` keeps
    working.  v4 yaml uses :class:`GenericStage` instead — see
    :func:`is_v4_config`.
    """

    return {
        "generate_outline": GenerateOutlineStage(adapter),
        "review_outline": ReviewOutlineStage(adapter, gate),
        "design_characters": DesignCharactersStage(adapter),
        "review_characters": ReviewCharactersStage(adapter, gate),
        "simulate_plot": SimulatePlotStage(adapter),
        "review_simulation": ReviewSimulationStage(adapter, gate),
        "write_chapter": WriteChapterStage(adapter),
        "review_chapter": ReviewChapterStage(adapter, gate),
        "full_consistency_check": FullConsistencyCheckStage(adapter),
        "final_polish": FinalPolishStage(adapter),
    }


def build_v4_stage(adapter: ClaudeAdapter) -> GenericStage:
    """Return a singleton :class:`GenericStage` wired to ``adapter``.

    The orchestrator uses this in the v4 path; one instance is shared
    across all stages because the per-step behaviour comes from
    :class:`StageConfig` (passed via :class:`StageContext`).
    """

    return GenericStage(adapter)


__all__ = [
    "GenericStage",
    "Stage",
    "build_stage_registry",
    "build_v4_stage",
    "is_v4_config",
    *list(__deprecated_stage_classes__),
]
