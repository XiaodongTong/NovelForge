"""Exception hierarchy for the NovelForge engine.

The error model follows ``plan.md`` §4.7. Each public error class corresponds
to a row in the recovery strategy matrix and can be caught explicitly by
the orchestrator.

The four-tier error matrix (spec §4.3) maps to:

- **Tier A — Infrastructure** (retryable, same prompt, backoff):
  :class:`RateLimited`, :class:`WriteFailure`, :class:`ContextOverflow`
- **Tier B — Model format error** (non-retryable, on_failure):
  :class:`SchemaInvalid`, :class:`OutputParseError`
- **Tier C — Model incomplete** (whole-stage retry with attempt_hint):
  :class:`StageIncomplete`, :class:`VerifyFailed`
- **Tier D — Model semantic error** (review gate loop):
  handled outside the stage pipeline
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class NovelForgeError(Exception):
    """Base class for all engine errors."""


# -- Config / Setup --------------------------------------------------------


class ConfigError(NovelForgeError):
    """Invalid or missing configuration."""


# -- Execution layer (Tier A: infrastructure) ------------------------------


class WriteFailure(NovelForgeError):
    """Claude invocation failed in a retryable way (network, timeout, 5xx)."""


class RateLimited(NovelForgeError):
    """Anthropic rate limit hit (HTTP 429)."""


class CLIError(NovelForgeError):
    """Claude Code CLI exited non-zero or produced unparseable output."""

    def __init__(self, message: str, exit_code: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class ContextOverflow(NovelForgeError):
    """The assembled context exceeded the configured budget."""


class SchemaInvalid(NovelForgeError):
    """The model returned output that did not match the review JSON schema."""


class OutputParseError(NovelForgeError):
    """A stage's raw output could not be parsed in the declared form.

    Used by :mod:`novelforge.claude.output_parser` for split-regex
    misses, JSON object extraction failures, etc.
    """


# -- Tier C: Model incomplete (whole-stage retry) --------------------------


class StageIncomplete(NovelForgeError):
    """The model did not emit the declared completion signal (tier C).

    Raised by :class:`novelforge.stages.generic.GenericStage` when the
    first-layer protocol (``done_when.completion_signal``) detects the
    completion marker is missing from the model's stdout.  The
    orchestrator catches this and retries the whole stage with an
    ``attempt_hint`` injected into the prompt, up to
    ``done_when.max_attempts``.
    """

    def __init__(
        self,
        message: str,
        *,
        stage_id: Optional[str] = None,
        attempt: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.stage_id = stage_id
        self.attempt = attempt


class VerifyFailed(NovelForgeError):
    """A ``done_when.checks`` entry failed (tier C, second layer).

    Carries the structured details (target / kind / expected / actual)
    so the orchestrator can build a machine-readable ``attempt_hint``
    for the next retry.
    """

    def __init__(
        self,
        message: str,
        *,
        stage_id: Optional[str] = None,
        attempt: Optional[int] = None,
        target: Optional[str] = None,
        kind: Optional[str] = None,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        super().__init__(message)
        self.stage_id = stage_id
        self.attempt = attempt
        self.target = target
        self.kind = kind
        self.expected = expected
        self.actual = actual

    @property
    def detail(self) -> Mapping[str, Any]:
        return {
            "stage_id": self.stage_id,
            "target": self.target,
            "kind": self.kind,
            "expected": self.expected,
            "actual": self.actual,
        }


# -- Routing / on_failure dispositions -------------------------------------


class RouteCycleExceeded(NovelForgeError):
    """A route loop hit ``execution.max_review_iterations``."""


class StageDisabled(NovelForgeError):
    """A ``route`` value pointed at a stage with ``enabled: false``."""


# -- Persistence -----------------------------------------------------------


class CheckpointCorrupt(NovelForgeError):
    """A checkpoint failed integrity verification."""

    def __init__(self, message: str, *, path: Optional[str] = None, reason: str = "") -> None:
        super().__init__(message)
        self.path = path
        self.reason = reason


class StateError(NovelForgeError):
    """``state.yaml`` is missing required fields or cannot be parsed."""


__all__ = [
    "NovelForgeError",
    "ConfigError",
    "WriteFailure",
    "RateLimited",
    "CLIError",
    "ContextOverflow",
    "SchemaInvalid",
    "OutputParseError",
    "StageIncomplete",
    "VerifyFailed",
    "RouteCycleExceeded",
    "StageDisabled",
    "CheckpointCorrupt",
    "StateError",
]
