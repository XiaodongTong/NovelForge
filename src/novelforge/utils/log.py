"""Logging configuration helpers."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Project-wide logger namespace.  All modules in the engine log via
# ``logging.getLogger("novelforge.<module>")`` so a single ``configure_logging``
# call sets up handlers consistently.
ROOT_LOGGER_NAME = "novelforge"

_STAGE_ENTER = "stage_enter"
_STAGE_EXIT = "stage_exit"
_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    console: bool = True,
) -> logging.Logger:
    """Configure the ``novelforge`` logger tree.

    - ``level`` is the global log level (e.g. ``"INFO"``, ``"DEBUG"``).
    - ``log_dir`` enables file rotation. When set, three RotatingFileHandlers
      are attached: ``pipeline.log`` (INFO+), ``errors.log`` (ERROR+) and
      ``token-usage.log`` is **not** attached here; it lives in
      ``claude.tokens`` which writes structured JSONL directly.
    - When ``console`` is True, a stream handler writes to stderr.
    """

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    formatter = logging.Formatter(_DEFAULT_FORMAT, _DATE_FORMAT)

    # Close any existing handlers so we don't leak file descriptors when
    # the function is invoked repeatedly (e.g. by tests).
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass
        logger.removeHandler(handler)

    if console:
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        pipeline = RotatingFileHandler(
            log_dir / "pipeline.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        pipeline.setLevel(logging.INFO)
        pipeline.setFormatter(formatter)
        logger.addHandler(pipeline)

        errors = RotatingFileHandler(
            log_dir / "errors.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        errors.setLevel(logging.ERROR)
        errors.setFormatter(formatter)
        logger.addHandler(errors)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``novelforge`` tree."""

    if not name.startswith(ROOT_LOGGER_NAME):
        name = f"{ROOT_LOGGER_NAME}.{name}"
    return logging.getLogger(name)


def log_stage_enter(stage_id: str, batch: Optional[str] = None) -> None:
    """Emit a ``stage_enter`` log marker for the current stage."""

    get_logger("pipeline").info(
        "stage_enter %s%s",
        stage_id,
        f" batch={batch}" if batch else "",
        extra={"event": _STAGE_ENTER, "stage": stage_id, "batch": batch},
    )


def log_stage_exit(stage_id: str, route: str, duration: float) -> None:
    """Emit a ``stage_exit`` log marker when a stage finishes."""

    get_logger("pipeline").info(
        "stage_exit %s route=%s duration=%.3fs",
        stage_id,
        route,
        duration,
        extra={
            "event": _STAGE_EXIT,
            "stage": stage_id,
            "route": route,
            "duration": duration,
        },
    )


def env_flag(name: str, default: bool = False) -> bool:
    """Return whether an env var looks truthy (1/true/yes/on)."""

    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
