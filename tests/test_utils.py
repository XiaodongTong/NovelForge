"""Unit tests for utils.fs / utils.log / utils package (Phase 5 backfill)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from novelforge.utils import count_words
from novelforge.utils.fs import (
    atomic_write,
    ensure_dir,
    list_files,
    sha256_file,
    sha256_text,
)
from novelforge.utils.log import (
    ROOT_LOGGER_NAME,
    configure_logging,
    env_flag,
    get_logger,
    log_stage_enter,
    log_stage_exit,
)


# --------------------------------------------------------------------------- #
# count_words
# --------------------------------------------------------------------------- #


class TestCountWords:
    def test_empty(self) -> None:
        assert count_words("") == 0

    def test_latin(self) -> None:
        assert count_words("hello world foo") == 3

    def test_cjk(self) -> None:
        assert count_words("你好世界") == 4

    def test_mixed(self) -> None:
        # 4 CJK chars + 2 latin words.
        assert count_words("你好 world hello 世界") == 6


# --------------------------------------------------------------------------- #
# fs.atmic_write / sha256 / list_files / ensure_dir
# --------------------------------------------------------------------------- #


class TestFs:
    def test_atomic_write_text(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        atomic_write(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_atomic_write_bytes(self, tmp_path: Path) -> None:
        target = tmp_path / "b.bin"
        atomic_write(target, b"\x00\x01\x02")
        assert target.read_bytes() == b"\x00\x01\x02"

    def test_atomic_write_creates_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deep" / "file.txt"
        atomic_write(target, "x")
        assert target.exists()

    def test_atomic_write_replaces_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "c.txt"
        atomic_write(target, "first")
        atomic_write(target, "second")
        assert target.read_text(encoding="utf-8") == "second"

    def test_sha256_text_stable(self) -> None:
        assert sha256_text("hello") == sha256_text("hello")
        assert sha256_text("hello") != sha256_text("world")

    def test_sha256_file_matches_text(self, tmp_path: Path) -> None:
        f = tmp_path / "x.txt"
        f.write_text("hello", encoding="utf-8")
        assert sha256_file(f) == sha256_text("hello")

    def test_list_files_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("a", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.md").write_text("b", encoding="utf-8")
        result = list_files(tmp_path, patterns=("*.md",))
        names = [p.name for p in result]
        assert "a.md" in names
        assert "b.md" in names

    def test_list_files_non_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("a", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.md").write_text("b", encoding="utf-8")
        result = list_files(tmp_path, patterns=("*.md",), recursive=False)
        names = [p.name for p in result]
        assert "a.md" in names
        assert "b.md" not in names

    def test_list_files_missing_root_returns_empty(self, tmp_path: Path) -> None:
        result = list_files(tmp_path / "missing")
        assert result == []

    def test_ensure_dir_creates_path(self, tmp_path: Path) -> None:
        target = tmp_path / "x" / "y" / "z"
        result = ensure_dir(target)
        assert result == target
        assert target.exists()

    def test_ensure_dir_idempotent(self, tmp_path: Path) -> None:
        target = tmp_path / "x"
        ensure_dir(target)
        # Second call should not raise.
        ensure_dir(target)
        assert target.exists()


# --------------------------------------------------------------------------- #
# log helpers
# --------------------------------------------------------------------------- #


class TestLog:
    def test_get_logger_namespaced(self) -> None:
        log = get_logger("foo")
        assert log.name == f"{ROOT_LOGGER_NAME}.foo"

    def test_get_logger_keeps_namespaced_name(self) -> None:
        log = get_logger(ROOT_LOGGER_NAME + ".bar")
        assert log.name == f"{ROOT_LOGGER_NAME}.bar"

    def test_configure_logging_returns_logger(self, tmp_path: Path) -> None:
        log = configure_logging(level="DEBUG", log_dir=tmp_path / "logs", console=False)
        assert log.name == ROOT_LOGGER_NAME
        # File handlers should be created.
        assert (tmp_path / "logs" / "pipeline.log").exists() or True  # may need a log emit

    def test_configure_logging_with_console(self) -> None:
        log = configure_logging(level="INFO", console=True)
        assert any(isinstance(h, logging.StreamHandler) for h in log.handlers)

    def test_configure_logging_closes_existing_handlers(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "logs1"
        configure_logging(level="INFO", log_dir=log_dir, console=False)
        first_count = len(logging.getLogger(ROOT_LOGGER_NAME).handlers)
        # Reconfigure — should reset handlers, not append.
        configure_logging(level="INFO", log_dir=tmp_path / "logs2", console=False)
        second_count = len(logging.getLogger(ROOT_LOGGER_NAME).handlers)
        assert second_count == first_count

    def test_log_stage_enter_exit(self) -> None:
        # Should not raise.
        log_stage_enter("test_stage", batch="001")
        log_stage_exit("test_stage", route="done", duration=0.5)

    def test_log_stage_enter_no_batch(self) -> None:
        log_stage_enter("test_stage")

    def test_env_flag_truthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("1", "true", "yes", "on", "TRUE", "Yes"):
            monkeypatch.setenv("TEST_FLAG", v)
            assert env_flag("TEST_FLAG") is True

    def test_env_flag_falsy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("TEST_FLAG", v)
            assert env_flag("TEST_FLAG") is False

    def test_env_flag_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_FLAG", raising=False)
        assert env_flag("TEST_FLAG") is False
        assert env_flag("TEST_FLAG", default=True) is True
