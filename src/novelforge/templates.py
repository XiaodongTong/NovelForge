"""Built-in pipeline templates (v4 contract model).

The runtime consumes :class:`novelforge.config.StageConfig` records;
this module is the **default data source** for ``novelforge init``.
Each built-in template is a named bundle of stages described entirely
in the v4 contract form (``produces`` / ``done_when`` / ``consumes``).

Two templates ship out of the box:

- ``long-epic``  — outline → characters → batch chapters → review.
- ``short-story`` — a leaner variant with a single non-batch write step.

The legacy ``PIPELINE_TEMPLATES`` mapping (v3 stage-id tuples) and the
old :class:`StageTemplate` record (8 imperative fields) have been
removed; ``init`` now materialises the contract record straight into
the user's ``novel-project.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import (
    CheckSpec,
    DoneWhenSpec,
    ProduceSpec,
    StageConfig,
    validate_stage,
)
from .errors import ConfigError

__all__ = [
    "ContractTemplate",
    "BUILTIN_TEMPLATES",
    "VALID_TEMPLATES",
    "get_template",
]


# --------------------------------------------------------------------------- #
# ContractTemplate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ContractTemplate:
    """A named bundle of v4 :class:`StageConfig` records.

    ``init`` consumes this directly: each stage's ``prompt_text`` is
    materialised to ``prompts/<prompt_file>`` and the stage record is
    dumped to ``novel-project.yaml``.
    """

    name: str
    description: str
    stages: tuple[StageConfig, ...]
    # Per-stage prompt text keyed by stage id.  ``init`` writes these
    # to ``prompts/<prompt_file>`` and the stage's ``prompt`` field is
    # set to that relative path.
    prompts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.stages:
            raise ConfigError(
                f"template {self.name!r}: at least one stage is required"
            )
        # Cross-stage validation — surface config errors eagerly so a
        # misconfigured built-in template fails at import time.
        for s in self.stages:
            errs = validate_stage(s)
            if errs:
                raise ConfigError(
                    f"template {self.name!r}: stage {s.id!r} failed "
                    f"validation: {errs}"
                )

    def stage_ids(self) -> list[str]:
        return [s.id for s in self.stages]

    def to_payload(self) -> list[dict[str, Any]]:
        """Render the template as a list of stage mappings for yaml."""

        out: list[dict[str, Any]] = []
        for stage in self.stages:
            entry: dict[str, Any] = {
                "id": stage.id,
                "model": stage.model,
                # Always reference the prompts/<file>.md so the user
                # can edit the prompt without touching yaml.
                "prompt": _prompt_file_for(stage.id),
                "produces": [p.to_dict() for p in stage.produces],
                "done_when": stage.done_when.to_dict(),
            }
            if stage.consumes is not None:
                entry["consumes"] = list(stage.consumes)
            if stage.batch != 1:
                entry["batch"] = stage.batch
            if stage.on_failure != "pause":
                entry["on_failure"] = stage.on_failure
            if not stage.enabled:
                entry["enabled"] = False
            out.append(entry)
        return out


def _prompt_file_for(stage_id: str) -> str:
    return stage_id.replace("_", "-") + ".md"


# --------------------------------------------------------------------------- #
# Built-in stages (reusable building blocks)
# --------------------------------------------------------------------------- #


_DEFAULT_REVIEW_MODEL = "claude-sonnet-4-6"
_DEFAULT_WRITE_MODEL = "claude-opus-4-7"


def _outline_stage() -> StageConfig:
    return StageConfig(
        id="generate_outline",
        model=_DEFAULT_WRITE_MODEL,
        prompt="prompts/generate-outline.md",
        produces=(
            ProduceSpec(path="output/summaries/plot.md", alias="outline"),
        ),
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="min_chars",
                    target="output/summaries/plot.md",
                    value=500,
                ),
            ),
        ),
    )


def _characters_stage() -> StageConfig:
    return StageConfig(
        id="design_characters",
        model=_DEFAULT_WRITE_MODEL,
        prompt="prompts/design-characters.md",
        consumes=("generate_outline",),
        produces=(
            ProduceSpec(path="output/meta/characters.md", alias="characters"),
        ),
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="min_chars",
                    target="output/meta/characters.md",
                    value=300,
                ),
            ),
        ),
    )


def _write_chapter_batch_stage() -> StageConfig:
    return StageConfig(
        id="write_chapter",
        model=_DEFAULT_WRITE_MODEL,
        prompt="prompts/write-chapter.md",
        consumes=("generate_outline", "design_characters"),
        produces=(
            ProduceSpec(
                path="output/chapters/{{num:03d}}.md", alias="chapter"
            ),
        ),
        batch=3,
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="min_chars",
                    target="output/chapters/{{num:03d}}.md",
                    value=1000,
                ),
            ),
        ),
    )


def _write_chapter_single_stage() -> StageConfig:
    return StageConfig(
        id="write_chapter",
        model=_DEFAULT_WRITE_MODEL,
        prompt="prompts/write-chapter.md",
        consumes=("generate_outline",),
        produces=(
            ProduceSpec(
                path="output/chapters/001.md", alias="chapter"
            ),
        ),
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="min_chars",
                    target="output/chapters/001.md",
                    value=500,
                ),
            ),
        ),
    )


def _review_chapter_stage() -> StageConfig:
    return StageConfig(
        id="review_chapter",
        model=_DEFAULT_REVIEW_MODEL,
        prompt="prompts/review-chapter.md",
        consumes=("write_chapter",),
        produces=(
            ProduceSpec(
                path="output/review/chapter-review.json",
                alias="chapter_review",
            ),
        ),
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="json_field",
                    target="output/review/chapter-review.json",
                    field="passed",
                ),
            ),
        ),
    )


def _final_polish_stage() -> StageConfig:
    return StageConfig(
        id="final_polish",
        model=_DEFAULT_WRITE_MODEL,
        prompt="prompts/final-polish.md",
        consumes=("write_chapter",),
        produces=(
            ProduceSpec(
                path="output/review/final-polish-notes.md",
                alias="polish_notes",
            ),
        ),
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="min_chars",
                    target="output/review/final-polish-notes.md",
                    value=200,
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Built-in prompts (one per stage)
# --------------------------------------------------------------------------- #


_PROMPT_OUTLINE = (
    "You are a senior webnovel planner.  Using the seeds and constraints "
    "provided, produce a chapter-by-chapter outline.  Use ``## Chapter N - "
    "<title>`` headings with 1-2 sentences per chapter describing the key "
    "beat.  Output markdown only."
)

_PROMPT_CHARACTERS = (
    "Read the upstream outline at {{upstream.generate_outline.outline}} and "
    "design a character dossier for every named character.  Use:\n\n"
    "# <Character Name>\n**Role**: ...\n**Voice**: ...\n"
    "**Relationships**: ...\n**Arc**: ...\n\n"
    "One dossier per character; output markdown only."
)

_PROMPT_WRITE_CHAPTER = (
    "Write the next chapter of the novel.  Use the outline beat above as "
    "your target.  Output 800-1500 Chinese characters of prose and end on "
    "a small reversal."
)

_PROMPT_REVIEW_CHAPTER = (
    "Review the chapter at {{upstream.write_chapter.chapter[*]}}.  Return a "
    "JSON object with: passed (boolean), findings (list), "
    "required_changes (list), summary (string).  Set passed=true when the "
    "chapter is acceptable."
)

_PROMPT_FINAL_POLISH = (
    "Read the manuscript chapters at "
    "{{upstream.write_chapter.chapter[*]}} and produce a final-polish "
    "brief.  For every chapter, list at most three tweaks (word choice, "
    "rhythm, clarity).  Output markdown."
)


# --------------------------------------------------------------------------- #
# Built-in templates
# --------------------------------------------------------------------------- #


_LONG_EPIC = ContractTemplate(
    name="long-epic",
    description=(
        "Outline → characters → batch chapters → review → final polish.  "
        "Suitable for a multi-chapter webnovel."
    ),
    stages=(
        _outline_stage(),
        _characters_stage(),
        _write_chapter_batch_stage(),
        _review_chapter_stage(),
        _final_polish_stage(),
    ),
    prompts={
        "generate_outline": _PROMPT_OUTLINE,
        "design_characters": _PROMPT_CHARACTERS,
        "write_chapter": _PROMPT_WRITE_CHAPTER,
        "review_chapter": _PROMPT_REVIEW_CHAPTER,
        "final_polish": _PROMPT_FINAL_POLISH,
    },
)


_SHORT_STORY = ContractTemplate(
    name="short-story",
    description=(
        "Outline → single chapter.  A leaner pipeline for short pieces."
    ),
    stages=(
        _outline_stage(),
        _write_chapter_single_stage(),
    ),
    prompts={
        "generate_outline": _PROMPT_OUTLINE,
        "write_chapter": _PROMPT_WRITE_CHAPTER,
    },
)


BUILTIN_TEMPLATES: dict[str, ContractTemplate] = {
    t.name: t for t in (_LONG_EPIC, _SHORT_STORY)
}

VALID_TEMPLATES: frozenset[str] = frozenset(BUILTIN_TEMPLATES)


def get_template(name: str) -> ContractTemplate:
    """Return the built-in :class:`ContractTemplate` named ``name``."""

    if name not in BUILTIN_TEMPLATES:
        raise ConfigError(
            f"unknown template {name!r}; "
            f"expected one of: {sorted(BUILTIN_TEMPLATES)}"
        )
    return BUILTIN_TEMPLATES[name]
