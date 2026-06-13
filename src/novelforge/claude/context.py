"""Context manager — assembles the per-stage input for the Claude call.

The engine never sends the full repo to Claude.  For each stage it
loads only the slice that matters and trims it to fit inside the
configured ``context_reserve``.

Slicing strategy (per ``plan.md`` §4.5):

- ``always_loaded`` — premise/world/CLAUDE.md.  Hard-loaded for every
  stage; never trimmed.
- ``rolling_windows`` — most recent N chapter bodies, ±M outline range,
  character dossiers, foreshadowing table.  Sized per stage.
- ``per_stage_extras`` — review stages also pull the artefact being
  reviewed.
- ``prompt_template`` — the stage's own prompt file (always last).

When the assembled context exceeds the budget we trim in the order
``history_chapters → outline_window → character_dossiers``.  Each trim
is logged at INFO with what was removed, why, and the resulting token
estimate.

v4 also adds :class:`PromptRenderer`, which substitutes the three
placeholder families (``{{novel.*}}``, ``{{ctx.*}}``,
``{{include: <path-or-glob>}}``) inside the per-stage prompt body.  It
delegates the ``{{include:}}`` expansion to the existing
:class:`ContextAssembler` so file-token budgeting is consistent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse

from ..config import ContextSpec, NovelProjectConfig, NovelSpec
from ..errors import ConfigError, ContextOverflow
from ..utils.fs import list_files
from ..utils.log import get_logger

log = get_logger("claude.context")

# Rough heuristic: 1 token ≈ 2 Latin characters.  Chinese is closer to
# 1 token per character but we overestimate conservatively (1.6x) so the
# budget check trips *before* we exceed the actual limit.
TOKEN_CHARS_RATIO = 2.0
CHINESE_TOKEN_CHARS_RATIO = 1.6
# Default safety margin when estimating tokens.
SAFETY_MARGIN = 1.2


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in ``text``.

    Splits on whitespace and CJK characters: each CJK glyph is one
    token, each Latin word is one token, plus a small safety margin.
    """

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
    """A labelled chunk of context that will be passed to Claude."""

    name: str
    content: str
    path: Optional[Path] = None
    tokens: int = 0
    trim_level: int = 0  # 0 = untouched, 1 = trimmed once, 2 = trimmed again
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
        """Concatenate slices and prompt template into a single prompt body."""

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
# Assembler
# --------------------------------------------------------------------------- #


