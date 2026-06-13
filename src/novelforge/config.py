"""Configuration layer.

Loads and validates ``novel-project.yaml`` files.  See ``plan.md`` §4.1
for the contract.  The exported ``NovelProjectConfig`` is a frozen
dataclass that downstream modules (state, orchestrator, context, …) can
type-check against.

v4 schema (spec §5.1) introduces :class:`StageConfig` (8 fields,
frozen) as the canonical per-stage record.  The :class:`PipelineSpec`
exposes ``stages: tuple[StageConfig, ...]`` for the v4 path while
keeping ``template`` and ``stages_override`` for backward
compatibility — both are accepted with a DeprecationWarning, per spec
§5.5.1.  The v4 path is preferred when ``stages`` is present.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
import re

import yaml

from .errors import ConfigError, SchemaInvalid
from .claude.output_parser import infer_form
from .utils.log import get_logger as _get_logger

_log = _get_logger("config")

# --------------------------------------------------------------------------- #
# Built-in pipeline templates.
# --------------------------------------------------------------------------- #

# Re-exported from ``templates`` so legacy import sites that pulled
# ``PIPELINE_TEMPLATES`` from this module keep working.  The data
# itself lives in :mod:`novelforge.templates` (T01, T02) and is the
# v4 source of truth.
from .templates import (
    PIPELINE_TEMPLATES as _PIPELINE_TEMPLATES,
    VALID_TEMPLATES,
    get_template as _get_template,
)

PIPELINE_TEMPLATES: dict[str, tuple[str, ...]] = {
    name: tuple(stages) for name, stages in _PIPELINE_TEMPLATES.items()
}


# --------------------------------------------------------------------------- #
# v4 stage schema (8 fields)
# --------------------------------------------------------------------------- #


_ON_FAILURE_VALUES = frozenset({"pause", "skip", "fail"})


@dataclass(frozen=True)
class StageConfig:
    """A single stage in a v4 pipeline (8 fields, frozen).

    Field order mirrors spec §5.1 so the dataclass render is also the
    canonical yaml render order.  Defaults are applied for the three
    truly optional fields (``batch``, ``on_failure``, ``enabled``).
    """

    id: str
    model: str
    prompt: str
    output: str
    split: Optional[str] = None
    batch: int = 1
    on_failure: str = "pause"
    enabled: bool = True

    def __post_init__(self) -> None:  # pragma: no cover - dataclass hook
        # frozen dataclass: tolerate AttributeError on uninitialised
        # fields by checking first.
        for fname in ("id", "model", "prompt", "output"):
            v = getattr(self, fname, None)
            if not isinstance(v, str) or not v.strip():
                raise ConfigError(
                    f"stage field {fname!r} must be a non-empty string"
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

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "model": self.model,
            "prompt": self.prompt,
            "output": self.output,
        }
        if self.split:
            d["split"] = self.split
        if self.batch != 1:
            d["batch"] = self.batch
        if self.on_failure != "pause":
            d["on_failure"] = self.on_failure
        if not self.enabled:
            d["enabled"] = self.enabled
        return d


def validate_stage(stage: StageConfig) -> list[str]:
    """Return a list of validation errors for ``stage`` (empty = OK).

    The :class:`StageConfig` constructor already enforces the type and
    presence of every required field.  This function covers the
    inter-field constraints that can't be expressed by ``__post_init__``
    without re-validating the output template form.
    """

    errors: list[str] = []
    # Form inference (A15: .json + {{x}} illegal).
    try:
        form = infer_form(stage.output)
    except ValueError as exc:
        errors.append(
            f"stage {stage.id!r}: output template is invalid: {exc}"
        )
        return errors
    if form == "split" and not stage.split:
        errors.append(
            f"stage {stage.id!r}: output contains placeholders but `split` "
            f"is missing — set `split` to a regex with a named `num` "
            f"capture group (A10)"
        )
    if stage.split:
        # Every {{var}} in the template must have a matching (?P<var>...)
        # capture group in the split regex, and vice versa.
        try:
            regex = re.compile(stage.split)
        except re.error as exc:
            errors.append(
                f"stage {stage.id!r}: 'split' regex is invalid: {exc}"
            )
        else:
            named = set(regex.groupindex.keys())
            placeholders = set(
                re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)", stage.output)
            )
            missing_in_regex = placeholders - named
            if missing_in_regex:
                errors.append(
                    f"stage {stage.id!r}: output placeholders {sorted(missing_in_regex)} "
                    f"have no matching (?P<name>...) capture in `split`"
                )
    if not stage.prompt.strip():
        errors.append(
            f"stage {stage.id!r}: 'prompt' is required (no built-in fallback)"
        )
    return errors


# --------------------------------------------------------------------------- #
# Dataclasses
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

    v4 promotes ``stages: tuple[StageConfig, ...]`` to the canonical
    runtime data source.  The v3 fields (``template``,
    ``stages_override``) are still accepted for backward compatibility
    but are normalised to ``stages`` during :func:`load_config`, and
    the orchestrator will emit a DeprecationWarning for each field
    present.  ``scaffold_from`` is a pure metadata field — the
    runtime never reads it (A16).
    """

    template: Optional[str] = None
    stages: tuple[StageConfig, ...] = ()
    stages_override: Optional[tuple[str, ...]] = None
    scaffold_from: Optional[str] = None


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
    # v4-only:
    route_history_max: int = 50


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


