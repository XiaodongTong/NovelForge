"""Persistent state store.

``state.yaml`` holds global state (current stage, progress, token usage,
paused flag) and a ``checkpoints/`` directory holds per-stage snapshots
with SHA-256 hashes of the produced files.

Public surface:

- :class:`StateStore`     — load/save/checkpoint
- :class:`Checkpoint`     — dataclass for a checkpoint file
- :class:`RecoveryPlan`   — what to do on resume

Design contract: ``plan.md`` §4.2 / ``spec.md`` A3-A4.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from .errors import CheckpointCorrupt, StateError
from .utils.fs import atomic_write, ensure_dir, sha256_file

ISO_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime(ISO_FMT)


def _parse_iso(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+0000"
    return datetime.strptime(value, ISO_FMT)


# --------------------------------------------------------------------------- #
# State model
# --------------------------------------------------------------------------- #


def _empty_progress() -> dict[str, Any]:
    return {
        "outline": "pending",
        "characters": "pending",
        "simulation": "pending",
        "chapters_written": 0,
        "chapters_reviewed": 0,
        "total_words": 0,
    }


def _empty_recovery() -> dict[str, Any]:
    return {
        "last_batch_chapters": [],
        "last_batch_status": "idle",
        "last_decision_reason": None,
    }


def _empty_token_usage() -> dict[str, int]:
    return {"total_input": 0, "total_output": 0}


@dataclass
class State:
    """In-memory representation of the engine's persistent state."""

    current_stage: Optional[str] = None
    pipeline_version: str = "1.0"
    started_at: str = field(default_factory=_now_iso)
    last_checkpoint_at: Optional[str] = None
    progress: dict[str, Any] = field(default_factory=_empty_progress)
    recovery: dict[str, Any] = field(default_factory=_empty_recovery)
    token_usage: dict[str, int] = field(default_factory=_empty_token_usage)
    last_review_iterations: int = 0
    paused: bool = False
    paused_reason: Optional[str] = None
    forced_stage: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "current_stage": self.current_stage,
            "pipeline_version": self.pipeline_version,
            "started_at": self.started_at,
            "last_checkpoint_at": self.last_checkpoint_at,
            "progress": copy.deepcopy(self.progress),
            "recovery": copy.deepcopy(self.recovery),
            "token_usage": dict(self.token_usage),
            "last_review_iterations": self.last_review_iterations,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "forced_stage": self.forced_stage,
        }
        # Persist the ``extra`` bag so per-stage counters (e.g.
        # ``review_iterations``) survive a save/load round-trip.  The
        # orchestrator relies on this to enforce the review-loop ceiling
        # after a process restart.
        if self.extra:
            data["extra"] = copy.deepcopy(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "State":
        if not isinstance(data, Mapping):
            raise StateError("state.yaml root must be a mapping")
        extra_raw = data.get("extra") or {}
        if not isinstance(extra_raw, Mapping):
            extra_raw = {}
        return cls(
            current_stage=data.get("current_stage"),
            pipeline_version=data.get("pipeline_version", "1.0"),
            started_at=data.get("started_at", _now_iso()),
            last_checkpoint_at=data.get("last_checkpoint_at"),
            progress=_merge(_empty_progress(), data.get("progress", {})),
            recovery=_merge(_empty_recovery(), data.get("recovery", {})),
            token_usage=_merge(_empty_token_usage(), data.get("token_usage", {})),
            last_review_iterations=int(data.get("last_review_iterations", 0) or 0),
            paused=bool(data.get("paused", False)),
            paused_reason=data.get("paused_reason"),
            forced_stage=data.get("forced_stage"),
            extra=dict(extra_raw),
        )

    def snapshot(self) -> dict[str, Any]:
        return self.to_dict()


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, Mapping):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# Checkpoint model
# --------------------------------------------------------------------------- #


