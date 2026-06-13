"""Tests for the exception hierarchy (Phase 1.1)."""

from __future__ import annotations

import pytest

from novelforge.errors import (
    ConfigError,
    NovelForgeError,
    StageIncomplete,
    VerifyFailed,
)


def test_stage_incomplete_is_novelforge_error() -> None:
    exc = StageIncomplete("missing signal")
    assert isinstance(exc, NovelForgeError)


def test_stage_incomplete_carries_stage_and_attempt() -> None:
    exc = StageIncomplete(
        "no signal",
        stage_id="write_chapter",
        attempt=2,
    )
    assert exc.stage_id == "write_chapter"
    assert exc.attempt == 2
    assert "no signal" in str(exc)


def test_verify_failed_is_novelforge_error() -> None:
    exc = VerifyFailed("check failed")
    assert isinstance(exc, NovelForgeError)


def test_verify_failed_carries_structured_detail() -> None:
    exc = VerifyFailed(
        "min_chars not met",
        stage_id="write_chapter",
        attempt=1,
        target="output/chapters/001.md",
        kind="min_chars",
        expected=500,
        actual=12,
    )
    assert exc.stage_id == "write_chapter"
    assert exc.target == "output/chapters/001.md"
    assert exc.kind == "min_chars"
    assert exc.expected == 500
    assert exc.actual == 12
    assert exc.detail == {
        "stage_id": "write_chapter",
        "target": "output/chapters/001.md",
        "kind": "min_chars",
        "expected": 500,
        "actual": 12,
    }


def test_verify_failed_detail_handles_missing_fields() -> None:
    exc = VerifyFailed("oops")
    assert exc.detail == {
        "stage_id": None,
        "target": None,
        "kind": None,
        "expected": None,
        "actual": None,
    }


def test_config_error_remains_subclass() -> None:
    assert issubclass(ConfigError, NovelForgeError)
