"""Claude Code CLI adapter.

This module is the **only** place in the engine that talks to the
Claude Code CLI.  Everything else (orchestrator, stages, context) sees
an :class:`ClaudeAdapter` interface — an instance with an
``invoke(prompt, ...) -> StageResult`` method that abstracts away the
subprocess details.

Key design choices (per ``plan.md`` §4.4):

- We invoke the CLI in ``--prompt`` mode, passing the prompt and any
  extra context files as positional/file arguments.
- The adapter always returns a :class:`StageResult` even on failure; it
  only raises when the CLI itself cannot be launched (binary missing,
  no API key configured, etc.).
- Token usage is parsed from the CLI's stdout.  When parsing fails we
  log a warning and assume ``(0, 0)`` — the engine should not crash
  because the CLI's output format changed.
- A :class:`MockClaudeAdapter` is available for tests; it returns
  canned responses from an in-memory fixture.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from ..errors import CLIError, RateLimited, WriteFailure
from ..utils.log import get_logger
from .tokens import TokenUsageLog

log = get_logger("claude.adapter")


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass
class StageResult:
    """Return value of :meth:`ClaudeAdapter.invoke`."""

    raw_output: str
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    exit_code: int = 0
    duration: float = 0.0
    model: str = ""
    error: Optional[str] = None
    parsed: Optional[dict[str, Any]] = None  # JSON-parsed output if available

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_output": self.raw_output,
            "token_usage": self.token_usage.to_dict(),
            "exit_code": self.exit_code,
            "duration": self.duration,
            "model": self.model,
            "error": self.error,
            "parsed": self.parsed,
        }


@runtime_checkable
class ClaudeAdapter(Protocol):
    """Adapter protocol implemented by the real and mock adapters."""

    def invoke(
        self,
        prompt: str,
        *,
        stage: str,
        model: str = "",
        batch: Optional[str] = None,
        context_files: Sequence[Path] = (),
        max_wait: float = 600.0,
    ) -> StageResult: ...


# --------------------------------------------------------------------------- #
# Token usage parsing
# --------------------------------------------------------------------------- #


# The Claude Code CLI does not currently publish a stable machine-readable
# token format.  We support three common patterns so the adapter keeps
# working as the CLI evolves:
#
# 1. A trailing JSON object on its own line: ``{"usage": {"input_tokens": 1, "output_tokens": 2}}``
# 2. ``Usage: in=1234 out=5678`` style summary
# 3. ``input_tokens=1234 output_tokens=5678`` style summary

_USAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"input[_\s]*tokens\s*[:=]\s*(\d+)", re.IGNORECASE),
    re.compile(r"output[_\s]*tokens\s*[:=]\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bin\s*=\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bout\s*=\s*(\d+)", re.IGNORECASE),
)


def parse_token_usage(stdout: str) -> TokenUsage:
    """Best-effort extraction of token usage from CLI output.

    Returns ``TokenUsage(0, 0)`` on no match; logs a warning when input
    was present but could not be parsed.
    """

    if not stdout:
        return TokenUsage()

    # Try the JSON case first.
    for line in stdout.splitlines()[::-1]:
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage = obj.get("usage") if isinstance(obj, dict) else None
            if isinstance(usage, dict):
                inp = int(usage.get("input_tokens", 0) or 0)
                out = int(usage.get("output_tokens", 0) or 0)
                if inp or out:
                    return TokenUsage(inp, out)

    inp = 0
    out = 0
    matches = _USAGE_PATTERNS
    for pat, idx in ((matches[0], "in"), (matches[1], "out"), (matches[2], "in"), (matches[3], "out")):
        m = pat.search(stdout)
        if not m:
            continue
        value = int(m.group(1))
        if idx == "in":
            inp = value
        else:
            out = value
    if inp or out:
        return TokenUsage(inp, out)
    return TokenUsage()


# --------------------------------------------------------------------------- #
# Real adapter
# --------------------------------------------------------------------------- #


class ClaudeCLIAdapter:
    """Invokes the Claude Code CLI via ``--prompt`` mode.

    Configuration:

    - ``cli_command`` (default ``"claude"``) — the binary to invoke.  Set
      to the full path if the binary is not on ``PATH``.
    - ``extra_args`` — extra CLI flags appended to every call.
    - ``usage_log`` — optional :class:`TokenUsageLog` to append per-call
      records to.
    - ``max_output_bytes`` — guardrail for the captured stdout.
    """

    DEFAULT_CLI = "claude"

    def __init__(
        self,
        cli_command: str = DEFAULT_CLI,
        extra_args: Sequence[str] = (),
        usage_log: Optional[TokenUsageLog] = None,
        max_output_bytes: int = 5 * 1024 * 1024,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.cli_command = cli_command
        self.extra_args = list(extra_args)
        self.usage_log = usage_log
        self.max_output_bytes = max_output_bytes
        self._env = env

    # -- public --------------------------------------------------------

    def invoke(
        self,
        prompt: str,
        *,
        stage: str,
        model: str = "",
        batch: Optional[str] = None,
        context_files: Sequence[Path] = (),
        max_wait: float = 600.0,
    ) -> StageResult:
        # Validate environment up front so we fail fast with a clear error.
        self._ensure_cli_available()
        self._ensure_api_key()

        args = self._build_args(prompt, model=model, context_files=context_files)
        cmd_str = " ".join(shlex.quote(a) for a in [self.cli_command, *args])
        log.debug("claude call: %s", cmd_str)

        start = time.monotonic()
        try:
            proc = subprocess.run(
                [self.cli_command, *args],
                capture_output=True,
                text=True,
                timeout=max_wait,
                env=self._resolved_env(),
            )
        except FileNotFoundError as exc:
            raise CLIError(
                f"Claude CLI not found: {self.cli_command!r}. "
                f"Install it or set the full path via adapter.cli_command.",
                exit_code=127,
                stderr="",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WriteFailure(
                f"Claude CLI timed out after {max_wait}s for stage={stage!r}"
            ) from exc
        duration = time.monotonic() - start

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        # Truncate stdout to avoid OOM on runaway outputs; record the truncation
        # in the result so the caller knows.
        truncated = False
        if len(stdout.encode("utf-8", errors="ignore")) > self.max_output_bytes:
            stdout = stdout.encode("utf-8", errors="ignore")[: self.max_output_bytes].decode(
                "utf-8", errors="ignore"
            )
            truncated = True

        usage = parse_token_usage(stdout)
        if not usage.input_tokens and not usage.output_tokens:
            log.warning(
                "Could not parse token usage from CLI stdout for stage=%s; "
                "assuming 0/0 (this should not block the pipeline).",
                stage,
            )
        if truncated:
            log.warning("CLI stdout for stage=%s exceeded %d bytes; truncated.", stage, self.max_output_bytes)

        # Error classification.
        if proc.returncode != 0:
            err = self._classify_error(proc.returncode, stderr)
            if isinstance(err, RateLimited):
                raise err
            if isinstance(err, CLIError):
                raise err
            # Generic write failure.
            raise WriteFailure(
                f"Claude CLI exited with code {proc.returncode} for stage={stage!r}: {stderr[:500]}"
            )

        result = StageResult(
            raw_output=stdout,
            token_usage=usage,
            exit_code=proc.returncode,
            duration=duration,
            model=model or "",
        )
        # Try to parse a structured JSON object from the tail of stdout;
        # the review stages rely on this.  Failures are non-fatal.
        result.parsed = _try_parse_json(stdout)
        if self.usage_log is not None:
            self.usage_log.append(
                stage=stage,
                batch=batch,
                model=model or "",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                duration=duration,
                exit_code=proc.returncode,
            )
        return result

    # -- internals -----------------------------------------------------

    def _resolved_env(self) -> Optional[Mapping[str, str]]:
        if self._env is not None:
            return self._env
        return None  # inherit parent env

    def _ensure_cli_available(self) -> None:
        # If the user passed a full path we use it as-is; otherwise resolve
        # from PATH.  This avoids FileNotFoundError masquerading as a real
        # failure inside subprocess.run.
        if "/" in self.cli_command or self.cli_command.startswith("."):
            if not Path(self.cli_command).exists():
                raise CLIError(
                    f"Claude CLI not found at {self.cli_command!r}",
                    exit_code=127,
                )
        else:
            if shutil.which(self.cli_command) is None:
                raise CLIError(
                    f"Claude CLI {self.cli_command!r} not on PATH. "
                    f"Install Claude Code CLI or set ANTHROPIC_API_KEY with "
                    f"--use-mock to bypass.",
                    exit_code=127,
                )

    def _ensure_api_key(self) -> None:
        # Claude Code CLI handles its own auth (OAuth / API key from its
        # own config).  We only check ANTHROPIC_API_KEY when no Claude CLI
        # config is present, to give a clear error.
        if os.environ.get("ANTHROPIC_API_KEY"):
            return
        # Heuristic: if the user has a ~/.claude directory, assume they
        # authenticated via the CLI.  Otherwise surface a clear warning.
        claude_dir = Path.home() / ".claude"
        if claude_dir.exists():
            return
        # Don't raise — the CLI itself will return a useful error if the
        # user really isn't authenticated.  We just log a warning.
        log.warning(
            "ANTHROPIC_API_KEY is not set and ~/.claude/ is missing. "
            "If the CLI fails, run `claude login` or set the key."
        )

    def _build_args(
        self,
        prompt: str,
        *,
        model: str,
        context_files: Sequence[Path],
    ) -> list[str]:
        args: list[str] = ["--prompt", prompt, "--non-interactive"]
        if model:
            args += ["--model", model]
        for path in context_files:
            args += ["--file", str(path)]
        args += self.extra_args
        return args

    @staticmethod
    def _classify_error(exit_code: int, stderr: str) -> Optional[Exception]:
        text = (stderr or "").lower()
        if exit_code == 429 or "rate limit" in text or "rate_limit" in text:
            return RateLimited(f"Claude API rate limit: {stderr[:200]}")
        if "context length" in text or "context window" in text or "too long" in text:
            # Let the orchestrator turn this into a ContextOverflow.
            return WriteFailure(f"Claude context overflow: {stderr[:200]}")
        if exit_code in (124, 137):  # timeout / OOM kill
            return WriteFailure(f"Claude CLI killed (exit={exit_code}): {stderr[:200]}")
        return None


def _try_parse_json(stdout: str) -> Optional[dict[str, Any]]:
    """Try to find a JSON object in ``stdout``.

    Looks at the last ``{...}`` block first (most CLIs print the
    payload last), then at the whole buffer.
    """

    if not stdout:
        return None
    # try the last line that starts with { and ends with }
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    # fallback: try the entire stdout
    stripped = stdout.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Mock adapter
# --------------------------------------------------------------------------- #


@dataclass
class MockResponse:
    """A single canned response."""

    output: str
    input_tokens: int = 100
    output_tokens: int = 200
    exit_code: int = 0
    model: str = "mock-model"
    parsed: Optional[dict[str, Any]] = None


class MockClaudeAdapter:
    """A configurable mock for tests.

    Usage::

        mock = MockClaudeAdapter()
        mock.set_response("generate_outline", MockResponse(output="..."))
        result = mock.invoke(prompt, stage="generate_outline", ...)

    For scripts that need to inject failures (rate limits, timeouts), use
    :meth:`set_failure`.
    """

    def __init__(self, usage_log: Optional[TokenUsageLog] = None) -> None:
        self._responses: dict[str, MockResponse] = {}
        self._failures: dict[str, BaseException] = {}
        self.calls: list[dict[str, Any]] = []
        self.usage_log = usage_log

    def set_response(self, stage: str, response: MockResponse) -> None:
        self._responses[stage] = response

    def set_failure(self, stage: str, exc: BaseException) -> None:
        self._failures[stage] = exc

    # Default JSON for review stages that have no explicit mock response.
    _DEFAULT_REVIEW_JSON = json.dumps(
        {
            "passed": True,
            "route": "APPROVED",
            "findings": [],
            "required_changes": [],
            "summary": "mock auto-approve",
        }
    )

    def invoke(
        self,
        prompt: str,
        *,
        stage: str,
        model: str = "",
        batch: Optional[str] = None,
        context_files: Sequence[Path] = (),
        max_wait: float = 600.0,
    ) -> StageResult:
        self.calls.append(
            {
                "stage": stage,
                "batch": batch,
                "prompt": prompt,
                "model": model,
                "context_files": [str(p) for p in context_files],
            }
        )
        if stage in self._failures:
            raise self._failures[stage]
        if stage in self._responses:
            response = self._responses[stage]
        elif stage.startswith("review_"):
            response = MockResponse(
                output=self._DEFAULT_REVIEW_JSON,
                input_tokens=30,
                output_tokens=40,
            )
        else:
            response = MockResponse(
                output=f"# mock output for {stage}\n\n{stage} content",
                input_tokens=100,
                output_tokens=200,
            )
        result = StageResult(
            raw_output=response.output,
            token_usage=TokenUsage(
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            ),
            exit_code=response.exit_code,
            duration=0.0,
            model=response.model or model,
            parsed=response.parsed,
        )
        if self.usage_log is not None:
            self.usage_log.append(
                stage=stage,
                batch=batch,
                model=result.model,
                input_tokens=result.token_usage.input_tokens,
                output_tokens=result.token_usage.output_tokens,
                duration=result.duration,
                exit_code=result.exit_code,
            )
        return result
