"""Stage package — single :class:`GenericStage` executor.

v4 (spec §4) collapses every per-stage class behind
:class:`GenericStage`.  The per-stage behaviour is fully described by
the :class:`novelforge.config.StageConfig` record passed via
:class:`StageContext.extras['stage_config']`, so a single class drives
every step of the pipeline.
"""

from __future__ import annotations

from ..claude.adapter import ClaudeAdapter
from .base import Stage
from .generic import GenericStage

__all__ = ["GenericStage", "Stage", "build_v4_stage"]


def build_v4_stage(adapter: ClaudeAdapter) -> GenericStage:
    """Return a singleton :class:`GenericStage` wired to ``adapter``.

    The orchestrator uses this in the v4 path; one instance is shared
    across all stages because the per-step behaviour comes from
    :class:`StageConfig` (passed via :class:`StageContext`).
    """

    return GenericStage(adapter)
