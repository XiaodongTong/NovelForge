"""Unit tests for the Claude adapter helpers (Phase 5 coverage backfill).

Covers the pure helpers in :mod:`novelforge.claude.adapter`:

- :func:`build_prompt` — suffix injection logic (AC-11)
- :func:`detect_completion_signal` — first-layer protocol
- :func:`parse_token_usage` — best-effort token usage parsing
- :func:`_try_parse_json` — JSON extraction
- :meth:`ClaudeCLIAdapter._classify_error` — exit code → exception mapping
- :meth:`ClaudeCLIAdapter._build_args` — CLI arg assembly
- :meth:`ClaudeCLIAdapter._ensure_cli_available` / `_ensure_api_key` — guard logic

The :class:`MockClaudeAdapter` switch matrix (NO_SIGNAL / EMPTY /
ALWAYS_FAIL) is exercised in :mod:`tests.test_e2e_contract` so here we
only sanity-check the default happy-path behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from novelforge.claude.adapter import (
    ClaudeCLIAdapter,
    MockClaudeAdapter,
    MockResponse,
    StageResult,
    TokenUsage,
    _try_parse_json,
    build_prompt,
    detect_completion_signal,
    parse_token_usage,
)
from novelforge.errors import CLIError, RateLimited, WriteFailure
from novelforge.verify import COMPLETION_SUFFIX, DEFAULT_COMPLETION_SIGNAL, EXECUTION_SUFFIX


# --------------------------------------------------------------------------- #
# build_prompt
# --------------------------------------------------------------------------- #


class TestBuildPrompt:
    def test_no_suffix_when_disabled(self) -> None:
        out = build_prompt("hello", append_suffix=False, completion_signal=DEFAULT_COMPLETION_SIGNAL)
        assert out == "hello"

    def test_execution_suffix_appended(self) -> None:
        out = build_prompt("hello", append_suffix=True, completion_signal=None)
        assert EXECUTION_SUFFIX in out
        assert out.startswith("hello")
        # No completion suffix when completion_signal=None.
        assert COMPLETION_SUFFIX not in out

    def test_completion_suffix_only_when_signal_set(self) -> None:
        out = build_prompt("hello", append_suffix=True, completion_signal=DEFAULT_COMPLETION_SIGNAL)
        assert EXECUTION_SUFFIX in out
        assert COMPLETION_SUFFIX in out
        assert DEFAULT_COMPLETION_SIGNAL in out

    def test_idempotent_when_suffix_already_present(self) -> None:
        prompt = "hello" + EXECUTION_SUFFIX + COMPLETION_SUFFIX
        out = build_prompt(prompt, append_suffix=True, completion_signal=DEFAULT_COMPLETION_SIGNAL)
        # Should not double-append.
        assert out.count(EXECUTION_SUFFIX) == 1
        assert out.count(COMPLETION_SUFFIX) == 1


# --------------------------------------------------------------------------- #
# detect_completion_signal
# --------------------------------------------------------------------------- #


class TestDetectCompletionSignal:
    def test_returns_true_when_signal_present(self) -> None:
        assert detect_completion_signal(
            f"output text\n{DEFAULT_COMPLETION_SIGNAL}\n",
            DEFAULT_COMPLETION_SIGNAL,
        ) is True

    def test_returns_false_when_signal_absent(self) -> None:
        assert detect_completion_signal("just text", DEFAULT_COMPLETION_SIGNAL) is False

    def test_returns_true_when_expected_is_empty(self) -> None:
        """AC-11: completion_signal=None short-circuits to True."""

        assert detect_completion_signal("anything", "") is True
        assert detect_completion_signal("", "") is True

    def test_handles_none_stdout(self) -> None:
        # `or "" guard inside the function.
        assert detect_completion_signal("", DEFAULT_COMPLETION_SIGNAL) is False


# --------------------------------------------------------------------------- #
# parse_token_usage
# --------------------------------------------------------------------------- #


class TestParseTokenUsage:
    def test_empty_stdout(self) -> None:
        u = parse_token_usage("")
        assert u == TokenUsage(0, 0)

    def test_json_usage_object(self) -> None:
        stdout = 'noise\n{"usage": {"input_tokens": 42, "output_tokens": 17}}\nmore'
        u = parse_token_usage(stdout)
        assert u.input_tokens == 42
        assert u.output_tokens == 17

    def test_json_object_without_usage(self) -> None:
        stdout = '{"foo": "bar"}'
        u = parse_token_usage(stdout)
        # Falls through to pattern search; no tokens.
        assert u == TokenUsage(0, 0)

    def test_keyvalue_patterns(self) -> None:
        stdout = "input_tokens: 100\noutput_tokens: 200"
        u = parse_token_usage(stdout)
        assert u.input_tokens == 100
        assert u.output_tokens == 200

    def test_in_out_patterns(self) -> None:
        stdout = "in=10 out=20"
        u = parse_token_usage(stdout)
        assert u.input_tokens == 10
        assert u.output_tokens == 20

    def test_no_patterns_matched(self) -> None:
        u = parse_token_usage("just regular output")
        assert u == TokenUsage(0, 0)


# --------------------------------------------------------------------------- #
# _try_parse_json
# --------------------------------------------------------------------------- #


class TestTryParseJson:
    def test_empty_returns_none(self) -> None:
        assert _try_parse_json("") is None

    def test_inline_json_line(self) -> None:
        stdout = 'prefix\n{"passed": true}\nsuffix'
        assert _try_parse_json(stdout) == {"passed": True}

    def test_pure_json_blob(self) -> None:
        stdout = '{"a": 1, "b": [2, 3]}'
        assert _try_parse_json(stdout) == {"a": 1, "b": [2, 3]}

    def test_invalid_json_returns_none(self) -> None:
        stdout = "not even close to json"
        assert _try_parse_json(stdout) is None

    def test_json_array_returns_none(self) -> None:
        # Only dict-typed objects are returned.
        stdout = "[1, 2, 3]"
        assert _try_parse_json(stdout) is None


# --------------------------------------------------------------------------- #
# ClaudeCLIAdapter._classify_error
# --------------------------------------------------------------------------- #


class TestClassifyError:
    def test_429_exit_code(self) -> None:
        err = ClaudeCLIAdapter._classify_error(429, "slow down")
        assert isinstance(err, RateLimited)

    def test_rate_limit_message(self) -> None:
        err = ClaudeCLIAdapter._classify_error(1, "Rate limit exceeded")
        assert isinstance(err, RateLimited)

    def test_context_overflow_message(self) -> None:
        err = ClaudeCLIAdapter._classify_error(1, "context length too long")
        assert isinstance(err, WriteFailure)

    def test_killed_exit_code_137(self) -> None:
        err = ClaudeCLIAdapter._classify_error(137, "killed")
        assert isinstance(err, WriteFailure)

    def test_killed_exit_code_124(self) -> None:
        err = ClaudeCLIAdapter._classify_error(124, "timeout")
        assert isinstance(err, WriteFailure)

    def test_unknown_returns_none(self) -> None:
        err = ClaudeCLIAdapter._classify_error(1, "some random error")
        assert err is None


# --------------------------------------------------------------------------- #
# ClaudeCLIAdapter._build_args
# --------------------------------------------------------------------------- #


class TestBuildArgs:
    def test_basic_args(self) -> None:
        adapter = ClaudeCLIAdapter()
        args = adapter._build_args("hello", model="claude-opus-4-7", context_files=())
        assert "--prompt" in args
        assert "hello" in args
        assert "--non-interactive" in args
        assert "--model" in args
        assert "claude-opus-4-7" in args

    def test_with_context_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f1.write_text("a", encoding="utf-8")
        f2 = tmp_path / "b.txt"
        f2.write_text("b", encoding="utf-8")
        adapter = ClaudeCLIAdapter()
        args = adapter._build_args("hello", model="", context_files=[f1, f2])
        # Each file appears as --file <path>.
        assert args.count("--file") == 2
        assert str(f1) in args
        assert str(f2) in args

    def test_with_extra_args(self) -> None:
        adapter = ClaudeCLIAdapter(extra_args=["--verbose"])
        args = adapter._build_args("hello", model="", context_files=())
        assert "--verbose" in args


# --------------------------------------------------------------------------- #
# ClaudeCLIAdapter._ensure_cli_available / _ensure_api_key
# --------------------------------------------------------------------------- #


class TestEnsureGuards:
    def test_missing_path_cli_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-cli"
        adapter = ClaudeCLIAdapter(cli_command=str(missing))
        with pytest.raises(CLIError) as excinfo:
            adapter._ensure_cli_available()
        assert "not found" in str(excinfo.value).lower()

    def test_missing_path_cli_raises_for_relative_dot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A relative ./foo path triggers the file-existence branch.
        monkeypatch.chdir(tmp_path)
        adapter = ClaudeCLIAdapter(cli_command="./missing-cli")
        with pytest.raises(CLIError):
            adapter._ensure_cli_available()

    def test_present_path_cli_passes(self, tmp_path: Path) -> None:
        cli = tmp_path / "fake-cli"
        cli.write_text("#!/bin/sh\n", encoding="utf-8")
        adapter = ClaudeCLIAdapter(cli_command=str(cli))
        # Should not raise.
        adapter._ensure_cli_available()

    def test_which_lookup_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = ClaudeCLIAdapter(cli_command="some-existing-cli")
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/" + name)
        adapter._ensure_cli_available()

    def test_which_lookup_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = ClaudeCLIAdapter(cli_command="missing-cli")
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(CLIError) as excinfo:
            adapter._ensure_cli_available()
        assert "PATH" in str(excinfo.value)

    def test_api_key_set_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        adapter = ClaudeCLIAdapter()
        # Should not raise.
        adapter._ensure_api_key()

    def test_api_key_missing_warns_when_claude_dir_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No API key, but ~/.claude/ exists → just warn (no raise).
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        adapter = ClaudeCLIAdapter()
        adapter._ensure_api_key()


# --------------------------------------------------------------------------- #
# Mock adapter sanity check
# --------------------------------------------------------------------------- #


class TestMockAdapterDefault:
    def test_default_body_carries_signal(self) -> None:
        adapter = MockClaudeAdapter()
        result = adapter.invoke("hello", stage="test")
        assert isinstance(result, StageResult)
        assert result.completion_signal is True
        assert DEFAULT_COMPLETION_SIGNAL in result.raw_output

    def test_records_call(self) -> None:
        adapter = MockClaudeAdapter()
        adapter.invoke("hello", stage="test", batch="001")
        assert len(adapter.calls) == 1
        call = adapter.calls[0]
        assert call["stage"] == "test"
        assert call["batch"] == "001"
        assert call["prompt"] == "hello"

    def test_set_failure_propagates(self) -> None:
        adapter = MockClaudeAdapter()
        adapter.set_failure("boom", RuntimeError("kaboom"))
        with pytest.raises(RuntimeError, match="kaboom"):
            adapter.invoke("hello", stage="boom")

    def test_set_response_overrides_default(self) -> None:
        adapter = MockClaudeAdapter()
        adapter.set_response("custom", MockResponse(output="custom body"))
        result = adapter.invoke("hello", stage="custom")
        assert "custom body" in result.raw_output

    def test_omit_signal_in_mock_response(self) -> None:
        adapter = MockClaudeAdapter()
        adapter.set_response(
            "no_signal", MockResponse(output="no signal here", omit_signal=True)
        )
        result = adapter.invoke("hello", stage="no_signal")
        assert result.completion_signal is False

    def test_reset_clears_call_log(self) -> None:
        adapter = MockClaudeAdapter()
        adapter.invoke("hello", stage="a")
        adapter.invoke("hello", stage="b")
        assert len(adapter.calls) == 2
        adapter.reset()
        assert adapter.calls == []
