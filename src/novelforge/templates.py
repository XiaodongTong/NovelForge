"""Built-in pipeline templates (v4 contract model).

The runtime consumes :class:`novelforge.config.StageConfig` records;
this module is the **default data source** for ``nf init``.
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
    "CLAUDE_TEMPLATE",
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
            # PR-2 (spec §AC-8): outline now lives in chapters-outline/
            # rather than the legacy output/summaries/plot.md.  Single
            # file; no split.
            ProduceSpec(path="chapters-outline/outline.md", alias="outline"),
        ),
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="min_chars",
                    target="chapters-outline/outline.md",
                    value=500,
                ),
            ),
        ),
    )


# PR-2 (spec §AC-7, §TD-7): the split regex is locked to ASCII slug-safe
# characters.  Real models are expected to follow the prompt convention
# "角色名仅含 [A-Za-z0-9_-]"; the regex enforces this on the runtime
# side as a safety net (matches outside the charset are silently
# dropped, which surfaces as a downstream VerifyFailed instead of an
# invalid filename).
_CHARACTERS_SPLIT_RE = r"^#\s+(?P<slug>[A-Za-z0-9_-]+)\s*$"


def _characters_stage() -> StageConfig:
    return StageConfig(
        id="design_characters",
        model=_DEFAULT_WRITE_MODEL,
        prompt="prompts/design-characters.md",
        consumes=("generate_outline",),
        produces=(
            # PR-2: one file per character; path uses {{slug}} so the
            # regex's named capture group renders into the filename.
            # No ``|default(...)`` filter — `_format_value` does not
            # honour it (TD-2); the ASCII-safe slug set guarantees
            # non-empty matches.
            ProduceSpec(
                path="characters/{{slug}}.md",
                alias="characters",
                split=_CHARACTERS_SPLIT_RE,
            ),
        ),
        done_when=DoneWhenSpec(
            checks=(
                CheckSpec(
                    kind="min_chars",
                    # Per-slug substitution: verify.py runs this
                    # check against each split file individually
                    # (verify test_done_when_target_substitutes_slug).
                    target="characters/{{slug}}.md",
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
    "**Character Name 硬约束**：每个 ``# <Character Name>`` 标题里的 "
    "名字**只能**含 `[A-Za-z0-9_-]` 字符（字母 / 数字 / `-` / `_`）。"
    "不要用空格、不要用中文、不要用标点。引擎会按 ``^#\\s+(?P<slug>[A-Za-z0-9_-]+)`` "
    "切分输出，落在 ``characters/<slug>.md``——任何不合规的名字会被切分时丢弃并"
    "触发 ``VerifyFailed``。\n\n"
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


# --------------------------------------------------------------------------- #
# CLAUDE.md scaffold template
# --------------------------------------------------------------------------- #
#
# This template is rendered by ``nf init`` (PR-1, spec §AC-13).  It
# documents the directory layout + user/engine boundaries so the user
# never has to guess which file they own vs which the engine writes.
# The leading ``<!-- generated ... -->`` marker is intentional: when
# ``init --force`` rewrites the file the user is reminded that the
# tree changes are managed, not authored.
# --------------------------------------------------------------------------- #


CLAUDE_TEMPLATE = """\
<!-- generated by nf init; do not edit by hand — re-run init if you need to refresh -->

# NovelForge 项目约束

> 引擎在所有 stage 都会把本文件作为 `{{include:}}` 加载。
> 想让规则生效，必须按下面的"语义边界"小节在对的目录里编辑。

## 1. 目录结构与边界

```
<project_dir>/
├── novel-project.yaml          # 契约配置（引擎读，不要手改 stages）
├── CLAUDE.md                   # 本文件：写作约束 / 边界说明
├── outline/                    # 【用户填】故事种子
│   ├── premise.md              #   核心冲突 + 主角北极星
│   └── world.md                #   世界设定：势力 / 时代 / 调性
├── characters/                 # 【引擎填】每个角色一个 .md
│   └── <slug>.md               #   slug 仅含 [A-Za-z0-9_-]
├── chapters-outline/           # 【引擎填】章节大纲
│   └── outline.md              #   generate_outline 的产物
├── output/                     # 运行时产物（review / 最终章节）
│   ├── summaries/              #   旧版兼容保留位（默认空）
│   ├── meta/                   #   旧版兼容保留位（默认空）
│   ├── chapters/               #   最终章节正文
│   └── review/                 #   review JSON + final-polish 报告
└── prompts/                    # 每个 stage 的 prompt 模板
    └── *.md
```

## 2. 写作约束（建议至少保留这五条）

1. 坚持第三人称有限视角，除非 prompt 明确允许其他视角。
2. 每章字数在 `novel.words_per_chapter` 范围内，必要时靠感官细节补足。
3. 不要写"无代价的复活"或"凭空的天降救兵"——每个奇迹都要有名字的代价。
4. 回避现代俚语、品牌名、直接的网络梗。
5. 每章结尾留一个**小型反转**（疑问 / 背叛 / 沉默的代价），推动读者继续。

## 3. 风格指南

- 主角的语气里要藏压抑的悲伤，但不要用对白直接说出来。
- 场景切换时给出明确的时空坐标（章节开头一行）。
- 对话要短促有力，避免长篇独白。
- 高潮场景以动作 + 感官收尾，**不要**用大段心理描写。

## 4. 角色档案要求

`characters/<slug>.md` 必须按以下结构填写（split mode 下引擎按 slug 切文件）：

```
# <Character Name>
**Role**: 主角 / 反派 / 配角 / ...
**Voice**: 一句话描述语言风格
**Relationships**: 与其他角色的关键关系
**Arc**: 在故事里的成长或衰落曲线
```

> **slug 字符集硬约束**：`[A-Za-z0-9_-]+`，**不要**含空格或中文。

## 5. 章节大纲要求

`chapters-outline/outline.md` 必须按以下结构填写（`generate_outline` 的产物）：

```
# Outline
## Chapter 1 - <title>
1-2 句本章关键节拍。
## Chapter 2 - <title>
...
```

引擎会用 `## Chapter N` 标题校验通过与否。

## 6. 引擎接口备忘

- 阶段契约：`produces` / `done_when` / `consumes` 三件套，详见 `docs/plan/stage-contract.md`。
- 上游读取：用 `{{upstream.<id>.<alias>}}` 或 `{{upstream.<id>.<alias>[*]}}`（split 列表）。
- 重试上限：`done_when.max_attempts`，耗尽后按 `on_failure` 处置。
"""
