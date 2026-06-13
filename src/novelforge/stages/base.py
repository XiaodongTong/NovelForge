"""Stage abstract base class and shared data types."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence

from ..claude.adapter import StageResult as AdapterStageResult
from ..config import NovelProjectConfig
from ..utils.log import get_logger

log = get_logger("stages.base")


@dataclass
class StageContract:
    """Inputs/outputs/retry contract for a stage.

    The orchestrator reads this to know which files the stage will
    produce so it can record their hashes in the checkpoint.
    """

    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    max_retries: int = 1


@dataclass
class StageExecutionResult:
    """Public result object returned by a stage's :meth:`execute` method.

    It is the union of the adapter result and any side-effect bookkeeping
    the stage performed (files written, batch numbers, etc.).
    """

    stage_id: str
    raw_output: str = ""
    files: list[Path] = field(default_factory=list)
    batch: Optional[str] = None
    route: str = "APPROVED"
    findings: list[str] = field(default_factory=list)
    token_usage_in: int = 0
    token_usage_out: int = 0
    duration: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_adapter(
        cls,
        stage_id: str,
        adapter_result: AdapterStageResult,
        *,
        files: Iterable[Path] = (),
        batch: Optional[str] = None,
        route: str = "APPROVED",
        findings: Optional[list[str]] = None,
        extras: Optional[dict[str, Any]] = None,
    ) -> "StageExecutionResult":
        return cls(
            stage_id=stage_id,
            raw_output=adapter_result.raw_output,
            files=list(files),
            batch=batch,
            route=route,
            findings=list(findings or []),
            token_usage_in=adapter_result.token_usage.input_tokens,
            token_usage_out=adapter_result.token_usage.output_tokens,
            duration=adapter_result.duration,
            extras=dict(extras or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "batch": self.batch,
            "route": self.route,
            "findings": list(self.findings),
            "token_usage": {
                "input": self.token_usage_in,
                "output": self.token_usage_out,
            },
            "duration": self.duration,
            "files": [str(p) for p in self.files],
            "extras": dict(self.extras),
        }


class StageContext:
    """Read-only context passed into a stage's :meth:`execute`.

    Bundles everything a stage might need without giving it the power to
    mutate shared state directly — the orchestrator owns state writes.
    """

    def __init__(
        self,
        *,
        config: NovelProjectConfig,
        project_root: Path,
        stage_id: str,
        batch: Optional[str] = None,
        chapter_index: Optional[int] = None,
        extras: Optional[dict[str, Any]] = None,
    ) -> None:
        self.config = config
        self.project_root = Path(project_root)
        self.stage_id = stage_id
        self.batch = batch
        self.chapter_index = chapter_index
        self.extras = dict(extras or {})


class Stage(abc.ABC):
    """Abstract stage.  Subclasses implement :meth:`execute`."""

    id: str = ""
    name: str = ""
    contract: StageContract = StageContract()

    @abc.abstractmethod
    def execute(self, ctx: StageContext) -> StageExecutionResult:
        """Run the stage and return a structured result.

        The orchestrator uses ``ctx`` to find inputs; the stage itself
        is responsible for invoking the Claude adapter and writing any
        output files.
        """

        raise NotImplementedError


__all__ = [
    "Stage",
    "StageContext",
    "StageContract",
    "StageExecutionResult",
]
