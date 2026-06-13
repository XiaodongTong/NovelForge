"""Context manager + prompt renderer (contract model).

The v4 contract replaces the old stage-id-keyed context assembly with
an :class:`ArtifactRegistry`-driven data flow.  A stage's
``consumes`` declaration picks which upstream stages' produces it can
see; the renderer then exposes those via the
``{{upstream.<stage_id>.<alias>}}`` placeholder family.

Storage-form rules (spec §AC-4):

- **single-product stage** (registry stores ``Path``):
  - ``{{upstream.<id>.<alias>}}`` → file content
  - ``{{upstream.<id>.<alias>.path}}`` → path string
  - ``[*]`` suffix → ``ConfigError``
- **batch / split stage** (registry stores ``list[Path]``):
  - ``{{upstream.<id>.<alias>[*]}}`` → multi-line content (one file per
    line block, joined with newlines)
  - ``{{upstream.<id>.<alias>[*].path}}`` → multi-line path list
  - bare ``{{upstream.<id>.<alias>}}`` / ``.path`` (no ``[*]``) →
    ``ConfigError``

Token budget (spec §6): when the assembled upstream content exceeds
the configured reserve, the assembler trims **in reverse consumes
order** (most-recently-declared-first survives) and emits a warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..artifact_registry import ArtifactRegistry
from ..config import ContextSpec, NovelSpec
from ..errors import ConfigError, ContextOverflow
from ..utils.fs import list_files
from ..utils.log import get_logger

log = get_logger("claude.context")

# Rough heuristic: 1 token ≈ 2 Latin characters.  CJK glyphs are closer
# to 1 token per character; we overestimate conservatively.
TOKEN_CHARS_RATIO = 2.0
CHINESE_TOKEN_CHARS_RATIO = 1.6
SAFETY_MARGIN = 1.2


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in ``text``."""

    if not text:
        return 0
    cjk = re.findall(r"[一-鿿]", text)
    cjk_count = len(cjk)
    remainder = re.sub(r"[一-鿿]", " ", text)
    words = [w for w in re.split(r"\s+", remainder) if w]
    estimate = cjk_count + len(words)
    return int(estimate * SAFETY_MARGIN)


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


@dataclass
class ContextSlice:
    """A labelled chunk of context passed to Claude."""

    name: str
    content: str
    path: Optional[Path] = None
    tokens: int = 0
    trim_level: int = 0
    trimmed: bool = False
    trim_reason: Optional[str] = None


@dataclass
class AssembledContext:
    """The full context for a single Claude call."""

    stage: str
    slices: list[ContextSlice] = field(default_factory=list)
    prompt_template: str = ""
    total_tokens: int = 0
    trim_log: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts: list[str] = []
        for s in self.slices:
            if not s.content:
                continue
            header = f"\n\n# {s.name}"
            if s.trimmed:
                header += " (trimmed)"
            parts.append(header + "\n" + s.content)
        if self.prompt_template:
            parts.append("\n\n# Task instructions\n" + self.prompt_template)
        return "".join(parts).strip() + "\n"

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "total_tokens": self.total_tokens,
            "slices": [
                {
                    "name": s.name,
                    "tokens": s.tokens,
                    "trim_level": s.trim_level,
                    "trimmed": s.trimmed,
                    "trim_reason": s.trim_reason,
                }
                for s in self.slices
            ],
            "trim_log": list(self.trim_log),
        }


# --------------------------------------------------------------------------- #
# Upstream placeholder expansion
# --------------------------------------------------------------------------- #


_UPSTREAM_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*upstream\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\[?\*?\]?\.?(path)?)\s*\}\}"
)


