"""Token usage logging (JSONL)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

ISO_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime(ISO_FMT)


class TokenUsageLog:
    """Append-only JSONL file of token usage records.

    The file lives at ``.novelforge/logs/token-usage.log`` and is shared
    across processes; writes are protected by a process-local lock.
    Each record has the form::

        {"timestamp": "2026-06-06T00:00:00+0000",
         "stage": "generate_outline",
         "batch": "001",
         "model": "claude-opus-4-7",
         "input_tokens": 1234,
         "output_tokens": 4321,
         "duration": 12.3,
         "exit_code": 0}

    ``input_tokens`` and ``output_tokens`` are always present (default 0).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            # touch
            self.path.touch()

    def append(
        self,
        *,
        stage: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        batch: Optional[str] = None,
        model: Optional[str] = None,
        duration: Optional[float] = None,
        exit_code: Optional[int] = None,
        extras: Optional[Mapping[str, Any]] = None,
    ) -> None:
        record = {
            "timestamp": _now_iso(),
            "stage": stage,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
        }
        if batch is not None:
            record["batch"] = str(batch)
        if model is not None:
            record["model"] = str(model)
        if duration is not None:
            record["duration"] = float(duration)
        if exit_code is not None:
            record["exit_code"] = int(exit_code)
        if extras:
            for k, v in extras.items():
                if k not in record:
                    record[k] = v
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self.ensure()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(json.loads(line))
        return out

    def total_tokens(self) -> tuple[int, int]:
        """Return ``(total_input, total_output)`` over all records."""

        tin = 0
        tout = 0
        for record in self.read_all():
            tin += int(record.get("input_tokens", 0) or 0)
            tout += int(record.get("output_tokens", 0) or 0)
        return tin, tout
