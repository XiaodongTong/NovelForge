"""Configuration layer.

Loads and validates ``novel-project.yaml`` files.  The exported
:class:`NovelProjectConfig` is a frozen dataclass that downstream
modules (state, orchestrator, context, …) can type-check against.

The stage schema (spec §4.1) is the **contract** model:

- :class:`ProduceSpec` — one of the ``produces[]`` entries (path + alias
  + optional ``split`` regex).
- :class:`StageConfig` — the per-stage record with ``id``, ``model``,
  ``prompt``, ``produces``, ``done_when``, ``consumes``, ``batch``,
  ``on_failure``, ``enabled``.
- :class:`CheckSpec` / :class:`DoneWhenSpec` are re-exported from
  :mod:`novelforge.verify` so the contract types live in one place.

v3 fields (``template``, ``stages_override``, ``scaffold_from``) are no
longer accepted — :func:`load_config` raises :class:`ConfigError` on
sight (spec §AC-15).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from .errors import ConfigError, SchemaInvalid
from .utils.log import get_logger as _get_logger
from .verify import CheckSpec, DoneWhenSpec  # re-exported below

_log = _get_logger("config")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_ON_FAILURE_VALUES = frozenset({"pause", "skip", "fail"})
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)(?::[^}|]+)?(?:\|[^}|]+)?\s*\}\}")


# --------------------------------------------------------------------------- #
# ProduceSpec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProduceSpec:
    """One ``produces[]`` entry (spec §4.1).

    A produce is a single output file (or, when ``split`` is set, a
    single regex-driven multi-file artefact).  Each produce is bound to
    a unique ``alias`` so downstream stages can reference it via
    ``{{upstream.<id>.<alias>}}``.
    """

    path: str
    alias: str
    split: Optional[str] = None
    form: str = "text"  # "text" | "json" — set by validate_path_form

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ConfigError(
                "produces[].path must be a non-empty string"
            )
        if not isinstance(self.alias, str) or not self.alias.strip():
            raise ConfigError(
                "produces[].alias must be a non-empty string"
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.alias):
            raise ConfigError(
                f"produces[].alias {self.alias!r} must be a valid identifier "
                f"(letters, digits, underscore; no leading digit)"
            )
        if self.split is not None:
            if not isinstance(self.split, str) or not self.split.strip():
                raise ConfigError(
                    f"produces[].split for alias {self.alias!r} must be a "
                    f"non-empty regex when present"
                )
        if self.form not in {"text", "json"}:
            raise ConfigError(
                f"produces[].form must be 'text' or 'json'; got {self.form!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"path": self.path, "alias": self.alias}
        if self.split:
            d["split"] = self.split
        return d


# --------------------------------------------------------------------------- #
# StageConfig
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StageConfig:
    """A single stage in the contract pipeline.

    Fields (spec §4.1):

    - ``id`` / ``model`` / ``prompt`` — identity + how to invoke.
    - ``produces`` — list of :class:`ProduceSpec` (≥ 1 required).
    - ``done_when`` — :class:`DoneWhenSpec` (defaults applied when None).
    - ``consumes`` — :class:`ConsumesSpec` describing upstream binding
      (default = all upstream stages).
    - ``batch`` — N when the stage runs N times in a ``{{num}}`` loop.
    - ``on_failure`` — pause / skip / fail when ``max_attempts`` exhausts.
    - ``enabled`` — false to skip wholesale.
    """

    id: str
    model: str
    prompt: str
    produces: tuple[ProduceSpec, ...]
    done_when: DoneWhenSpec = field(default_factory=DoneWhenSpec)
    consumes: Optional[tuple[str, ...]] = None
    batch: int = 1
    on_failure: str = "pause"
    enabled: bool = True

    def __post_init__(self) -> None:
        for fname in ("id", "model", "prompt"):
            v = getattr(self, fname, None)
            if not isinstance(v, str) or not v.strip():
                raise ConfigError(
                    f"stage field {fname!r} must be a non-empty string"
                )
        if not self.produces:
            raise ConfigError(
                f"stage {self.id!r}: 'produces' must be a non-empty list"
            )
        if not isinstance(self.batch, int) or isinstance(self.batch, bool):
            raise ConfigError(
                f"stage {self.id!r}: 'batch' must be a positive integer"
            )
        if self.batch < 1:
            raise ConfigError(
                f"stage {self.id!r}: 'batch' must be >= 1 (got {self.batch})"
            )
        if self.on_failure not in _ON_FAILURE_VALUES:
            raise ConfigError(
                f"stage {self.id!r}: 'on_failure' must be one of "
                f"{sorted(_ON_FAILURE_VALUES)} (got {self.on_failure!r})"
            )
        # Alias uniqueness within a stage (AC-16).
        seen_alias: set[str] = set()
        for p in self.produces:
            if p.alias in seen_alias:
                raise ConfigError(
                    f"stage {self.id!r}: duplicate produces alias "
                    f"{p.alias!r} (alias must be unique within a stage)"
                )
            seen_alias.add(p.alias)
        # consumes: tuple[str, ...] | None (None = all upstreams) | () (none)
        if self.consumes is not None:
            for cid in self.consumes:
                if not isinstance(cid, str) or not cid.strip():
                    raise ConfigError(
                        f"stage {self.id!r}: 'consumes' entries must be "
                        f"non-empty strings"
                    )

    @property
    def is_batch(self) -> bool:
        return self.batch > 1

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "model": self.model,
            "prompt": self.prompt,
            "produces": [p.to_dict() for p in self.produces],
            "done_when": self.done_when.to_dict(),
        }
        if self.consumes is not None:
            d["consumes"] = list(self.consumes)
        if self.batch != 1:
            d["batch"] = self.batch
        if self.on_failure != "pause":
            d["on_failure"] = self.on_failure
        if not self.enabled:
            d["enabled"] = self.enabled
        return d


# --------------------------------------------------------------------------- #
# ProduceSpec / StageConfig validation
# --------------------------------------------------------------------------- #


def _placeholders_in(path: str) -> set[str]:
    """Return the set of ``{{name}}`` placeholders in ``path``."""

    return {m for m in _PLACEHOLDER_RE.findall(path)}


def _validate_produce_spec(
    produce: ProduceSpec,
    *,
    stage_id: str,
    is_batch: bool,
) -> list[str]:
    """Inter-field validation that can't fit in __post_init__.

    Returns a list of error strings (empty = OK).
    """

    errors: list[str] = []
    has_split = produce.split is not None
    placeholders = _placeholders_in(produce.path)
    ends_json = produce.path.rstrip().endswith(".json")

    # AC-16: batch + split mutually exclusive.
    if is_batch and has_split:
        errors.append(
            f"stage {stage_id!r}: produces alias {produce.alias!r} cannot "
            f"combine `batch` with `split` (v1 — choose one mechanism)"
        )

    if has_split:
        # split mode: path must use {{name}} placeholders, and every
        # placeholder must correspond to a named capture group in the
        # split regex.
        if not placeholders:
            errors.append(
                f"stage {stage_id!r}: produces alias {produce.alias!r} "
                f"has `split` set but path has no {{name}} placeholders"
            )
        try:
            regex = re.compile(produce.split or "")
        except re.error as exc:
            errors.append(
                f"stage {stage_id!r}: produces alias {produce.alias!r} "
                f"`split` regex is invalid: {exc}"
            )
        else:
            named = set(regex.groupindex.keys())
            missing_in_regex = placeholders - named
            missing_in_path = named - placeholders
            if missing_in_regex:
                errors.append(
                    f"stage {stage_id!r}: produces alias {produce.alias!r} "
                    f"path placeholders {sorted(missing_in_regex)} have no "
                    f"matching (?P<name>...) capture in `split`"
                )
            if missing_in_path:
                errors.append(
                    f"stage {stage_id!r}: produces alias {produce.alias!r} "
                    f"`split` capture groups {sorted(missing_in_path)} are "
                    f"not used in path"
                )
        if ends_json:
            errors.append(
                f"stage {stage_id!r}: produces alias {produce.alias!r} "
                f"`split` is incompatible with `.json` suffix"
            )
    elif is_batch:
        # batch mode: path must use {{num}}.
        if "num" not in placeholders:
            errors.append(
                f"stage {stage_id!r}: produces alias {produce.alias!r} "
                f"requires a {{{{num}}}} placeholder when `batch > 1`"
            )
        extra = placeholders - {"num"}
        if extra:
            errors.append(
                f"stage {stage_id!r}: produces alias {produce.alias!r} "
                f"uses placeholders {sorted(extra)} but only {{{{num}}}} "
                f"is valid for batch produces"
            )
    else:
        # single mode: path should not have placeholders.
        if placeholders:
            errors.append(
                f"stage {stage_id!r}: produces alias {produce.alias!r} "
                f"path has placeholders {sorted(placeholders)} but neither "
                f"`split` nor `batch > 1` is set"
            )
    return errors


def validate_stage(stage: StageConfig) -> list[str]:
    """Return a list of validation errors for ``stage`` (empty = OK)."""

    errors: list[str] = []
    for p in stage.produces:
        errors.extend(
            _validate_produce_spec(p, stage_id=stage.id, is_batch=stage.is_batch)
        )
    if not stage.prompt.strip():
        errors.append(
            f"stage {stage.id!r}: 'prompt' is required (no built-in fallback)"
        )
    return errors


def _validate_alias_uniqueness_across_stages(
    stages: Sequence[StageConfig],
) -> list[str]:
    """AC-18: every produces[].alias must be unique within the pipeline."""

    errors: list[str] = []
    seen: dict[str, str] = {}  # alias → first owning stage_id
    for s in stages:
        for p in s.produces:
            if p.alias in seen:
                errors.append(
                    f"produces[].alias {p.alias!r} is declared by both "
                    f"stage {seen[p.alias]!r} and stage {s.id!r}; cross-stage "
                    f"alias overlap is forbidden (AC-18)"
                )
            else:
                seen[p.alias] = s.id
    return errors


# --------------------------------------------------------------------------- #
# Section dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NovelSpec:
    """The ``novel:`` section of the project file."""

    title: str
    genre: str
    target_chapters: int
    words_per_chapter: tuple[int, int]
    style: str
    seeds: tuple[str, ...]
    constraints: tuple[str, ...]

    def chapter_word_range(self) -> tuple[int, int]:
        return self.words_per_chapter


@dataclass(frozen=True)
class PipelineSpec:
    """The ``pipeline:`` section.

    Only the v4 ``stages: tuple[StageConfig, ...]`` form is supported.
    """

    stages: tuple[StageConfig, ...] = ()


@dataclass(frozen=True)
class BatchSize:
    """Per-stage batch sizes."""

    outline: int = 50
    chapter: int = 3

    def __post_init__(self) -> None:  # pragma: no cover - dataclass hook
        if self.outline <= 0 or self.chapter <= 0:
            raise ValueError("batch sizes must be positive")


@dataclass(frozen=True)
class ContextSpec:
    """Context window configuration."""

    total: int = 200_000
    context_reserve: int = 60_000
    output_reserve: int = 12_000
    rolling_window: int = 3
    outline_range: int = 10


@dataclass(frozen=True)
class RetrySpec:
    """Retry / backoff configuration."""

    max_retries: int = 3
    backoff: str = "exponential"  # "exponential" or "linear"
    max_wait: int = 300  # seconds


@dataclass(frozen=True)
class ExecutionSpec:
    """The ``execution:`` section."""

    batch_size: BatchSize
    context: ContextSpec
    retry: RetrySpec
    max_review_iterations: int
    review_model: str
    write_model: str


@dataclass(frozen=True)
class NovelProjectConfig:
    """Top-level engine configuration."""

    project_path: Path
    novel: NovelSpec
    pipeline: PipelineSpec
    execution: ExecutionSpec
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def chapter_range(self) -> tuple[int, int]:
        return self.novel.chapter_word_range()


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #


def _as_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"field {field!r} must be a non-empty string")
    return value


def _as_int(value: Any, field: str, *, min_value: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"field {field!r} must be an integer")
    if min_value is not None and value < min_value:
        raise ConfigError(f"field {field!r} must be >= {min_value}")
    return value


def _as_list(value: Any, field: str, *, element_type: type = str) -> list:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"field {field!r} must be a non-empty list")
    out: list = []
    for i, elem in enumerate(value):
        if not isinstance(elem, element_type):
            raise ConfigError(
                f"field {field!r}[{i}] must be a {element_type.__name__}, got {type(elem).__name__}"
            )
        out.append(elem)
    return out


def _parse_words_per_chapter(value: Any) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(
            "field novel.words_per_chapter must be a [min, max] pair of integers"
        )
    lo, hi = value
    if (
        isinstance(lo, bool)
        or isinstance(hi, bool)
        or not isinstance(lo, int)
        or not isinstance(hi, int)
    ):
        raise ConfigError(
            "field novel.words_per_chapter must be a [min, max] pair of integers"
        )
    if lo <= 0 or hi <= 0:
        raise ConfigError("field novel.words_per_chapter entries must be > 0")
    if lo > hi:
        raise ConfigError(
            f"field novel.words_per_chapter: min ({lo}) must be <= max ({hi})"
        )
    return (lo, hi)


def _parse_novel(raw: Mapping[str, Any]) -> NovelSpec:
    if "novel" not in raw or not isinstance(raw["novel"], Mapping):
        raise ConfigError("missing required section: novel")
    section = raw["novel"]
    title = _as_str(section.get("title", ""), "novel.title")
    genre = _as_str(section.get("genre", ""), "novel.genre")
    target = _as_int(section.get("target_chapters", 0), "novel.target_chapters", min_value=1)
    if "words_per_chapter" not in section:
        raise ConfigError("missing required field: novel.words_per_chapter")
    wpc = _parse_words_per_chapter(section["words_per_chapter"])
    style = _as_str(section.get("style", ""), "novel.style")
    seeds = tuple(_as_list(section.get("seeds", []), "novel.seeds", element_type=str))
    constraints = tuple(
        _as_list(section.get("constraints", []), "novel.constraints", element_type=str)
    )
    return NovelSpec(
        title=title,
        genre=genre,
        target_chapters=target,
        words_per_chapter=wpc,
        style=style,
        seeds=seeds,
        constraints=constraints,
    )


# --------------------------------------------------------------------------- #
# Parsing helpers for the new contract fields
# --------------------------------------------------------------------------- #


def _parse_check(raw: Mapping[str, Any], *, position: int) -> CheckSpec:
    if not isinstance(raw, Mapping):
        raise SchemaInvalid(
            f"done_when.checks[{position}] must be a mapping"
        )
    kind = str(raw.get("kind", "")).strip()
    target = str(raw.get("target", "")).strip()
    if not kind or not target:
        raise SchemaInvalid(
            f"done_when.checks[{position}]: 'kind' and 'target' are required"
        )
    return CheckSpec(
        kind=kind,
        target=target,
        value=raw.get("value"),
        field=raw.get("field"),
        pattern=raw.get("pattern"),
        callable=raw.get("callable"),
    )


def _parse_done_when(raw: Any) -> DoneWhenSpec:
    if raw is None:
        return DoneWhenSpec()
    if not isinstance(raw, Mapping):
        raise SchemaInvalid("done_when must be a mapping or null")
    completion_signal = raw.get("completion_signal", None)
    # An explicit null is allowed (AC-11); a missing key defaults to the
    # standard marker.  Distinguish by checking key presence.
    if "completion_signal" in raw:
        cs_value = raw["completion_signal"]
        if cs_value is None:
            completion_signal = None
        elif isinstance(cs_value, str):
            completion_signal = cs_value
        else:
            raise SchemaInvalid(
                "done_when.completion_signal must be a string or null"
            )
    else:
        completion_signal = DoneWhenSpec().completion_signal

    raw_checks = raw.get("checks") or []
    if not isinstance(raw_checks, list):
        raise SchemaInvalid("done_when.checks must be a list")
    checks = tuple(_parse_check(c, position=i) for i, c in enumerate(raw_checks))

    max_attempts = raw.get("max_attempts", DoneWhenSpec().max_attempts)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise SchemaInvalid(
            "done_when.max_attempts must be a positive integer"
        )
    mode = str(raw.get("mode", "all"))
    if mode not in {"all", "any"}:
        raise SchemaInvalid(
            f"done_when.mode must be 'all' or 'any'; got {mode!r}"
        )
    return DoneWhenSpec(
        completion_signal=completion_signal,
        checks=checks,
        max_attempts=max_attempts,
        mode=mode,
    )


def _parse_produce(raw: Any, *, position: int) -> ProduceSpec:
    if not isinstance(raw, Mapping):
        raise SchemaInvalid(
            f"produces[{position}] must be a mapping"
        )
    path = str(raw.get("path", "")).strip()
    alias = str(raw.get("alias", "")).strip()
    if not path or not alias:
        raise SchemaInvalid(
            f"produces[{position}]: 'path' and 'alias' are required"
        )
    split = raw.get("split")
    if split is not None:
        if not isinstance(split, str):
            raise SchemaInvalid(
                f"produces[{position}].split must be a string regex"
            )
        split = split.strip() or None
    form = "json" if path.rstrip().endswith(".json") else "text"
    return ProduceSpec(path=path, alias=alias, split=split, form=form)


def _parse_stage_config(
    raw: Mapping[str, Any],
    *,
    position: int,
) -> StageConfig:
    """Parse one ``pipeline.stages[i]`` record (contract model)."""

    if not isinstance(raw, Mapping):
        raise SchemaInvalid(
            f"pipeline.stages[{position}] must be a mapping, got {type(raw).__name__}"
        )
    try:
        stage_id = str(raw["id"]).strip()
    except KeyError as exc:
        raise SchemaInvalid(
            f"pipeline.stages[{position}] is missing required field 'id'"
        ) from exc
    if not stage_id:
        raise SchemaInvalid(
            f"pipeline.stages[{position}].id must be a non-empty string"
        )
    model = str(raw.get("model", "")).strip()
    if not model:
        raise SchemaInvalid(
            f"stage {stage_id!r} (pipeline.stages[{position}]): "
            f"'model' is required"
        )
    prompt = str(raw.get("prompt", "")).strip()
    if not prompt:
        raise SchemaInvalid(
            f"stage {stage_id!r} (pipeline.stages[{position}]): "
            f"'prompt' is required (no built-in fallback)"
        )

    raw_produces = raw.get("produces")
    if raw_produces is None or not isinstance(raw_produces, list) or not raw_produces:
        raise SchemaInvalid(
            f"stage {stage_id!r}: 'produces' must be a non-empty list"
        )
    produces = tuple(
        _parse_produce(p, position=i) for i, p in enumerate(raw_produces)
    )

    done_when = _parse_done_when(raw.get("done_when"))

    consumes_raw = raw.get("consumes", "missing")
    consumes: Optional[tuple[str, ...]]
    if consumes_raw == "missing" or consumes_raw is None:
        consumes = None  # default: all upstreams
    elif isinstance(consumes_raw, list):
        # Explicit list, including empty list ("no upstreams").
        cleaned: list[str] = []
        for c in consumes_raw:
            if not isinstance(c, str) or not c.strip():
                raise SchemaInvalid(
                    f"stage {stage_id!r}: 'consumes' entries must be non-empty strings"
                )
            cleaned.append(c.strip())
        consumes = tuple(cleaned)
    else:
        raise SchemaInvalid(
            f"stage {stage_id!r}: 'consumes' must be a list or null"
        )

    batch = raw.get("batch", 1)
    if not isinstance(batch, int) or isinstance(batch, bool) or batch < 1:
        raise SchemaInvalid(
            f"stage {stage_id!r}: 'batch' must be a positive integer"
        )
    on_failure = str(raw.get("on_failure", "pause"))
    if on_failure not in _ON_FAILURE_VALUES:
        raise SchemaInvalid(
            f"stage {stage_id!r}: 'on_failure' must be one of "
            f"{sorted(_ON_FAILURE_VALUES)} (got {on_failure!r})"
        )
    enabled = bool(raw.get("enabled", True))
    return StageConfig(
        id=stage_id,
        model=model,
        prompt=prompt,
        produces=produces,
        done_when=done_when,
        consumes=consumes,
        batch=batch,
        on_failure=on_failure,
        enabled=enabled,
    )


# --------------------------------------------------------------------------- #
# Pipeline section
# --------------------------------------------------------------------------- #


# v3 fields that must be rejected on sight (AC-15 / spec §AC-15).
_V3_DEPRECATED_FIELDS = ("template", "stages_override", "scaffold_from")


def _parse_pipeline(raw: Mapping[str, Any]) -> PipelineSpec:
    """Parse the ``pipeline:`` section (v4-only)."""

    section = raw.get("pipeline") or {}
    if not isinstance(section, Mapping):
        raise ConfigError("section pipeline must be a mapping")

    # AC-15: reject any v3 field on sight.
    for v3_field in _V3_DEPRECATED_FIELDS:
        if v3_field in section:
            raise ConfigError(
                f"field pipeline.{v3_field} is no longer supported; "
                f"use pipeline.stages: [...] (see docs/plan/stage-contract.md)"
            )

    explicit_stages = section.get("stages")
    if explicit_stages is None:
        raise ConfigError(
            "field pipeline.stages is required (v3 template / stages_override "
            "are no longer supported)"
        )
    if not isinstance(explicit_stages, list) or not explicit_stages:
        raise SchemaInvalid(
            "field pipeline.stages must be a non-empty list of stage mappings"
        )
    stages_tuple = tuple(
        _parse_stage_config(s, position=i)
        for i, s in enumerate(explicit_stages)
    )

    # Cross-record validation.
    seen: set[str] = set()
    for s in stages_tuple:
        if s.id in seen:
            raise SchemaInvalid(
                f"pipeline.stages contains duplicate id {s.id!r}"
            )
        seen.add(s.id)
        for err in validate_stage(s):
            raise SchemaInvalid(err)
    # Cross-stage alias uniqueness (AC-18).
    for err in _validate_alias_uniqueness_across_stages(stages_tuple):
        raise SchemaInvalid(err)

    return PipelineSpec(stages=stages_tuple)


def _parse_execution(raw: Mapping[str, Any]) -> ExecutionSpec:
    section = raw.get("execution") or {}
    if not isinstance(section, Mapping):
        raise ConfigError("section execution must be a mapping")

    batch = section.get("batch_size") or {}
    if not isinstance(batch, Mapping):
        raise ConfigError("execution.batch_size must be a mapping")
    batch_size = BatchSize(
        outline=_as_int(batch.get("outline", 50), "execution.batch_size.outline", min_value=1),
        chapter=_as_int(batch.get("chapter", 3), "execution.batch_size.chapter", min_value=1),
    )

    ctx_raw = section.get("context") or {}
    if not isinstance(ctx_raw, Mapping):
        raise ConfigError("execution.context must be a mapping")
    context = ContextSpec(
        total=_as_int(ctx_raw.get("total", 200_000), "execution.context.total", min_value=1000),
        context_reserve=_as_int(
            ctx_raw.get("context_reserve", 60_000),
            "execution.context.context_reserve",
            min_value=1,
        ),
        output_reserve=_as_int(
            ctx_raw.get("output_reserve", 12_000),
            "execution.context.output_reserve",
            min_value=1,
        ),
        rolling_window=_as_int(
            ctx_raw.get("rolling_window", 3),
            "execution.context.rolling_window",
            min_value=0,
        ),
        outline_range=_as_int(
            ctx_raw.get("outline_range", 10),
            "execution.context.outline_range",
            min_value=0,
        ),
    )
    if context.context_reserve + context.output_reserve >= context.total:
        raise ConfigError(
            "execution.context: context_reserve + output_reserve must be less than total"
        )

    retry_raw = section.get("retry") or {}
    if not isinstance(retry_raw, Mapping):
        raise ConfigError("execution.retry must be a mapping")
    retry = RetrySpec(
        max_retries=_as_int(
            retry_raw.get("max_retries", 3),
            "execution.retry.max_retries",
            min_value=0,
        ),
        backoff=_as_str(retry_raw.get("backoff", "exponential"), "execution.retry.backoff"),
        max_wait=_as_int(
            retry_raw.get("max_wait", 300),
            "execution.retry.max_wait",
            min_value=1,
        ),
    )
    if retry.backoff not in {"exponential", "linear", "constant"}:
        raise ConfigError(
            f"execution.retry.backoff must be one of exponential/linear/constant, got {retry.backoff!r}"
        )

    max_review = _as_int(
        section.get("max_review_iterations", 3),
        "execution.max_review_iterations",
        min_value=1,
    )
    review_model = _as_str(
        section.get("review_model", "claude-sonnet-4-6"),
        "execution.review_model",
    )
    write_model = _as_str(
        section.get("write_model", "claude-opus-4-7"),
        "execution.write_model",
    )
    return ExecutionSpec(
        batch_size=batch_size,
        context=context,
        retry=retry,
        max_review_iterations=max_review,
        review_model=review_model,
        write_model=write_model,
    )


def _resolve_project_root(path: Path) -> Path:
    """Project root = directory containing novel-project.yaml."""

    return path.resolve().parent


def _check_seed_files(novel: NovelSpec, project_root: Path) -> None:
    """Log a warning for any missing seed / constraint file."""

    for rel in novel.seeds:
        candidate = (project_root / rel).resolve()
        if not candidate.exists():
            _log.warning(
                "seed file not found: %s (will surface at first "
                "{{include:}} stage via on_failure)",
                rel,
            )
    for rel in novel.constraints:
        candidate = (project_root / rel).resolve()
        if not candidate.exists():
            _log.warning(
                "constraint file not found: %s (will surface at first "
                "{{include:}} stage via on_failure)",
                rel,
            )


def load_config(path: Path) -> NovelProjectConfig:
    """Load and validate a novel-project.yaml file.

    Raises:
        ConfigError: when the file is missing, unparseable, contains a
            v3-only field, or fails any validation rule.  Also
            :class:`SchemaInvalid` (a subclass) for stage-record errors.
    """

    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw_obj = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {exc}") from exc
    if not isinstance(raw_obj, Mapping):
        raise ConfigError("config root must be a mapping")

    novel = _parse_novel(raw_obj)
    pipeline = _parse_pipeline(raw_obj)
    execution = _parse_execution(raw_obj)
    project_root = _resolve_project_root(path)
    _check_seed_files(novel, project_root)

    return NovelProjectConfig(
        project_path=path,
        novel=novel,
        pipeline=pipeline,
        execution=execution,
        raw=dict(raw_obj),
    )


def with_max_chapters(cfg: NovelProjectConfig, max_chapters: int) -> NovelProjectConfig:
    """Return a copy of ``cfg`` with ``novel.target_chapters`` overridden."""

    if max_chapters is None:
        return cfg
    if max_chapters <= 0:
        raise ConfigError("max_chapters must be > 0")
    new_novel = replace(cfg.novel, target_chapters=max_chapters)
    return replace(cfg, novel=new_novel)


def stages_for(cfg: NovelProjectConfig) -> Sequence[StageConfig]:
    """Return the resolved stage list for a config."""

    return list(cfg.pipeline.stages)


def stage_ids_for(cfg: NovelProjectConfig) -> Sequence[str]:
    """Convenience: just the stage ids in order."""

    return [s.id for s in cfg.pipeline.stages]


__all__ = [
    "CheckSpec",
    "DoneWhenSpec",
    "ProduceSpec",
    "StageConfig",
    "validate_stage",
    "NovelSpec",
    "PipelineSpec",
    "BatchSize",
    "ContextSpec",
    "RetrySpec",
    "ExecutionSpec",
    "NovelProjectConfig",
    "load_config",
    "with_max_chapters",
    "stages_for",
    "stage_ids_for",
]