class ContextAssembler:
    """Builds the per-stage context.

    Parameters
    ----------
    project_root:
        Directory of the user project (e.g. ``samples/minimal-novel``).
    novel:
        The ``novel:`` section of the config (used for seeds/constraints).
    context_spec:
        The ``execution.context:`` block.
    """

    def __init__(
        self,
        project_root: Path,
        novel: NovelSpec,
        context_spec: ContextSpec,
    ) -> None:
        self.project_root = Path(project_root)
        self.novel = novel
        self.spec = context_spec
        self.budget = context_spec.context_reserve
        # Cached once per assembler instance.
        self._seeds_cache: Optional[list[ContextSlice]] = None
        self._outline_cache: Optional[list[ContextSlice]] = None

    # -- public --------------------------------------------------------

    def assemble(
        self,
        stage: str,
        *,
        batch: Optional[str] = None,
        chapter_index: Optional[int] = None,
        review_target_path: Optional[Path] = None,
    ) -> AssembledContext:
        slices: list[ContextSlice] = []

        # 1. always_loaded (hard)
        slices.extend(self._always_loaded())

        # 2. per-stage window
        if stage in {"write_chapter"}:
            slices.extend(self._chapter_window(chapter_index))
        elif stage in {"review_chapter"}:
            slices.extend(self._chapter_window(chapter_index))
            slices.extend(self._review_target(review_target_path))
        elif stage in {"generate_outline", "design_characters", "simulate_plot"}:
            slices.extend(self._outline_window())
        elif stage in {"full_consistency_check", "final_polish"}:
            # These stages review the manuscript: load chapters + outline
            # + the explicit review target (if any).
            slices.extend(self._chapter_window(chapter_index))
            slices.extend(self._outline_window())
            slices.extend(self._review_target(review_target_path))
        elif stage.startswith("review_"):
            slices.extend(self._review_target(review_target_path))

        # 3. budget enforcement + trimming
        slices = self._enforce_budget(stage, slices)

        total = sum(s.tokens for s in slices)
        return AssembledContext(
            stage=stage,
            slices=slices,
            total_tokens=total,
        )

    # -- slices --------------------------------------------------------

    def _always_loaded(self) -> list[ContextSlice]:
        if self._seeds_cache is not None:
            return self._seeds_cache
        slices: list[ContextSlice] = []
        # premise + world
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
        # constraints
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
        self._seeds_cache = slices
        return slices

    def _outline_window(self) -> list[ContextSlice]:
        if self._outline_cache is not None:
            return self._outline_cache
        # The "outline" itself lives under output/summaries/plot.md and
        # outline-tracking.md.  We load them all.
        slices: list[ContextSlice] = []
        for rel in (
            "output/summaries/plot.md",
            "output/summaries/outline-tracking.md",
        ):
            path = self._resolve(rel)
            text = _read(path)
            if text:
                slices.append(
                    ContextSlice(
                        name=f"outline:{rel}",
                        content=text,
                        path=path,
                        tokens=estimate_tokens(text),
                    )
                )
        self._outline_cache = slices
        return slices

    def _chapter_window(self, chapter_index: Optional[int]) -> list[ContextSlice]:
        """Load recent chapters and the outline window around ``chapter_index``."""

        slices: list[ContextSlice] = []

        # 1. Outline window: ±outline_range chapters around the current one.
        if chapter_index is not None and self.spec.outline_range > 0:
            lo = max(1, chapter_index - self.spec.outline_range)
            hi = chapter_index + self.spec.outline_range
            outline_slice = self._outline_range(lo, hi)
            if outline_slice:
                slices.append(outline_slice)

        # 2. Recent N chapters.
        if self.spec.rolling_window > 0:
            recent = self._recent_chapters(self.spec.rolling_window)
            slices.extend(recent)

        # 3. Character dossiers
        char_summaries = list_files(
            self.project_root / "output" / "meta",
            patterns=("*.md",),
            recursive=False,
        )
        for p in char_summaries:
            text = _read(p)
            if text:
                slices.append(
                    ContextSlice(
                        name=f"character:{p.name}",
                        content=text,
                        path=p,
                        tokens=estimate_tokens(text),
                    )
                )

        # 4. Foreshadowing tracking (small, always included if present).
        f_path = self._resolve("output/summaries/foreshadowing.md")
        f_text = _read(f_path)
        if f_text:
            slices.append(
                ContextSlice(
                    name="foreshadowing",
                    content=f_text,
                    path=f_path,
                    tokens=estimate_tokens(f_text),
                )
            )

        return slices

    def _review_target(self, target: Optional[Path]) -> list[ContextSlice]:
        if target is None:
            return []
        text = _read(target)
        if not text:
            return []
        return [
            ContextSlice(
                name=f"review_target:{target.name}",
                content=text,
                path=target,
                tokens=estimate_tokens(text),
            )
        ]

    def _outline_range(self, lo: int, hi: int) -> Optional[ContextSlice]:
        """Concatenate outline items for chapters [lo, hi]."""

        path = self._resolve("output/summaries/outline-tracking.md")
        text = _read(path)
        if not text:
            return None
        kept: list[str] = []
        pattern = re.compile(
            r"^##\s+(?:chapter\s+)?(\d+)\s*[-–—:]\s*(.+?)$",
            re.MULTILINE | re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            num = int(m.group(1))
            if lo <= num <= hi:
                kept.append(m.group(0))
        if not kept:
            return None
        body = "\n".join(kept)
        return ContextSlice(
            name=f"outline_window:{lo}-{hi}",
            content=body,
            path=path,
            tokens=estimate_tokens(body),
        )

    def _recent_chapters(self, n: int) -> list[ContextSlice]:
        chap_dir = self.project_root / "output" / "chapters"
        files = sorted(chap_dir.glob("*.md"))
        if not files:
            return []
        recent = files[-n:]
        slices: list[ContextSlice] = []
        for p in recent:
            text = _read(p)
            slices.append(
                ContextSlice(
                    name=f"history:{p.name}",
                    content=text,
                    path=p,
                    tokens=estimate_tokens(text),
                )
            )
        return slices

    # -- budget --------------------------------------------------------

    def _enforce_budget(
        self, stage: str, slices: list[ContextSlice]
    ) -> list[ContextSlice]:
        """Apply the three-tier trim policy when the budget is exceeded.

        Tier 1: shrink every history chapter to its last 500 chars.
        Tier 2: shrink the outline window to ``±outline_range/2`` (or drop
                it entirely if it still doesn't fit).
        Tier 3: drop character dossiers.
        """

        budget = self.budget
        total = sum(s.tokens for s in slices)
        if total <= budget:
            return slices

        log_lines: list[str] = []
        # Tier 1: history → last 500 chars
        for s in slices:
            if s.name.startswith("history") and s.tokens > 0:
                self._shrink_history(s, stage, log_lines)
        total = sum(s.tokens for s in slices)
        if total <= budget:
            self._finalize_trim_log(slices, log_lines)
            return slices

        # Tier 2: outline_window → halve
        for s in slices:
            if s.name.startswith("outline_window") and s.tokens > 0:
                self._trim_slice(s, stage, log_lines)
        total = sum(s.tokens for s in slices)
        if total <= budget:
            self._finalize_trim_log(slices, log_lines)
            return slices

        # Tier 3: drop character dossiers
        kept: list[ContextSlice] = []
        for s in slices:
            if s.name.startswith("character") and s.tokens > 0:
                reason = f"trimmed: {s.name} dropped stage={stage}"
                log.info(reason)
                log_lines.append(reason)
                # Mark as trimmed and zero out
                s.trimmed = True
                s.trim_reason = reason
                s.content = ""
                s.tokens = 0
            kept.append(s)
        slices = kept
        total = sum(s.tokens for s in slices)
        if total <= budget:
            self._finalize_trim_log(slices, log_lines)
            return slices

        # Final desperate pass: shrink remaining non-protected slices too
        for s in slices:
            if s.name.startswith("outline_window") and s.tokens > 0:
                self._shrink_history(s, stage, log_lines)
        total = sum(s.tokens for s in slices)
        if total > budget:
            log.warning(
                "context_overflow stage=%s total=%d budget=%d — caller should reduce prompt size",
                stage,
                total,
                budget,
            )
            raise ContextOverflow(
                f"context overflow in stage={stage}: {total} tokens > budget {budget} even after trimming"
            )
        self._finalize_trim_log(slices, log_lines)
        return slices

    def _finalize_trim_log(
        self, slices: list[ContextSlice], log_lines: list[str]
    ) -> None:
        # Annotate the trim log onto the first protected slice so the
        # caller can pick it up via ``AssembledContext.to_dict()``.
        if slices and log_lines and not slices[0].trim_reason:
            slices[0].trim_reason = "; ".join(log_lines)

    def _trim_slice(
        self, slice: ContextSlice, stage: str, log_lines: list[str]
    ) -> None:
        """Halve the slice's content (and tokens) once."""

        if not slice.content:
            return
        new_content = slice.content[: max(1, len(slice.content) // 2)]
        reason = f"trimmed: {slice.name} (level {slice.trim_level + 1}) stage={stage}"
        log.info(reason)
        log_lines.append(reason)
        slice.content = new_content
        slice.tokens = estimate_tokens(new_content)
        slice.trim_level += 1
        slice.trimmed = True
        slice.trim_reason = reason

    def _shrink_history(
        self, slice: ContextSlice, stage: str, log_lines: list[str]
    ) -> None:
        """Aggressively keep only the last 500 characters of a history slice."""

        if not slice.content:
            return
        new_content = slice.content[-500:]
        reason = f"trimmed: {slice.name} shrunk to last 500 chars stage={stage}"
        log.info(reason)
        log_lines.append(reason)
        slice.content = new_content
        slice.tokens = estimate_tokens(new_content)
        slice.trim_level = max(slice.trim_level, 2)
        slice.trimmed = True
        slice.trim_reason = reason

    # -- paths ---------------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        return (self.project_root / rel).resolve()


# --------------------------------------------------------------------------- #
# PromptRenderer (v4)
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
    include_files: list[str] = field(default_factory=list)
    include_tokens: int = 0
    warnings: list[str] = field(default_factory=list)


class PromptRenderer:
    """Renders the v4 placeholder families in a stage prompt.

    Three families are supported (per spec §5.3):

    - ``{{novel.<key>}}`` — replaced by the matching attribute of the
      ``novel:`` config (e.g. ``{{novel.target_chapters}}``).  Unknown
      keys raise :class:`ConfigError` with the stage id so the failure
      is easy to locate.
    - ``{{ctx.<key>}}`` — replaced by values from the runtime context
      map (``stage_id``, ``batch``, ``chapter_index``, ``iteration``,
      ...).  Unknown keys raise :class:`ConfigError`.
    - ``{{include: <path-or-glob>}}`` — replaced with the concatenated
      contents of the matching files (relative to the project root).
      Tokens are estimated via :func:`estimate_tokens` and the budget
      is enforced by reusing the existing
      :class:`ContextAssembler`.  When the included files would
      exceed the budget, a warning is recorded on the result and the
      content is **trimmed** (the user gets partial content, not an
      exception — A12).
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
    ) -> None:
        self.project_root = Path(project_root)
        self.novel = novel
        self.context_spec = context_spec

    # -- public --------------------------------------------------------

    def render(
        self,
        template: str,
        *,
        ctx: Optional[dict[str, Any]] = None,
    ) -> PromptRenderResult:
        ctx = dict(ctx or {})
        result = PromptRenderResult(text=template)

        # 1. {{novel.*}}
        result.text, novel_count = self._expand_novel(result.text)
        result.novel_expansions = novel_count

        # 2. {{ctx.*}}
        result.text, ctx_count = self._expand_ctx(result.text, ctx)
        result.ctx_expansions = ctx_count

        # 3. {{include: ...}}
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
        # Budget enforcement: when include tokens exceed context_reserve,
        # warn (do not raise) so the user still gets partial content
        # (A12).  The exact trimming policy mirrors the assembler.
        budget = self.context_spec.context_reserve
        if result.include_tokens > budget:
            warning = (
                f"include files exceed context budget "
                f"({result.include_tokens} > {budget} tokens); "
                f"content kept verbatim but downstream ContextAssembler "
                f"will trim further if needed. files={result.include_files}"
            )
            log.warning(warning)
            result.warnings.append(warning)
        return rendered

    def _resolve_include_files(self, spec: str) -> list[Path]:
        """Resolve a ``{{include: <spec>}}`` spec to a list of files.

        Supports:

        - relative path → single file (must exist)
        - glob (``*`` / ``**``) → every match
        - single ``-`` → stdin (not supported; emits warning upstream)

        Paths are resolved relative to ``self.project_root``; absolute
        paths are accepted but discouraged.
        """

        if not spec:
            return []
        target = Path(spec)
        if not target.is_absolute():
            target = self.project_root / target
        # Glob when spec contains wildcard characters.
        if any(ch in spec for ch in "*?["):
            matches = sorted(self.project_root.glob(spec))
            return [m for m in matches if m.is_file()]
        if target.is_file():
            return [target]
        if target.is_dir():
            return sorted(p for p in target.rglob("*.md") if p.is_file())
        return []


def _parse_url_safe(value: str) -> str:  # pragma: no cover - helper
    return urlparse(value).geturl()