@dataclass
class Checkpoint:
    """A persisted checkpoint for a single stage batch."""

    stage: str
    batch: str
    files: list[dict[str, str]]  # [{path, sha256, size}]
    timestamp: str
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "batch": self.batch,
            "files": list(self.files),
            "timestamp": self.timestamp,
            **({"extras": dict(self.extras)} if self.extras else {}),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Checkpoint":
        if not isinstance(data, Mapping):
            raise CheckpointCorrupt("checkpoint root is not a mapping")
        stage = data.get("stage")
        batch = data.get("batch")
        files = data.get("files")
        timestamp = data.get("timestamp") or _now_iso()
        if not isinstance(stage, str) or not stage:
            raise CheckpointCorrupt("checkpoint is missing 'stage'")
        if not isinstance(batch, str) or not batch:
            raise CheckpointCorrupt("checkpoint is missing 'batch'")
        if not isinstance(files, list):
            raise CheckpointCorrupt("checkpoint 'files' must be a list")
        return cls(
            stage=stage,
            batch=batch,
            files=list(files),
            timestamp=timestamp,
            extras=dict(data.get("extras", {}) or {}),
        )

    # -- integrity -------------------------------------------------------

    def verify(self, project_root: Path) -> tuple[bool, str]:
        """Re-hash every file under ``project_root``.

        Returns ``(ok, reason)``.  ``reason`` is empty on success.
        """

        for entry in self.files:
            if not isinstance(entry, Mapping):
                return False, "file entry is not a mapping"
            rel = entry.get("path")
            expected = entry.get("sha256")
            if not isinstance(rel, str) or not isinstance(expected, str):
                return False, "file entry missing path/sha256"
            full = (project_root / rel).resolve()
            if not full.exists():
                return False, f"file missing: {rel}"
            actual = sha256_file(full)
            if actual != expected:
                return (
                    False,
                    f"hash mismatch for {rel}: expected {expected[:12]}…, got {actual[:12]}…",
                )
        return True, ""


# --------------------------------------------------------------------------- #
# StateStore
# --------------------------------------------------------------------------- #


