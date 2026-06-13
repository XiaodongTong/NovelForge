"""DoneWhen / CheckSpec — second-layer completion verification.

This module owns the **declarative** part of the completion contract
(spec §4.2): a stage's ``done_when.checks`` list is a sequence of
independent assertions about the produced files.  The runtime calls
:func:`run_done_when` after the stage has written its ``produces`` to
disk; any failing check becomes a :class:`VerifyFailed` that triggers a
whole-stage retry (tier C).

Six check kinds are supported (D9):

- ``exists``     — the file at ``target`` exists on disk.
- ``min_chars``  — character length of the file content ≥ ``value``.
- ``min_bytes``  — byte length of the file content ≥ ``value``.
- ``regex_match``— content matches ``pattern`` (search semantics).
- ``json_field`` — the JSON at ``target`` has field ``field`` whose
                   value equals (or contains) ``value``.
- ``callable``   — ``"module:func"`` resolves to a Python callable
                   that takes ``(content, **expected)`` and returns a
                   bool.

Each check operates on a single ``target`` path.  Placeholders such as
``{{num}}`` are substituted before evaluation so batch / split stages
can validate each produced file individually.
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .errors import ConfigError, VerifyFailed

__all__ = [
    "CheckSpec",
    "DoneWhenSpec",
    "CheckOutcome",
    "CheckResult",
    "run_done_when",
    "run_check",
    "substitute_placeholders",
    "DEFAULT_COMPLETION_SIGNAL",
    "DEFAULT_MAX_ATTEMPTS",
]


# Suffixes appended to the prompt before invoke (spec §4.2).  The
# COMPLETION_SUFFIX is only appended when the stage's
# ``done_when.completion_signal`` is non-null.
EXECUTION_SUFFIX = (
    "\n\n<!-- NovelForge execution context -->\n"
    "Produce exactly the artefacts declared by this stage. "
    "Do not output prose outside the declared produces."
)

DEFAULT_COMPLETION_SIGNAL = "<promise>COMPLETE</promise>"
COMPLETION_SUFFIX = (
    f"\n\nWhen you have completed all declared produces, end your "
    f"response with a line containing exactly: {DEFAULT_COMPLETION_SIGNAL}"
)

DEFAULT_MAX_ATTEMPTS = 3
_VALID_KINDS = frozenset(
    {"exists", "min_chars", "min_bytes", "regex_match", "json_field", "callable"}
)
_VALID_MODES = frozenset({"all", "any"})


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckSpec:
    """A single ``done_when.checks[]`` entry.

    The ``target`` is a project-relative path with optional
    ``{{num}}`` / ``{{name}}`` placeholders that are substituted per
    file during evaluation.
    """

    kind: str
    target: str
    value: Any = None
    field: Optional[str] = None
    pattern: Optional[str] = None
    callable: Optional[str] = None  # "module:func" spec for kind=callable

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ConfigError(
                f"done_when.checks[].kind must be one of "
                f"{sorted(_VALID_KINDS)}; got {self.kind!r}"
            )
        if not isinstance(self.target, str) or not self.target.strip():
            raise ConfigError(
                "done_when.checks[].target must be a non-empty string"
            )
        if self.kind == "callable" and not self.callable:
            raise ConfigError(
                "done_when.checks[].callable is required when kind='callable'"
            )
        if self.kind == "regex_match" and not self.pattern:
            raise ConfigError(
                "done_when.checks[].pattern is required when kind='regex_match'"
            )
        if self.kind == "json_field" and not self.field:
            raise ConfigError(
                "done_when.checks[].field is required when kind='json_field'"
            )
        if self.kind in {"min_chars", "min_bytes"} and not isinstance(
            self.value, int
        ):
            raise ConfigError(
                f"done_when.checks[].value must be int for kind={self.kind!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "target": self.target}
        if self.value is not None:
            d["value"] = self.value
        if self.field is not None:
            d["field"] = self.field
        if self.pattern is not None:
            d["pattern"] = self.pattern
        if self.callable is not None:
            d["callable"] = self.callable
        return d


@dataclass(frozen=True)
class DoneWhenSpec:
    """The full ``done_when:`` block of a stage."""

    completion_signal: Optional[str] = DEFAULT_COMPLETION_SIGNAL
    checks: tuple[CheckSpec, ...] = ()
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    mode: str = "all"  # all | any (applies to checks)

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or isinstance(
            self.max_attempts, bool
        ):
            raise ConfigError(
                "done_when.max_attempts must be an integer"
            )
        if self.max_attempts < 1:
            raise ConfigError(
                f"done_when.max_attempts must be >= 1 (got {self.max_attempts})"
            )
        if self.mode not in _VALID_MODES:
            raise ConfigError(
                f"done_when.mode must be one of {sorted(_VALID_MODES)}; "
                f"got {self.mode!r}"
            )
        if self.completion_signal is not None and (
            not isinstance(self.completion_signal, str)
            or not self.completion_signal.strip()
        ):
            raise ConfigError(
                "done_when.completion_signal must be a non-empty string or null"
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "completion_signal": self.completion_signal,
            "max_attempts": self.max_attempts,
            "mode": self.mode,
            "checks": [c.to_dict() for c in self.checks],
        }
        return d


# --------------------------------------------------------------------------- #
# Placeholder substitution
# --------------------------------------------------------------------------- #


# Matches ``{{name}}``, ``{{name:03d}}``, ``{{name|slug}}`` and the
# combined ``{{name:03d|slug}}`` form.  Same pattern used by
# :mod:`novelforge.config` so placeholder rules stay in lock-step.
_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)(?::[^}|]+)?(?:\|[^}|]+)?\s*\}\}"
)


def substitute_placeholders(text: str, values: Mapping[str, Any]) -> str:
    """Substitute ``{{name}}`` / ``{{name:03d}}`` / ``{{name|slug}}`` markers.

    The format spec (``:03d``) and the filter (``|slug``) are applied
    to the resolved value before substitution; unknown placeholders
    raise :class:`ConfigError` (loud, not silent).
    """

    def repl(match: re.Match[str]) -> str:
        full = match.group(0)
        name = match.group(1)
        if name not in values:
            raise ConfigError(
                f"placeholder {name!r} is not available; "
                f"defined: {sorted(values)}"
            )
        value = values[name]
        # Reuse the output_parser's formatter so substitution rules
        # are identical between path templates and check targets.
        from .claude.output_parser import _format_value
        inner = full[2:-2].strip()  # strip the {{ }} wrapper
        return _format_value(inner, value)

    return _PLACEHOLDER_RE.sub(repl, text)


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass
class CheckOutcome:
    """The result of evaluating a single :class:`CheckSpec`."""

    spec: CheckSpec
    passed: bool
    target: str
    expected: Any = None
    actual: Any = None
    reason: str = ""


@dataclass
class CheckResult:
    """The aggregate result of a :class:`DoneWhenSpec` evaluation."""

    passed: bool
    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def first_failure(self) -> Optional[CheckOutcome]:
        for o in self.outcomes:
            if not o.passed:
                return o
        return None


# --------------------------------------------------------------------------- #
# Per-kind evaluators
# --------------------------------------------------------------------------- #


def _read_text(target: Path) -> str:
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8", errors="ignore")


def _check_exists(target: Path) -> tuple[bool, str]:
    return target.exists(), "file exists" if target.exists() else "file missing"


def _check_min_chars(target: Path, value: int) -> tuple[bool, int, str]:
    if not target.exists():
        return False, 0, "file missing"
    content = _read_text(target)
    actual = len(content)
    return actual >= value, actual, ""


def _check_min_bytes(target: Path, value: int) -> tuple[bool, int, str]:
    if not target.exists():
        return False, 0, "file missing"
    actual = target.stat().st_size
    return actual >= value, actual, ""


def _check_regex_match(target: Path, pattern: str) -> tuple[bool, str, str]:
    if not target.exists():
        return False, "", "file missing"
    content = _read_text(target)
    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        raise ConfigError(
            f"done_when.checks[].pattern is not a valid regex: {exc}"
        ) from exc
    match = regex.search(content)
    if match is None:
        return False, "", f"pattern not found"
    return True, match.group(0), ""


def _check_json_field(
    target: Path, field_name: str, expected: Any
) -> tuple[bool, Any, str]:
    if not target.exists():
        return False, None, "file missing"
    content = _read_text(target)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, None, f"not JSON: {exc.msg}"
    if not isinstance(payload, Mapping) or field_name not in payload:
        return False, None, f"field {field_name!r} missing"
    actual = payload[field_name]
    if expected is None:
        return True, actual, ""
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False, actual, "expected list value"
        missing = [e for e in expected if e not in actual]
        return (not missing), actual, (
            f"missing entries: {missing}" if missing else ""
        )
    return (actual == expected), actual, ""


def _resolve_callable(spec: str) -> Callable[..., bool]:
    """Resolve a ``"module:func"`` callable reference."""

    if ":" not in spec:
        raise ConfigError(
            f"callable spec {spec!r} must be 'module:func'"
        )
    module_name, _, func_name = spec.partition(":")
    if not module_name or not func_name:
        raise ConfigError(
            f"callable spec {spec!r} is missing module or func"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(
            f"callable module {module_name!r} cannot be imported: {exc}"
        ) from exc
    func = getattr(module, func_name, None)
    if not callable(func):
        raise ConfigError(
            f"callable {module_name}:{func_name} is not callable"
        )
    return func


def _check_callable(
    target: Path, spec: str, expected: Mapping[str, Any]
) -> tuple[bool, Any, str]:
    func = _resolve_callable(spec)
    if not target.exists():
        return False, None, "file missing"
    content = _read_text(target)
    try:
        result = bool(func(content, **dict(expected)))
    except TypeError as exc:
        # Callable signature mismatch — surface as a config error.
        raise ConfigError(
            f"callable {spec!r} raised TypeError: {exc}"
        ) from exc
    return result, result, "" if result else "callable returned falsy"


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def run_check(
    spec: CheckSpec,
    project_root: Path,
    placeholders: Mapping[str, Any],
) -> CheckOutcome:
    """Evaluate a single check spec; return a :class:`CheckOutcome`."""

    target_rel = substitute_placeholders(spec.target, placeholders)
    target = (Path(project_root) / target_rel).resolve()
    expected: Any = None
    actual: Any = None
    passed = False
    reason = ""

    if spec.kind == "exists":
        passed, reason = _check_exists(target)
    elif spec.kind == "min_chars":
        expected = spec.value
        passed, actual, reason = _check_min_chars(target, spec.value)
    elif spec.kind == "min_bytes":
        expected = spec.value
        passed, actual, reason = _check_min_bytes(target, spec.value)
    elif spec.kind == "regex_match":
        expected = spec.pattern
        passed, actual, reason = _check_regex_match(target, spec.pattern)
    elif spec.kind == "json_field":
        expected = spec.value
        passed, actual, reason = _check_json_field(
            target, spec.field, spec.value
        )
    elif spec.kind == "callable":
        expected = {"value": spec.value} if spec.value is not None else {}
        passed, actual, reason = _check_callable(target, spec.callable, expected)
    else:  # pragma: no cover - validated in CheckSpec
        raise ConfigError(f"unknown check kind {spec.kind!r}")

    return CheckOutcome(
        spec=spec,
        passed=passed,
        target=target_rel,
        expected=expected,
        actual=actual,
        reason=reason,
    )


def run_done_when(
    spec: DoneWhenSpec,
    files: Sequence[Path],
    project_root: Path,
    placeholders: Mapping[str, Any],
    *,
    per_file_placeholders: Optional[Sequence[Mapping[str, Any]]] = None,
) -> CheckResult:
    """Evaluate every check in ``spec``.

    ``files`` is the list of paths just produced by the stage; each is
    evaluated as a separate target so batch / split stages can validate
    every artefact individually.

    ``placeholders`` is the default substitution map applied to every
    file (typically ``{{num}}`` for batch items, ``{{stage_id}}`` /
    ``{{batch}}`` for logging).  When the caller also passes
    ``per_file_placeholders`` (one mapping per file in ``files``), the
    per-file map **overrides** the default for that file.

    Returns a :class:`CheckResult` whose ``passed`` flag honours
    ``spec.mode`` (``all`` → all checks pass; ``any`` → at least one
    passes).
    """

    outcomes: list[CheckOutcome] = []
    # Each file produced by the stage becomes its own placeholder set
    # so checks referencing {{num}} / {{name}} can be evaluated per
    # artefact.
    file_placeholders_list: list[dict[str, Any]] = []
    if not files:
        file_placeholders_list = [dict(placeholders)]
    else:
        for idx, f in enumerate(files):
            merged = dict(placeholders)
            merged.setdefault("path", str(f))
            if per_file_placeholders is not None and idx < len(
                per_file_placeholders
            ):
                merged.update(per_file_placeholders[idx])
            file_placeholders_list.append(merged)

    for check in spec.checks:
        for ph in file_placeholders_list:
            outcomes.append(run_check(check, project_root, ph))

    if spec.mode == "all":
        passed = all(o.passed for o in outcomes) if outcomes else True
    else:  # mode == "any"
        passed = any(o.passed for o in outcomes) if outcomes else True

    return CheckResult(passed=passed, outcomes=outcomes)


def verify_or_raise(
    spec: DoneWhenSpec,
    files: Sequence[Path],
    project_root: Path,
    placeholders: Mapping[str, Any],
    *,
    stage_id: str,
    attempt: int,
    per_file_placeholders: Optional[Sequence[Mapping[str, Any]]] = None,
) -> CheckResult:
    """Run :func:`run_done_when`; raise :class:`VerifyFailed` on miss."""

    result = run_done_when(
        spec,
        files,
        project_root,
        placeholders,
        per_file_placeholders=per_file_placeholders,
    )
    if result.passed:
        return result
    failure = result.first_failure
    if failure is None:  # pragma: no cover - defensive
        raise VerifyFailed(
            f"stage {stage_id!r} done_when failed (no detail)",
            stage_id=stage_id,
            attempt=attempt,
        )
    raise VerifyFailed(
        f"stage {stage_id!r} check failed: kind={failure.spec.kind} "
        f"target={failure.target} expected={failure.expected!r} "
        f"actual={failure.actual!r} reason={failure.reason!r}",
        stage_id=stage_id,
        attempt=attempt,
        target=failure.target,
        kind=failure.spec.kind,
        expected=failure.expected,
        actual=failure.actual,
    )
