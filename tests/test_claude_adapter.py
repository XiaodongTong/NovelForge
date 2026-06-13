"""M3 tests: Claude adapter (mock + parsing + JSONL log)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novelforge.claude.adapter import (
    ClaudeCLIAdapter,
    MockClaudeAdapter,
    MockResponse,
    StageResult,
    TokenUsage,
    parse_token_usage,
)
from novelforge.claude.tokens import TokenUsageLog
from novelforge.errors import CLIError, RateLimited, WriteFailure


# --------------------------------------------------------------------------- #
# parse_token_usage
# --------------------------------------------------------------------------- #


def test_parse_token_usage_json_tail() -> None:
    stdout = 'some narrative text\n{"usage": {"input_tokens": 12, "output_tokens": 34}}\n'
    usage = parse_token_usage(stdout)
    assert usage.input_tokens == 12
    assert usage.output_tokens == 34


def test_parse_token_usage_in_out_format() -> None:
    stdout = "Working...\nUsage: in=12 out=34\n"
    usage = parse_token_usage(stdout)
    assert usage.input_tokens == 12
    assert usage.output_tokens == 34


def test_parse_token_usage_equals_format() -> None:
    stdout = "Result: input_tokens=12 output_tokens=34\n"
    usage = parse_token_usage(stdout)
    assert usage.input_tokens == 12
    assert usage.output_tokens == 34


def test_parse_token_usage_no_match_returns_zero() -> None:
    usage = parse_token_usage("no usage info here")
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_parse_token_usage_empty_input() -> None:
    assert parse_token_usage("").input_tokens == 0
    assert parse_token_usage("").output_tokens == 0


# --------------------------------------------------------------------------- #
# MockClaudeAdapter
# --------------------------------------------------------------------------- #


def test_mock_default_response(tmp_path: Path) -> None:
    log = TokenUsageLog(tmp_path / "u.log")
    adapter = MockClaudeAdapter(usage_log=log)
    result = adapter.invoke("hi", stage="generate_outline")
    assert isinstance(result, StageResult)
    assert result.exit_code == 0
    assert result.token_usage.input_tokens == 100
    assert "generate_outline" in result.raw_output
    # call recorded
    assert adapter.calls[0]["stage"] == "generate_outline"


def test_mock_custom_response(tmp_path: Path) -> None:
    adapter = MockClaudeAdapter()
    adapter.set_response(
        "review_outline",
        MockResponse(
            output='{"passed": true, "route": "APPROVED"}',
            input_tokens=7,
            output_tokens=11,
            parsed={"passed": True, "route": "APPROVED"},
        ),
    )
    result = adapter.invoke("p", stage="review_outline")
    assert result.token_usage.input_tokens == 7
    assert result.parsed == {"passed": True, "route": "APPROVED"}


def test_mock_failure_is_raised(tmp_path: Path) -> None:
    adapter = MockClaudeAdapter()
    adapter.set_failure("write_chapter", RateLimited("simulated 429"))
    with pytest.raises(RateLimited):
        adapter.invoke("p", stage="write_chapter")


def test_mock_writes_to_token_usage_log(tmp_path: Path) -> None:
    log_path = tmp_path / "u.log"
    log = TokenUsageLog(log_path)
    adapter = MockClaudeAdapter(usage_log=log)
    adapter.invoke("p", stage="generate_outline", batch="001")
    records = log.read_all()
    assert len(records) == 1
    assert records[0]["stage"] == "generate_outline"
    assert records[0]["input_tokens"] == 100
    assert records[0]["batch"] == "001"
    # Verify JSONL is well-formed
    raw = log_path.read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line) for line in raw)


# --------------------------------------------------------------------------- #
# ClaudeCLIAdapter construction & arg building
# --------------------------------------------------------------------------- #


def test_cli_adapter_raises_when_binary_missing(tmp_path: Path) -> None:
    adapter = ClaudeCLIAdapter(cli_command=str(tmp_path / "no-such-bin"))
    with pytest.raises(CLIError, match="not found"):
        adapter.invoke("hi", stage="generate_outline")


def test_cli_adapter_raises_when_binary_not_on_path() -> None:
    adapter = ClaudeCLIAdapter(cli_command="definitely-not-a-real-cli-xyz")
    with pytest.raises(CLIError, match="not on PATH"):
        adapter.invoke("hi", stage="generate_outline")


def test_cli_adapter_classify_rate_limit() -> None:
    assert isinstance(
        ClaudeCLIAdapter._classify_error(429, "rate limit exceeded"),
        RateLimited,
    )
    assert isinstance(
        ClaudeCLIAdapter._classify_error(0, "rate_limit hit"),
        RateLimited,
    )


def test_cli_adapter_classify_context_overflow() -> None:
    exc = ClaudeCLIAdapter._classify_error(1, "context length exceeded")
    assert isinstance(exc, WriteFailure)
    assert "context overflow" in str(exc)


def test_cli_adapter_classify_timeout() -> None:
    exc = ClaudeCLIAdapter._classify_error(124, "")
    assert isinstance(exc, WriteFailure)


# --------------------------------------------------------------------------- #
# Real adapter subprocess integration (mocked subprocess.run)
# --------------------------------------------------------------------------- #


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> "MagicMock":
    cm = MagicMock()
    cm.stdout = stdout
    cm.stderr = stderr
    cm.returncode = returncode
    return cm


def test_cli_adapter_invokes_subprocess(tmp_path: Path) -> None:
    log_path = tmp_path / "tokens.log"
    usage_log = TokenUsageLog(log_path)
    adapter = ClaudeCLIAdapter(cli_command="/bin/echo", usage_log=usage_log)
    # We feed echo with a payload that includes a JSON usage line.
    payload = (
        "Generated outline\n"
        '{"usage": {"input_tokens": 33, "output_tokens": 44}}\n'
    )

    fake_proc = _completed(payload, returncode=0, stderr="")

    with patch("subprocess.run", return_value=fake_proc) as run:
        result = adapter.invoke("the prompt", stage="generate_outline", model="m")
    run.assert_called_once()
    args, kwargs = run.call_args
    # the first arg is the argv list
    argv = args[0]
    assert argv[0] == "/bin/echo"
    # --prompt flag must be present
    assert "--prompt" in argv
    # model flag
    assert "m" in argv
    # non-interactive
    assert "--non-interactive" in argv
    assert result.exit_code == 0
    assert result.token_usage.input_tokens == 33
    assert result.token_usage.output_tokens == 44
    assert result.parsed == {"usage": {"input_tokens": 33, "output_tokens": 44}}
    # JSONL log was appended
    records = usage_log.read_all()
    assert len(records) == 1
    assert records[0]["input_tokens"] == 33


def test_cli_adapter_nonzero_exit_raises(tmp_path: Path) -> None:
    adapter = ClaudeCLIAdapter(cli_command="/bin/echo")
    fake_proc = _completed("nope", returncode=1, stderr="boom")
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(WriteFailure):
            adapter.invoke("p", stage="write_chapter")


def test_cli_adapter_rate_limit_translates_to_rate_limited(tmp_path: Path) -> None:
    adapter = ClaudeCLIAdapter(cli_command="/bin/echo")
    fake_proc = _completed("rate limit exceeded", returncode=429, stderr="rate limit")
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(RateLimited):
            adapter.invoke("p", stage="write_chapter")


def test_cli_adapter_timeout_raises_write_failure(tmp_path: Path) -> None:
    import subprocess as sp

    adapter = ClaudeCLIAdapter(cli_command="/bin/echo")
    with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="x", timeout=1.0)):
        with pytest.raises(WriteFailure):
            adapter.invoke("p", stage="write_chapter")


def test_cli_adapter_uses_context_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.md"
    f1.write_text("a", encoding="utf-8")
    f2 = tmp_path / "b.md"
    f2.write_text("b", encoding="utf-8")
    adapter = ClaudeCLIAdapter(cli_command="/bin/echo")
    fake_proc = _completed("ok", returncode=0)
    with patch("subprocess.run", return_value=fake_proc) as run:
        adapter.invoke("p", stage="write_chapter", context_files=[f1, f2])
    argv = run.call_args[0][0]
    # Each context file should be passed via --file
    file_args = [argv[i + 1] for i, a in enumerate(argv) if a == "--file"]
    assert str(f1) in file_args
    assert str(f2) in file_args


def test_cli_adapter_truncates_oversize_output(tmp_path: Path) -> None:
    adapter = ClaudeCLIAdapter(
        cli_command="/bin/echo",
        max_output_bytes=100,
    )
    huge = "X" * 5000
    fake_proc = _completed(huge, returncode=0)
    with patch("subprocess.run", return_value=fake_proc):
        result = adapter.invoke("p", stage="write_chapter")
    assert len(result.raw_output) < 200
