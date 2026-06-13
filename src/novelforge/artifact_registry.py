"""Runtime artifact registry (Phase 1.2).

The :class:`ArtifactRegistry` is the in-memory data-flow mediator between
stages.  When a stage completes its ``produces`` contract successfully,
the resulting file paths are registered here under their declared
``alias``.  Downstream stages query the registry via the
``{{upstream.<id>.<alias>}}`` placeholder family.

Storage forms (spec §AC-1, §AC-7, §AC-8):

- **single-product stage** — ``registry[stage_id][alias]`` is a single
  :class:`pathlib.Path`.
- **batch stage** (``batch: N``) or **split stage** (``produces[].split``)
  — the same key is a ``list[Path]``.

The registry is serialised to ``state.yaml.extra.artifacts`` (D12) so
checkpoints / resumes see a consistent view.  The on-disk form mirrors
the in-memory form: ``{stage_id: {alias: "str" | ["str", ...]}}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from .errors import StateError

__all__ = ["ArtifactRegistry", "ArtifactValue"]


ArtifactValue = Union[Path, list[Path]]


class ArtifactRegistry:
    """A flat ``{stage_id: {alias: Path | list[Path]}}`` store.

    The registry is intentionally schema-light: callers (orchestrator,
    ContextAssembler) know whether a given (stage_id, alias) is expected
    to be a single value or a list, and they explicit-access accordingly.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, ArtifactValue]] = {}

    # -- registration ---------------------------------------------------

    def register(self, stage_id: str, alias: str, value: ArtifactValue) -> None:
        """Persist ``value`` under ``stage_id.alias``.

        Re-registering an existing ``(stage_id, alias)`` overwrites the
        previous value (used when a stage is re-run after
        ``StageIncomplete`` / ``VerifyFailed``).
        """

        if not stage_id:
            raise ValueError("stage_id is required for register()")
        if not alias:
            raise ValueError("alias is required for register()")
        if isinstance(value, list):
            cleaned: list[Path] = []
            for v in value:
                if not isinstance(v, Path):
                    raise TypeError(
                        f"ArtifactRegistry.register: list elements must be Path, "
                        f"got {type(v).__name__}"
                    )
                cleaned.append(v)
            stored: ArtifactValue = cleaned
        elif isinstance(value, Path):
            stored = value
        else:
            raise TypeError(
                f"ArtifactRegistry.register: value must be Path or list[Path], "
                f"got {type(value).__name__}"
            )
        bucket = self._data.setdefault(stage_id, {})
        bucket[alias] = stored

    # -- lookup ---------------------------------------------------------

    def get(self, stage_id: str, alias: str) -> ArtifactValue:
        """Return the stored value, raising if missing.

        Callers that expect a single value should call :meth:`get_one`;
        callers that expect a list should call :meth:`get_list`.  The
        generic :meth:`get` is provided for tests / debugging.
        """

        bucket = self._data.get(stage_id)
        if bucket is None:
            raise KeyError(
                f"unknown upstream stage_id {stage_id!r}; "
                f"registered: {sorted(self._data)}"
            )
        if alias not in bucket:
            raise KeyError(
                f"unknown upstream alias {alias!r} under stage {stage_id!r}; "
                f"registered: {sorted(bucket)}"
            )
        return bucket[alias]

    def get_one(self, stage_id: str, alias: str) -> Path:
        v = self.get(stage_id, alias)
        if isinstance(v, list):
            raise TypeError(
                f"alias {stage_id}.{alias} is a list ({len(v)} items); "
                f"use [*] suffix"
            )
        return v

    def get_list(self, stage_id: str, alias: str) -> list[Path]:
        v = self.get(stage_id, alias)
        if not isinstance(v, list):
            raise TypeError(
                f"alias {stage_id}.{alias} is a single Path; "
                f"[*] suffix not allowed"
            )
        return list(v)

    def has(self, stage_id: str, alias: str) -> bool:
        bucket = self._data.get(stage_id)
        return bool(bucket) and alias in bucket

    def stages(self) -> list[str]:
        return list(self._data.keys())

    def aliases(self, stage_id: str) -> list[str]:
        bucket = self._data.get(stage_id)
        return list(bucket.keys()) if bucket else []

    def is_list(self, stage_id: str, alias: str) -> bool:
        """Return True iff the stored value is a list."""

        bucket = self._data.get(stage_id)
        if not bucket or alias not in bucket:
            return False
        return isinstance(bucket[alias], list)

    # -- serialisation --------------------------------------------------

    def to_dict(self) -> dict[str, dict[str, Any]]:
        """Serialise to a yaml-friendly structure.

        Each leaf is either a path string (single product) or a list of
        path strings (batch / split).  Paths are rendered relative to a
        project root by the orchestrator before this is persisted.
        """

        out: dict[str, dict[str, Any]] = {}
        for sid, bucket in self._data.items():
            out[sid] = {}
            for alias, value in bucket.items():
                if isinstance(value, list):
                    out[sid][alias] = [str(p) for p in value]
                else:
                    out[sid][alias] = str(value)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRegistry":
        reg = cls()
        if not isinstance(data, Mapping):
            raise StateError("artifacts payload must be a mapping")
        for sid, bucket in data.items():
            if not isinstance(bucket, Mapping):
                raise StateError(
                    f"artifacts[{sid!r}] must be a mapping; "
                    f"got {type(bucket).__name__}"
                )
            for alias, raw in bucket.items():
                if isinstance(raw, list):
                    reg.register(sid, alias, [Path(p) for p in raw])
                elif isinstance(raw, str):
                    reg.register(sid, alias, Path(raw))
                else:
                    raise StateError(
                        f"artifacts[{sid!r}][{alias!r}] must be str or list[str]; "
                        f"got {type(raw).__name__}"
                    )
        return reg

    # -- introspection --------------------------------------------------

    def __len__(self) -> int:
        return sum(len(b) for b in self._data.values())

    def __contains__(self, stage_id: object) -> bool:
        return stage_id in self._data

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"ArtifactRegistry(stages={list(self._data)})"