def _parse_stage_config(
    raw: Mapping[str, Any],
    *,
    position: int,
) -> StageConfig:
    """Parse one ``pipeline.stages[i]`` record (spec §5.1)."""

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
    output = str(raw.get("output", "")).strip()
    if not output:
        raise SchemaInvalid(
            f"stage {stage_id!r} (pipeline.stages[{position}]): "
            f"'output' is required"
        )
    split = raw.get("split")
    if split is not None and not isinstance(split, str):
        raise SchemaInvalid(
            f"stage {stage_id!r}: 'split' must be a string regex"
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
        output=output,
        split=split,
        batch=batch,
        on_failure=on_failure,
        enabled=enabled,
    )


def _parse_pipeline(raw: Mapping[str, Any]) -> PipelineSpec:
    """Parse the ``pipeline:`` section.

    Returns a fully-resolved :class:`PipelineSpec` where ``stages`` is
    always a tuple of :class:`StageConfig`.  The v3 fields
    (``template`` / ``stages_override``) are honoured for back-compat
    and translated into a synthetic ``stages`` list (one per stage id
    in the resolved template + override), defaulting each
    :class:`StageConfig` field from the built-in
    :mod:`novelforge.templates` record.
    """

    section = raw.get("pipeline") or {}
    if not isinstance(section, Mapping):
        raise ConfigError("section pipeline must be a mapping")

    # scaffold_from: pure metadata, never validated
    scaffold_from_raw = section.get("scaffold_from")
    scaffold_from: Optional[str] = None
    if scaffold_from_raw is not None:
        if not isinstance(scaffold_from_raw, str):
            raise ConfigError(
                "field pipeline.scaffold_from must be a string (metadata only; "
                "runtime ignores any value, including unknown template names)"
            )
        scaffold_from = scaffold_from_raw

    explicit_stages = section.get("stages")
    has_explicit = explicit_stages is not None
    if has_explicit:
        if not isinstance(explicit_stages, list) or not explicit_stages:
            raise SchemaInvalid(
                "field pipeline.stages must be a non-empty list of stage mappings"
            )
        stages_tuple = tuple(
            _parse_stage_config(s, position=i)
            for i, s in enumerate(explicit_stages)
        )
    else:
        stages_tuple = ()

    # v3 fallback: template + stages_override
    template: Optional[str] = None
    stages_override: Optional[tuple[str, ...]] = None

    if not has_explicit:
        template_raw = section.get("template", "long-epic")
        if not isinstance(template_raw, str):
            raise ConfigError("field pipeline.template must be a string")
        if template_raw not in VALID_TEMPLATES:
            raise ConfigError(
                f"unknown pipeline.template {template_raw!r}; "
                f"expected one of: {sorted(VALID_TEMPLATES)}"
            )
        template = template_raw
        override_raw = section.get("stages_override")
        if override_raw is None:
            stage_ids: tuple[str, ...] = PIPELINE_TEMPLATES[template]
        else:
            if not isinstance(override_raw, (list, tuple)) or not override_raw:
                raise ConfigError(
                    "field pipeline.stages_override must be a non-empty list of stage ids"
                )
            stage_ids = tuple(str(s) for s in override_raw)
            if len(set(stage_ids)) != len(stage_ids):
                raise ConfigError("pipeline.stages_override contains duplicates")
            stages_override = stage_ids
        stages_tuple = tuple(_synthesize_stage_configs(stage_ids))

    # Cross-record validation
    seen: set[str] = set()
    for s in stages_tuple:
        if s.id in seen:
            raise SchemaInvalid(
                f"pipeline.stages contains duplicate id {s.id!r}"
            )
        seen.add(s.id)
        for err in validate_stage(s):
            raise SchemaInvalid(err)

    return PipelineSpec(
        template=template,
        stages=stages_tuple,
        stages_override=stages_override,
        scaffold_from=scaffold_from,
    )


