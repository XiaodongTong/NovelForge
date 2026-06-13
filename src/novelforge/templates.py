"""Built-in pipeline templates (v4 data source).

Templates used to live as a list of stage ids in :mod:`novelforge.config`
and as individual Python classes in :mod:`novelforge.stages`.  v4
unifies the data model: every stage — whether the user wrote it by hand
or it came from ``init``/``migrate`` — is an 8-field :class:`StageConfig`
record (see :mod:`novelforge.config`).

This module is the **only** place the engine stores the v3 built-in
prompts and the per-stage defaults.  ``init`` uses it to materialise a
fresh project; ``migrate`` uses it to upgrade a v3 yaml; the runtime
falls back to it when the user supplies only ``template: long-epic``
in their yaml (with a DeprecationWarning, per spec §5.5.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

# Stage ids covered by the v3 stage classes.  v4 still has to be able to
# scaffold them when a user runs ``init --template long-epic`` on a
# clean directory.
ALL_STAGE_IDS: tuple[str, ...] = (
    "generate_outline",
    "review_outline",
    "design_characters",
    "review_characters",
    "simulate_plot",
    "review_simulation",
    "write_chapter",
    "review_chapter",
    "full_consistency_check",
    "final_polish",
)

# Built-in templates map → ordered list of stage ids.  The runtime
# never reads these directly (the orchestrator consumes
# ``PipelineSpec.stages``) but ``init`` / ``migrate`` need them.
PIPELINE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "long-epic": (
        "generate_outline",
        "review_outline",
        "design_characters",
        "review_characters",
        "simulate_plot",
        "review_simulation",
        "write_chapter",
        "review_chapter",
        "full_consistency_check",
        "final_polish",
    ),
    "short-story": (
        "generate_outline",
        "review_outline",
        "design_characters",
        "review_characters",
        "write_chapter",
        "review_chapter",
        "final_polish",
    ),
    "series": (
        "generate_outline",
        "review_outline",
        "design_characters",
        "review_characters",
        "simulate_plot",
        "review_simulation",
        "write_chapter",
        "review_chapter",
        "full_consistency_check",
    ),
}

VALID_TEMPLATES: frozenset[str] = frozenset(PIPELINE_TEMPLATES)


# --------------------------------------------------------------------------- #
# Stage template record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StageTemplate:
    """A single stage's v4 defaults.

    Mirrors :class:`novelforge.config.StageConfig` field-for-field so
    that ``init`` / ``migrate`` can lift the record into the user's
    yaml with no transformation.  ``prompt_text`` is the inline prompt
    body; ``init`` writes it to ``prompts/<prompt_file>`` and sets
    ``prompt`` to the relative path.
    """

    id: str
    model: str
    prompt_text: str
    output: str
    split: Optional[str] = None
    batch: int = 1
    on_failure: str = "pause"
    enabled: bool = True
    # ---- migration helpers ----
    # The recommended file name when materialising ``prompt_text`` to
    # the project's prompts/ directory.  Derived from the stage id.
    prompt_file: str = field(default="")

    def __post_init__(self) -> None:
        # frozen dataclass: mutate via object.__setattr__.
        if not self.prompt_file:
            object.__setattr__(self, "prompt_file", _id_to_prompt_file(self.id))

    def to_dict(self) -> dict[str, Any]:
        """Render as a v4 yaml-ready mapping (prompt inlined)."""

        d: dict[str, Any] = {
            "id": self.id,
            "model": self.model,
            "prompt": self.prompt_text,
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


def _id_to_prompt_file(stage_id: str) -> str:
    """Map a stage id to the conventional prompt filename.

    e.g. ``write_chapter`` → ``write-chapter.md``.  Single source of
    truth so ``init`` writes the same filename the runtime would
    expect.
    """

    return stage_id.replace("_", "-") + ".md"


# --------------------------------------------------------------------------- #
# Default stage templates (10 stages, the long-epic set)
# --------------------------------------------------------------------------- #


STAGE_TEMPLATES: tuple[StageTemplate, ...] = (
    StageTemplate(
        id="generate_outline",
        model="claude-opus-4-7",
        output="output/summaries/plot.md",
        prompt_text=(
            "You are a senior webnovel planner.  Produce:\n\n"
            "1. A high-level plot arc (themes, antagonists, the "
            "protagonist's inner arc).\n"
            "2. A chapter-by-chapter outline with one "
            "``## Chapter N - <title>`` heading for every chapter in the "
            "target, each followed by 1-2 sentences describing the key "
            "beat.\n\n"
            "Do not write prose.  Output markdown only."
        ),
    ),
    StageTemplate(
        id="review_outline",
        model="claude-sonnet-4-6",
        output="output/review/outline-review.json",
        prompt_text=(
            "You are a senior webnovel editor.  Review the outline above "
            "and return a JSON object with:\n\n"
            "- ``passed``: boolean\n"
            "- ``route``: ``\"done\"`` (approved) or the id of the stage "
            "to revisit (e.g. ``\"generate_outline\"``)\n"
            "- ``findings``: list of strings\n"
            "- ``required_changes``: list of strings\n"
            "- ``summary``: short paragraph\n\n"
            "Be strict: any plot hole or pacing issue must be flagged.  "
            "Output only the JSON object."
        ),
    ),
    StageTemplate(
        id="design_characters",
        model="claude-opus-4-7",
        output="output/meta/characters.md",
        prompt_text=(
            "You are a character designer for a long-form webnovel.  Read "
            "the outline above and produce a dossier for every named "
            "character.  Use the format:\n\n"
            "# <Character Name>\n\n"
            "**Role**: ...\n"
            "**Voice**: ...\n"
            "**Relationships**: ...\n"
            "**Arc**: ...\n\n"
            "One dossier per character, separated by a single blank "
            "line.  Output only the dossiers."
        ),
    ),
    StageTemplate(
        id="review_characters",
        model="claude-sonnet-4-6",
        output="output/review/characters-review.json",
        prompt_text=(
            "Review the character dossiers above and return a JSON "
            "object with ``passed``, ``route``, ``findings``, "
            "``required_changes`` and ``summary`` fields.  Set "
            "``route`` to ``\"done\"`` when the dossiers are ready."
        ),
    ),
    StageTemplate(
        id="simulate_plot",
        model="claude-opus-4-7",
        output="output/summaries/plot-simulation.md",
        prompt_text=(
            "Act as a \"table read\" reviewer.  Walk through the outline "
            "chapter by chapter and identify:\n\n"
            "- escalation gaps (where the tension does not compound)\n"
            "- foreshadowing payoffs (what was set up, when it lands)\n"
            "- character motivation issues (where behaviour feels "
            "forced)\n"
            "- chapter-level pacing notes\n\n"
            "Output a single markdown file with one section per topic."
        ),
    ),
    StageTemplate(
        id="review_simulation",
        model="claude-sonnet-4-6",
        output="output/review/simulation-review.json",
        prompt_text=(
            "Review the plot simulation above and return a JSON object "
            "with ``passed``, ``route``, ``findings``, "
            "``required_changes`` and ``summary`` fields.  Set "
            "``route`` to ``\"done\"`` when the simulation is sound."
        ),
    ),
    StageTemplate(
        id="write_chapter",
        model="claude-opus-4-7",
        output="output/chapters/{{num:03d}}-{{title|slug}}.md",
        split=(
            r"^#\s+[Cc]hapter\s+"
            r"(?P<num>\d+)\s*[-–—:]?\s*"
            r"(?P<title>.+?)\s*$"
        ),
        batch=1,
        prompt_text=(
            "Write the next chapter(s) of the novel.  Use the outline "
            "beat above as your target.  Output ``# Chapter N - <Title>`` "
            "followed by 800-1500 Chinese characters of prose per "
            "chapter.  End on a small reversal."
        ),
    ),
    StageTemplate(
        id="review_chapter",
        model="claude-sonnet-4-6",
        output="output/review/chapter-review.json",
        prompt_text=(
            "You are reviewing the latest chapter.  Return a JSON object "
            "with the keys ``passed``, ``route``, ``findings``, "
            "``required_changes`` and ``summary``.  Set ``route`` to "
            "``\"write_chapter`` if the chapter needs a rewrite, or "
            "``\"done`` if it can move on."
        ),
    ),
    StageTemplate(
        id="full_consistency_check",
        model="claude-sonnet-4-6",
        output="output/review/consistency-report.md",
        prompt_text=(
            "Read the outline and all chapter files.  Produce a "
            "consistency report covering:\n\n"
            "- Character name & trait consistency\n"
            "- Foreshadowing payoff (every setup has a corresponding "
            "pay-off)\n"
            "- Timeline continuity\n"
            "- World rule violations\n\n"
            "Be terse and list every issue with a chapter reference.  "
            "If nothing is wrong, say so explicitly."
        ),
    ),
    StageTemplate(
        id="final_polish",
        model="claude-opus-4-7",
        output="output/review/final-polish-notes.md",
        prompt_text=(
            "Read the manuscript and produce a final-polish brief.  For "
            "every chapter, list at most three tweaks (word choice, "
            "sentence rhythm, clarity).  Be terse.  Output markdown."
        ),
    ),
)

# Sanity: every built-in id from PIPELINE_TEMPLATES has a template.
_TEMPLATES_BY_ID: dict[str, StageTemplate] = {t.id: t for t in STAGE_TEMPLATES}
for _tid in ALL_STAGE_IDS:
    if _tid not in _TEMPLATES_BY_ID:  # pragma: no cover - guard
        raise RuntimeError(f"templates.py missing record for {_tid}")


def get_template(stage_id: str) -> StageTemplate:
    """Return the built-in template for ``stage_id``.

    Raises ``KeyError`` if the id is not in :data:`STAGE_TEMPLATES`.
    """

    return _TEMPLATES_BY_ID[stage_id]


def get_template_mapping(
    stage_ids: Sequence[str],
) -> dict[str, StageTemplate]:
    """Return ``{stage_id: template}`` for each id in ``stage_ids``."""

    return {sid: _TEMPLATES_BY_ID[sid] for sid in stage_ids}


__all__ = [
    "ALL_STAGE_IDS",
    "PIPELINE_TEMPLATES",
    "VALID_TEMPLATES",
    "STAGE_TEMPLATES",
    "StageTemplate",
    "get_template",
    "get_template_mapping",
]