def expand_upstream_placeholders(
    text: str,
    registry: ArtifactRegistry,
    *,
    stage_id: str,
) -> str:
    """Substitute every ``{{upstream.<id>.<alias>...}}`` in ``text``.

    Form mismatches (single produce used with ``[*]``, batch produce
    used without ``[*]``) raise :class:`ConfigError` (spec §AC-4).
    """

    def repl(match: re.Match[str]) -> str:
        full = match.group(0)
        upstream_id = match.group(1)
        alias = match.group(2)
        suffix = match.group(3) or ""
        # Detect the [*] marker by re-reading the raw token; the regex
        # above normalises it away so we look for the literal text.
        is_list_marker = "[*]" in full
        if not registry.has(upstream_id, alias):
            raise ConfigError(
                f"prompt placeholder {full!r} in stage {stage_id!r} "
                f"references unknown upstream {upstream_id}.{alias}; "
                f"check `consumes` or upstream `produces.alias`"
            )
        is_list_storage = registry.is_list(upstream_id, alias)
        if is_list_marker and not is_list_storage:
            raise ConfigError(
                f"prompt placeholder {full!r} in stage {stage_id!r}: "
                f"{upstream_id}.{alias} is a single produce; "
                f"[*] suffix is not allowed"
            )
        if (not is_list_marker) and is_list_storage:
            raise ConfigError(
                f"prompt placeholder {full!r} in stage {stage_id!r}: "
                f"{upstream_id}.{alias} is a batch / split produce; "
                f"must use [*] suffix"
            )
        if is_list_storage:
            paths = registry.get_list(upstream_id, alias)
            if suffix == "path":
                return "\n".join(str(p) for p in paths)
            chunks = []
            for p in paths:
                chunks.append(f"--- {p} ---\n{_read(p)}")
            return "\n\n".join(chunks)
        path = registry.get_one(upstream_id, alias)
        if suffix == "path":
            return str(path)
        return _read(path)

    return _UPSTREAM_PLACEHOLDER_RE.sub(repl, text)


# --------------------------------------------------------------------------- #
# ContextAssembler
# --------------------------------------------------------------------------- #


class ContextAssembler:
    """Builds the per-stage prompt body.

    The assembler's main job in the contract model is to:

    1. Resolve ``consumes`` to a list of upstream stage ids (default =
       all upstream stages that have registered produces).
    2. Materialise each upstream alias as a :class:`ContextSlice` so
       the token budget can be enforced.
    3. Expose the ``{{upstream.*}}`` placeholder family for inline
       references inside the prompt template.
    """

    def __init__(
        self,
        project_root: Path,
        novel: NovelSpec,
        context_spec: ContextSpec,
        registry: ArtifactRegistry,
    ) -> None:
        self.project_root = Path(project_root)
        self.novel = novel
        self.spec = context_spec
        self.budget = context_spec.context_reserve
        self.registry = registry

    # -- public --------------------------------------------------------

    def assemble(
        self,
        stage_id: str,
        *,
        consumes: Optional[Sequence[str]],
        executed_stages: Sequence[str],
    ) -> AssembledContext:
        slices: list[ContextSlice] = []
        slices.extend(self._always_loaded())

        resolved = self._resolve_consumes(consumes, executed_stages)
        for upstream_id in resolved:
            slices.extend(self._slices_for_stage(upstream_id))

        slices = self._enforce_budget(stage_id, slices)
        total = sum(s.tokens for s in slices)
        return AssembledContext(
            stage=stage_id,
            slices=slices,
            total_tokens=total,
        )

    def render_template(
        self,
        stage_id: str,
        template: str,
    ) -> str:
        """Expand ``{{upstream.*}}`` placeholders in a prompt template."""

        return expand_upstream_placeholders(template, self.registry, stage_id=stage_id)

    # -- slices --------------------------------------------------------

    def _always_loaded(self) -> list[ContextSlice]:
        slices: list[ContextSlice] = []
        for rel in self.novel.seeds:
            path = self._resolve(rel)
            text = _read(path)
            slices.append(
                ContextSlice(
                    name=f"seed:{rel}",
                    content=text,
                    path=path,
                    tokens=estimate_tokens(text),
                )
            )
        for rel in self.novel.constraints:
            path = self._resolve(rel)
            text = _read(path)
            slices.append(
                ContextSlice(
                    name=f"constraint:{rel}",
                    content=text,
                    path=path,
                    tokens=estimate_tokens(text),
                )
            )
        return slices

    def _slices_for_stage(self, stage_id: str) -> list[ContextSlice]:
        if stage_id not in self.registry:
            return []
        out: list[ContextSlice] = []
        for alias in self.registry.aliases(stage_id):
            if self.registry.is_list(stage_id, alias):
                paths = self.registry.get_list(stage_id, alias)
                for idx, path in enumerate(paths, start=1):
                    out.append(self._slice_for_path(stage_id, f"{alias}[{idx}]", path))
            else:
                path = self.registry.get_one(stage_id, alias)
                out.append(self._slice_for_path(stage_id, alias, path))
        return out

    @staticmethod
    def _slice_for_path(stage_id: str, alias: str, path: Path) -> ContextSlice:
        text = _read(path)
        return ContextSlice(
            name=f"upstream:{stage_id}.{alias}",
            content=text,
            path=path,
            tokens=estimate_tokens(text),
        )

    @staticmethod
    def _resolve_consumes(
        consumes: Optional[Sequence[str]],
        executed_stages: Sequence[str],
    ) -> list[str]:
        """Honour the consumes contract (AC-5 / AC-6).

        - ``None`` → all executed upstreams.
        - ``[]`` → no upstreams.
        - ``[a, b]`` → exactly a, b (in declared order).
        """

        if consumes is None:
            return list(executed_stages)
        return [c for c in consumes]

    # -- budget --------------------------------------------------------

    def _enforce_budget(
        self, stage: str, slices: list[ContextSlice]
    ) -> list[ContextSlice]:
        """Trim slices in reverse-consumes order when over budget."""

        budget = self.budget
        total = sum(s.tokens for s in slices)
        if total <= budget:
            return slices

        log_lines: list[str] = []
        # Drop upstream slices first (in reverse order = least-recent
        # first); keep seeds + constraints.
        upstream_indices = [
            i for i, s in enumerate(slices)
            if s.name.startswith("upstream:")
        ]
        for i in reversed(upstream_indices):
            if total <= budget:
                break
            s = slices[i]
            reason = f"trimmed: {s.name} dropped stage={stage} (token budget)"
            log.info(reason)
            log_lines.append(reason)
            s.trimmed = True
            s.trim_reason = reason
            total -= s.tokens
            s.tokens = 0
            s.content = ""

        total = sum(s.tokens for s in slices)
        if total > budget:
            log.warning(
                "context_overflow stage=%s total=%d budget=%d",
                stage,
                total,
                budget,
            )
            raise ContextOverflow(
                f"context overflow in stage={stage}: {total} tokens > "
                f"budget {budget} even after trimming"
            )

        if log_lines and slices:
            slices[0].trim_reason = "; ".join(log_lines)
        return slices

    # -- paths ---------------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        return (self.project_root / rel).resolve()


