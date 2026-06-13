"""Exception hierarchy for the NovelForge engine.

The error model follows ``plan.md`` §4.7. Each public error class corresponds
to a row in the recovery strategy matrix and can be caught explicitly by
the orchestrator.
"""

from __future__ import annotations

from typing import Optional


class NovelForgeError(Exception):
    """Base class for all engine errors."""


# -- Config / Setup --------------------------------------------------------


class ConfigError(NovelForgeError):
    """Invalid or missing configuration."""


# -- Execution layer -------------------------------------------------------


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


# -- v4 output / route -----------------------------------------------------


class OutputParseError(NovelForgeError):
    """A stage's raw output could not be parsed in the declared form.

    Used by :mod:`novelforge.claude.output_parser` for split-regex
    misses, JSON object extraction failures, etc.
    """


class RouteCycleExceeded(NovelForgeError):
    """The route loop hit ``execution.max_review_iterations`` (v4 name)."""


class StageDisabled(NovelForgeError):
    """A ``route`` value pointed at a stage with ``enabled: false`` (v4)."""


# -- Persistence -----------------------------------------------------------


class CheckpointCorrupt(NovelForgeError):
    """A checkpoint failed integrity verification."""

    def __init__(self, message: str, *, path: Optional[str] = None, reason: str = "") -> None:
        super().__init__(message)
        self.path = path
        self.reason = reason


class StateError(NovelForgeError):
    """``state.yaml`` is missing required fields or cannot be parsed."""


# -- Review / Routing ------------------------------------------------------


class ReviewLoopExceeded(NovelForgeError):
    """The configured ``max_review_iterations`` was reached for a stage."""


class FundamentIssue(NovelForgeError):
    """The review gate returned ``FUNDAMENTAL_ISSUE`` and the engine paused."""


# -- v4 status / progress --------------------------------------------------


# v4 aliases the v3 ``ReviewLoopExceeded`` to the new name
# ``RouteCycleExceeded``.  Both names are exported so existing
# ``except`` blocks keep working after the refactor.
ReviewLoopExceeded.__name__ = "ReviewLoopExceeded"
ReviewLoopExceeded.__qualname__ = "ReviewLoopExceeded"