def _synthesize_stage_configs(stage_ids: Sequence[str]) -> Iterable[StageConfig]:
    """Build a synthetic list of :class:`StageConfig` from built-in templates.

    Used when a user supplies only ``template:``/``stages_override:``
    in their yaml.  Each stage pulls its 8 fields from
    :func:`novelforge.templates.get_template` so the engine can run
    with a single :class:`GenericStage` code path (T14).
    """

    for sid in stage_ids:
        try:
            tpl = _get_template(sid)
        except KeyError as exc:
            raise ConfigError(
                f"pipeline references unknown stage id {sid!r}; "
                f"known: {sorted(_ALL_STAGE_IDS)}"
            ) from exc
        yield StageConfig(
            id=tpl.id,
            model=tpl.model,
            prompt=tpl.prompt_text,
            output=tpl.output,
            split=tpl.split,
            batch=tpl.batch,
            on_failure=tpl.on_failure,
            enabled=tpl.enabled,
        )


from .templates import ALL_STAGE_IDS as _ALL_STAGE_IDS


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
    route_history_max = _as_int(
        section.get("route_history_max", 50),
        "execution.route_history_max",
        min_value=1,
    )
    return ExecutionSpec(
        batch_size=batch_size,
        context=context,
        retry=retry,
        max_review_iterations=max_review,
        review_model=review_model,
        write_model=write_model,
        route_history_max=route_history_max,
    )


def _resolve_project_root(path: Path) -> Path:
    """Project root = directory containing novel-project.yaml."""

    return path.resolve().parent


def _check_seed_files(novel: NovelSpec, project_root: Path) -> None:
    """Log a warning for any missing seed / constraint file.

    Per spec §4.1 and A13: ``init`` only materialises the yaml +
    ``prompts/``; ``outline/`` and ``CLAUDE.md`` are user-supplied.
    Validate must therefore pass without them — missing files surface
    at runtime on the first ``{{include:}}`` stage and are handled
    via that stage's ``on_failure`` disposition (default ``pause``).
    """

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
        ConfigError: when the file is missing, unparseable, or fails
            any validation rule.  Also :class:`SchemaInvalid` (a
            subclass) for v4 stage-record errors.
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

    # Deprecation messages for v3 fields are emitted at *runtime* (see
    # ``deprecation_warnings_for``) so they reach the run log without
    # breaking test suites that treat DeprecationWarning as an error.

    return NovelProjectConfig(
        project_path=path,
        novel=novel,
        pipeline=pipeline,
        execution=execution,
        raw=dict(raw_obj),
    )


def _explicit_stages_present(raw: Mapping[str, Any]) -> bool:
    section = raw.get("pipeline")
    if not isinstance(section, Mapping):
        return False
    return section.get("stages") is not None


def deprecation_warnings_for(cfg: NovelProjectConfig) -> list[str]:
    """Return the list of deprecation messages implied by ``cfg``.

    Used by the orchestrator / CLI at *runtime* (not at load time) so
    the warnings reach the run log without breaking test suites that
    treat :class:`DeprecationWarning` as an error.  One message per
    deprecated v3 field present in the source yaml (A1 / spec §10).
    """

    messages: list[str] = []
    section = cfg.raw.get("pipeline") if cfg.raw else None
    if isinstance(section, Mapping):
        if (
            "template" in section
            and section.get("stages") is None
        ):
            messages.append(
                "pipeline.template is deprecated; use pipeline.stages (v4) "
                "instead.  See spec §5.5.1."
            )
        if section.get("stages_override") is not None:
            messages.append(
                "pipeline.stages_override is deprecated and ignored at "
                "runtime; merge its values into pipeline.stages instead.  "
                "See spec §5.5.1."
            )
    return messages


def with_max_chapters(cfg: NovelProjectConfig, max_chapters: int) -> NovelProjectConfig:
    """Return a copy of ``cfg`` with ``novel.target_chapters`` overridden."""

    if max_chapters is None:
        return cfg
    if max_chapters <= 0:
        raise ConfigError("max_chapters must be > 0")
    new_novel = replace(cfg.novel, target_chapters=max_chapters)
    return replace(cfg, novel=new_novel)


def stages_for(cfg: NovelProjectConfig) -> Sequence[StageConfig]:
    """Return the resolved stage list for a config.

    Always reflects the runtime stage list.  v4 ``pipeline.stages`` is
    used when present; otherwise ``pipeline.stages_override`` falls
    back to ``pipeline.template`` (with DeprecationWarning emitted at
    load time).
    """

    return list(cfg.pipeline.stages)


def stage_ids_for(cfg: NovelProjectConfig) -> Sequence[str]:
    """Convenience: just the stage ids in order."""

    return [s.id for s in cfg.pipeline.stages]


__all__ = [
    "PIPELINE_TEMPLATES",
    "VALID_TEMPLATES",
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
