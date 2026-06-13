"""Claude Code CLI adapter.

This module is the **only** place in the engine that talks to the
Claude Code CLI.  Everything else (orchestrator, stages, context) sees
an :class:`ClaudeAdapter` interface — an instance with an
``invoke(prompt, ...) -> StageResult`` method that abstracts away the
subprocess details.

Key design choices:

- We invoke the CLI in ``--prompt`` mode, passing the prompt and any
  extra context files as positional/file arguments.
- The adapter always returns a :class:`StageResult` even on failure; it
  only raises when the CLI itself cannot be launched (binary missing,
  no API key configured, etc.).
- :class:`StageResult` exposes ``completion_signal`` so the orchestrator
  can apply the first-layer completion check (spec §AC-2).
- A :class:`MockClaudeAdapter` is available for tests; it returns
  canned responses from an in-memory fixture and honours three
  environment-variable switches (``NOVELFORGE_MOCK_NO_SIGNAL`` /
  ``NOVELFORGE_MOCK_EMPTY`` / ``NOVELFORGE_MOCK_ALWAYS_FAIL``) so the
  contract tests can drive the C-tier retry loop end-to-end.
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
from ..verify import COMPLETION_SUFFIX, DEFAULT_COMPLETION_SIGNAL, EXECUTION_SUFFIX
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
    completion_signal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_output": self.raw_output,
            "token_usage": self.token_usage.to_dict(),
            "exit_code": self.exit_code,
            "duration": self.duration,
            "model": self.model,
            "error": self.error,
            "parsed": self.parsed,
            "completion_signal": self.completion_signal,
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
        append_suffix: bool = True,
        completion_signal: Optional[str] = DEFAULT_COMPLETION_SIGNAL,
    ) -> StageResult: ...


# --------------------------------------------------------------------------- #
# Suffix + completion signal helpers
# --------------------------------------------------------------------------- #


def build_prompt(
    prompt: str,
    *,
    append_suffix: bool,
    completion_signal: Optional[str],
) -> str:
    """Append the EXECUTION_SUFFIX (and optionally COMPLETION_SUFFIX).

    - ``append_suffix=False`` keeps ``prompt`` unchanged (used by tests
      and by stages that pre-render their own suffix).
    - ``completion_signal=None`` skips the COMPLETION_SUFFIX so the
      prompt is not polluted when the first-layer protocol is disabled
      (AC-11).
    """

    if not append_suffix:
        return prompt
    out = prompt
    if EXECUTION_SUFFIX and EXECUTION_SUFFIX not in out:
        out = out.rstrip() + EXECUTION_SUFFIX
    if completion_signal and COMPLETION_SUFFIX not in out:
        out = out.rstrip() + COMPLETION_SUFFIX
    return out


def detect_completion_signal(stdout: str, expected: str) -> bool:
    """Return True iff the configured signal marker appears in stdout."""

    if not expected:
        return True
    return expected in (stdout or "")


# --------------------------------------------------------------------------- #
# Token usage parsing
# --------------------------------------------------------------------------- #


_USAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"input[_\s]*tokens\s*[:=]\s*(\d+)", re.IGNORECASE),
    re.compile(r"output[_\s]*tokens\s*[:=]\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bin\s*=\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bout\s*=\s*(\d+)", re.IGNORECASE),
)


def parse_token_usage(stdout: str) -> TokenUsage:
    """Best-effort extraction of token usage from CLI output."""

    if not stdout:
        return TokenUsage()
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
    """Invokes the Claude Code CLI via ``--prompt`` mode."""

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

    def invoke(
        self,
        prompt: str,
        *,
        stage: str,
        model: str = "",
        batch: Optional[str] = None,
        context_files: Sequence[Path] = (),
        max_wait: float = 600.0,
        append_suffix: bool = True,
        completion_signal: Optional[str] = DEFAULT_COMPLETION_SIGNAL,
    ) -> StageResult:
        self._ensure_cli_available()
        self._ensure_api_key()

        final_prompt = build_prompt(
            prompt,
            append_suffix=append_suffix,
            completion_signal=completion_signal,
        )
        args = self._build_args(final_prompt, model=model, context_files=context_files)
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

        if proc.returncode != 0:
            err = self._classify_error(proc.returncode, stderr)
            if isinstance(err, RateLimited):
                raise err
            if isinstance(err, CLIError):
                raise err
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
        result.parsed = _try_parse_json(stdout)
        result.completion_signal = detect_completion_signal(
            stdout, completion_signal or ""
        )
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
        return None

    def _ensure_cli_available(self) -> None:
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
        if os.environ.get("ANTHROPIC_API_KEY"):
            return
        claude_dir = Path.home() / ".claude"
        if claude_dir.exists():
            return
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
            return WriteFailure(f"Claude context overflow: {stderr[:200]}")
        if exit_code in (124, 137):
            return WriteFailure(f"Claude CLI killed (exit={exit_code}): {stderr[:200]}")
        return None


def _try_parse_json(stdout: str) -> Optional[dict[str, Any]]:
    """Try to find a JSON object in ``stdout``."""

    if not stdout:
        return None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
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
    omit_signal: bool = False


def _env_truthy(name: str) -> bool:
    raw = os.environ.get(name, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class MockClaudeAdapter:
    """A configurable mock for tests.

    Default behaviour: each invoke returns a non-empty ``produces``
    body that ends with the standard completion signal, so the
    first-layer check passes and the second-layer ``done_when.checks``
    can verify the file content.

    Three environment switches (spec §4.2 / verify.md) drive the
    negative scenarios:

    - ``NOVELFORGE_MOCK_NO_SIGNAL=1`` — first invoke omits the
      completion signal (triggers ``StageIncomplete``); subsequent
      invokes behave normally.
    - ``NOVELFORGE_MOCK_EMPTY=1`` — first invoke writes empty produces
      (triggers ``VerifyFailed`` via ``min_chars`` etc.); subsequent
      invokes behave normally.
    - ``NOVELFORGE_MOCK_ALWAYS_FAIL=1`` — every invoke both omits the
      signal and writes empty produces (drives the ``max_attempts``
      exhaustion scenario).
    """

    def __init__(self, usage_log: Optional[TokenUsageLog] = None) -> None:
        self._responses: dict[str, MockResponse] = {}
        self._failures: dict[str, BaseException] = {}
        self.calls: list[dict[str, Any]] = []
        self.usage_log = usage_log
        self._call_counts: dict[tuple[str, Optional[str]], int] = {}

    def set_response(self, stage: str, response: MockResponse) -> None:
        self._responses[stage] = response

    def set_failure(self, stage: str, exc: BaseException) -> None:
        self._failures[stage] = exc

    def reset(self) -> None:
        """Clear recorded calls + per-key counters (keep canned responses)."""

        self.calls.clear()
        self._call_counts.clear()

    def invoke(
        self,
        prompt: str,
        *,
        stage: str,
        model: str = "",
        batch: Optional[str] = None,
        context_files: Sequence[Path] = (),
        max_wait: float = 600.0,
        append_suffix: bool = True,
        completion_signal: Optional[str] = DEFAULT_COMPLETION_SIGNAL,
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

        # Determine the canned body for this stage.
        if stage in self._responses:
            base = self._responses[stage]
            body = base.output
            base_omit_signal = base.omit_signal
        else:
            body = self._default_body(stage)
            base_omit_signal = False

        # Apply negative-switch semantics.
        key = (stage, batch)
        count = self._call_counts.get(key, 0) + 1
        self._call_counts[key] = count

        always_fail = _env_truthy("NOVELFORGE_MOCK_ALWAYS_FAIL")
        no_signal_first = _env_truthy("NOVELFORGE_MOCK_NO_SIGNAL")
        empty_first = _env_truthy("NOVELFORGE_MOCK_EMPTY")

        omit_signal = base_omit_signal
        empty_body = False
        if always_fail:
            omit_signal = True
            empty_body = True
        else:
            if no_signal_first and count == 1:
                omit_signal = True
            if empty_first and count == 1:
                empty_body = True

        if empty_body:
            body = ""

        if not omit_signal and completion_signal:
            # Ensure the signal appears once at the end of the body.
            if completion_signal not in body:
                body = body.rstrip() + "\n" + completion_signal

        result = StageResult(
            raw_output=body,
            token_usage=TokenUsage(input_tokens=50, output_tokens=80),
            exit_code=0,
            duration=0.0,
            model="mock-model",
        )
        result.completion_signal = detect_completion_signal(
            body, completion_signal or ""
        )
        result.parsed = _try_parse_json(body)
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

    @staticmethod
    def _default_body(stage: str) -> str:
        """A reasonable default body that satisfies typical checks."""

        if stage.startswith("review") or stage.startswith("judge"):
            return (
                '{"passed": true, "findings": [], "summary": "ok"}'
            )
        if stage.startswith("generate") or stage.endswith("outline"):
            # Include a "## Chapter N - Title" heading so the default
            # sample's regex_match check on outlines passes in mock mode.
            return (
                f"# Outline for {stage}\n\n"
                "## Chapter 1 - The Hero Awakens\n"
                "A young hero discovers a hidden power and must rise to "
                "meet an ancient threat. The world is broken; the seeds "
                "of restoration lie scattered, waiting to be found."
            )
        # Generic prose: ≥ 200 chars so default min_chars checks pass.
        return (
            f"# Output for {stage}\n\n"
            + ("The chapter content unfolds with deliberate care. " * 8)
        )


__all__ = [
    "TokenUsage",
    "StageResult",
    "ClaudeAdapter",
    "ClaudeCLIAdapter",
    "MockClaudeAdapter",
    "MockResponse",
    "build_prompt",
    "detect_completion_signal",
    "parse_token_usage",
]