class StateStore:
    """Owns the ``.novelforge/`` directory under a project root."""

    STATE_FILENAME = "state.yaml"
    CHECKPOINTS_DIR = "checkpoints"
    LOGS_DIR = "logs"
    METRICS_DIR = "metrics"

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / self.STATE_FILENAME
        self.checkpoints_dir = self.state_dir / self.CHECKPOINTS_DIR
        self.logs_dir = self.state_dir / self.LOGS_DIR
        self.metrics_dir = self.state_dir / self.METRICS_DIR

    # -- IO ---------------------------------------------------------------

    def ensure_dirs(self) -> None:
        ensure_dir(self.state_dir)
        ensure_dir(self.checkpoints_dir)
        ensure_dir(self.logs_dir)
        ensure_dir(self.metrics_dir)

    @property
    def exists(self) -> bool:
        return self.state_path.exists()

    def load(self) -> State:
        """Load the on-disk state.

        Returns a fresh :class:`State` if ``state.yaml`` does not exist.
        Raises :class:`StateError` if the file is corrupt.
        """

        if not self.state_path.exists():
            return State()
        try:
            raw = yaml.safe_load(self.state_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise StateError(f"state.yaml is not valid YAML: {exc}") from exc
        if raw is None:
            return State()
        return State.from_dict(raw)

    def save(self, state: State) -> None:
        """Atomically persist ``state`` to ``state.yaml``."""

        self.ensure_dirs()
        atomic_write(self.state_path, yaml.safe_dump(state.to_dict(), sort_keys=False))

    def write(self, **changes: Any) -> State:
        """Convenience wrapper: load → mutate → save → return new state."""

        state = self.load()
        new = replace(state, **changes)
        self.save(new)
        return new

    # -- Checkpoints -----------------------------------------------------

    def checkpoint_path(self, stage: str, batch: str) -> Path:
        # Keep batch path safe (no slashes in filename).
        safe_batch = str(batch).replace("/", "_").replace("\\", "_")
        return self.checkpoints_dir / f"{stage}-{safe_batch}.yaml"

    def write_checkpoint(self, cp: Checkpoint) -> Path:
        self.ensure_dirs()
        path = self.checkpoint_path(cp.stage, cp.batch)
        atomic_write(path, yaml.safe_dump(cp.to_dict(), sort_keys=False))
        return path

    def read_checkpoint(self, path: Path) -> Checkpoint:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise CheckpointCorrupt(
                f"checkpoint yaml invalid: {path}", path=str(path), reason=str(exc)
            ) from exc
        return Checkpoint.from_dict(raw)

    def list_checkpoints(self) -> list[Path]:
        if not self.checkpoints_dir.exists():
            return []
        return sorted(self.checkpoints_dir.glob("*.yaml"))

    def latest_checkpoint(self) -> Optional[Path]:
        """Return the most recently *written* checkpoint, not the last
        alphabetical one.  We sort by mtime so a stage that runs later
        (e.g. ``write_chapter``) is correctly identified as newer than
        one that runs earlier (e.g. ``review_outline``), regardless of
        how their names sort lexicographically.
        """

        files = self.list_checkpoints()
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    def verify_checkpoint(self, path: Path, project_root: Path) -> Checkpoint:
        cp = self.read_checkpoint(path)
        ok, reason = cp.verify(project_root)
        if not ok:
            raise CheckpointCorrupt(
                f"checkpoint corrupt: {reason}",
                path=str(path),
                reason=reason,
            )
        return cp

    # -- Recovery --------------------------------------------------------

    def recovery_plan(
        self,
        project_root: Path,
        stages: Sequence[str],
        force_stage: Optional[str] = None,
    ) -> "RecoveryPlan":
        """Decide which stage to run next.

        Decision tree (per ``plan.md`` §4.2):

        1. If ``force_stage`` is given → use it. Reason:
           ``forced-stage=<id>, reason=…``.
        2. If ``state.yaml`` missing → return ``stage=stages[0]``,
           reason ``fresh_start``.
        3. Look for the latest valid checkpoint.
        4. If a valid checkpoint exists → resume from the stage it
           belongs to. Reason: ``resuming from <stage>-<batch>``.
        5. If the latest checkpoint is corrupt (or all of them are),
           set ``paused=True`` and ``paused_reason='all_checkpoints_corrupt'``
           but do **not** mutate ``state.yaml`` automatically — the user
           is expected to either delete the corrupt file or pass
           ``--force-stage``.  Reason: ``all_checkpoints_corrupt``.

        The ``stages`` argument is normalised to a list of stage ids
        (string), regardless of whether the caller passed strings or
        :class:`novelforge.config.StageConfig` records.
        """

        stage_ids = [s if isinstance(s, str) else getattr(s, "id", str(s)) for s in stages]

        if force_stage:
            if force_stage not in stage_ids:
                return RecoveryPlan(
                    next_stage=stage_ids[0] if stage_ids else None,
                    reason=f"forced-stage={force_stage} (unknown; falling back to start)",
                    paused=False,
                    forced=True,
                    forced_stage=force_stage,
                )
            return RecoveryPlan(
                next_stage=force_stage,
                reason=f"forced-stage={force_stage}",
                paused=False,
                forced=True,
                forced_stage=force_stage,
            )

        if not stage_ids:
            return RecoveryPlan(next_stage=None, reason="no_stages", paused=False)

        if not self.state_path.exists():
            return RecoveryPlan(
                next_stage=stage_ids[0],
                reason="fresh_start",
                paused=False,
            )

        state = self.load()
        if state.paused:
            return RecoveryPlan(
                next_stage=state.current_stage or stage_ids[0],
                reason=f"resuming_paused: {state.paused_reason}",
                paused=True,
                paused_reason=state.paused_reason,
            )

        checkpoints = self.list_checkpoints()
        if not checkpoints:
            return RecoveryPlan(
                next_stage=stage_ids[0],
                reason="no_checkpoints",
                paused=False,
            )

        # Per plan §4.2: find the latest *valid* checkpoint by iterating
        # from newest to oldest.  Only pause when ALL are corrupt.
        sorted_by_mtime = sorted(
            checkpoints, key=lambda p: p.stat().st_mtime, reverse=True
        )
        cp: Optional[Checkpoint] = None
        corrupt_paths: list[str] = []
        for candidate in sorted_by_mtime:
            try:
                cp = self.verify_checkpoint(candidate, project_root)
                break
            except CheckpointCorrupt as exc:
                corrupt_paths.append(str(exc.path or candidate))
                continue

        if cp is None:
            return RecoveryPlan(
                next_stage=None,
                reason=f"all_checkpoints_corrupt: {len(corrupt_paths)} checkpoint(s) checked",
                paused=True,
                paused_reason="all_checkpoints_corrupt",
                corrupt_checkpoint=corrupt_paths[0] if corrupt_paths else None,
            )

        next_idx = stage_ids.index(cp.stage) + 1 if cp.stage in stage_ids else 0
        if next_idx >= len(stage_ids):
            return RecoveryPlan(
                next_stage=None,
                reason="pipeline_complete",
                paused=False,
            )
        return RecoveryPlan(
            next_stage=stage_ids[next_idx],
            reason=f"resuming from {cp.stage}-{cp.batch}",
            paused=False,
            checkpoint=cp,
        )


@dataclass
class RecoveryPlan:
    next_stage: Optional[str]
    reason: str
    paused: bool = False
    paused_reason: Optional[str] = None
    forced: bool = False
    forced_stage: Optional[str] = None
    checkpoint: Optional[Checkpoint] = None
    corrupt_checkpoint: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_stage": self.next_stage,
            "reason": self.reason,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "forced": self.forced,
            "forced_stage": self.forced_stage,
            "corrupt_checkpoint": self.corrupt_checkpoint,
        }