# --------------------------------------------------------------------------- #
# PromptRenderer ({{novel.*}} / {{ctx.*}} / {{include:}})
# --------------------------------------------------------------------------- #


_NOVEL_PLACEHOLDER_RE = re.compile(r"\{\{\s*novel\.([A-Za-z0-9_]+)\s*\}\}")
_CTX_PLACEHOLDER_RE = re.compile(r"\{\{\s*ctx\.([A-Za-z0-9_]+)\s*\}\}")
_INCLUDE_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*include\s*:\s*([^}]+?)\s*\}\}"
)


@dataclass
class PromptRenderResult:
    """The output of a :meth:`PromptRenderer.render` call."""

    text: str
    novel_expansions: int = 0
    ctx_expansions: int = 0
    upstream_expansions: int = 0
    include_files: list[str] = field(default_factory=list)
    include_tokens: int = 0
    warnings: list[str] = field(default_factory=list)


class PromptRenderer:
    """Renders the v4 placeholder families in a stage prompt.

    Supported families:

    - ``{{novel.<key>}}`` — replaced by the matching attribute of the
      ``novel:`` config.
    - ``{{ctx.<key>}}`` — replaced by values from the runtime context
      map (``stage_id``, ``batch``, ``attempt``, ``last_failure_type``,
      ...).
    - ``{{include: <path-or-glob>}}`` — replaced with the concatenated
      contents of the matching files (relative to the project root).
    - ``{{upstream.<id>.<alias>...}}`` — replaced with upstream
      produce contents (handled via :func:`expand_upstream_placeholders`).
    """

    _NOVEL_ATTRS: dict[str, str] = {
        "title": "title",
        "genre": "genre",
        "target_chapters": "target_chapters",
        "words_per_chapter_min": "_wpc_min",
        "words_per_chapter_max": "_wpc_max",
        "style": "style",
    }

    def __init__(
        self,
        *,
        project_root: Path,
        novel: NovelSpec,
        context_spec: ContextSpec,
        registry: Optional[ArtifactRegistry] = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.novel = novel
        self.context_spec = context_spec
        self.registry = registry

    # -- public --------------------------------------------------------

    def render(
        self,
        template: str,
        *,
        ctx: Optional[dict[str, Any]] = None,
        stage_id: Optional[str] = None,
    ) -> PromptRenderResult:
        ctx = dict(ctx or {})
        result = PromptRenderResult(text=template)

        # 1. {{novel.*}}
        result.text, novel_count = self._expand_novel(result.text)
        result.novel_expansions = novel_count

        # 2. {{ctx.*}}
        result.text, ctx_count = self._expand_ctx(result.text, ctx)
        result.ctx_expansions = ctx_count

        # 3. {{upstream.*}}
        if self.registry is not None and stage_id is not None:
            before = result.text
            result.text = expand_upstream_placeholders(
                result.text, self.registry, stage_id=stage_id
            )
            result.upstream_expansions = before.count("{{upstream.") - result.text.count(
                "{{upstream."
            )

        # 4. {{include: ...}}
        result.text = self._expand_includes(
            result.text,
            result=result,
        )
        return result

    # -- novel ---------------------------------------------------------

    def _expand_novel(self, text: str) -> tuple[str, int]:
        count = 0

        def repl(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            key = match.group(1)
            value = self._novel_value(key)
            return str(value)

        return _NOVEL_PLACEHOLDER_RE.sub(repl, text), count

    def _novel_value(self, key: str) -> Any:
        if key in {"words_per_chapter_min", "words_per_chapter_max"}:
            lo, hi = self.novel.words_per_chapter
            if key.endswith("_min"):
                return lo
            return hi
        if not hasattr(self.novel, key):
            raise ConfigError(
                f"prompt references unknown novel attribute {key!r}; "
                f"allowed: {sorted(self._NOVEL_ATTRS)}"
            )
        return getattr(self.novel, key)

    # -- ctx -----------------------------------------------------------

    def _expand_ctx(
        self,
        text: str,
        ctx: dict[str, Any],
    ) -> tuple[str, int]:
        count = 0

        def repl(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            key = match.group(1)
            if key not in ctx:
                raise ConfigError(
                    f"prompt references unknown ctx attribute {key!r}; "
                    f"available: {sorted(ctx)}"
                )
            return str(ctx[key])

        return _CTX_PLACEHOLDER_RE.sub(repl, text), count

    # -- include -------------------------------------------------------

    def _expand_includes(
        self,
        text: str,
        *,
        result: PromptRenderResult,
    ) -> str:
        if "{{include:" not in text:
            return text

        def repl(match: re.Match[str]) -> str:
            spec = match.group(1).strip()
            files = self._resolve_include_files(spec)
            if not files:
                result.warnings.append(
                    f"{{include: {spec}}} did not match any files"
                )
                return ""
            chunks: list[str] = []
            for path in files:
                try:
                    rel = str(path.relative_to(self.project_root))
                except ValueError:
                    rel = str(path)
                result.include_files.append(rel)
                content = _read(path)
                chunks.append(f"--- {rel} ---\n{content}")
            joined = "\n\n".join(chunks) + "\n"
            result.include_tokens += estimate_tokens(joined)
            return joined

        rendered = _INCLUDE_PLACEHOLDER_RE.sub(repl, text)
        budget = self.context_spec.context_reserve
        if result.include_tokens > budget:
            warning = (
                f"include files exceed context budget "
                f"({result.include_tokens} > {budget} tokens); "
                f"content kept verbatim but downstream ContextAssembler "
                f"will trim further if needed."
            )
            log.warning(warning)
            result.warnings.append(warning)
        return rendered

    def _resolve_include_files(self, spec: str) -> list[Path]:
        if not spec:
            return []
        target = Path(spec)
        if not target.is_absolute():
            target = self.project_root / target
        if any(ch in spec for ch in "*?["):
            matches = sorted(self.project_root.glob(spec))
            return [m for m in matches if m.is_file()]
        if target.is_file():
            return [target]
        if target.is_dir():
            return sorted(p for p in target.rglob("*.md") if p.is_file())
        return []


__all__ = [
    "ContextAssembler",
    "ContextSlice",
    "AssembledContext",
    "PromptRenderer",
    "PromptRenderResult",
    "estimate_tokens",
    "expand_upstream_placeholders",
]
