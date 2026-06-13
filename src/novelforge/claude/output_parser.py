"""Output-form parsing for v4 contract stages.

The v4 contract makes ``produces`` a list of :class:`ProduceSpec`
records.  Each produce declares:

- ``path`` — the destination template (possibly with ``{{num}}`` /
  ``{{name}}`` placeholders).
- ``alias`` — the downstream-facing identifier.
- ``split`` — an optional regex; when set, a single model response is
  sliced into multiple files (one per regex match).

Three storage forms are recognised (spec §AC-1, AC-7, AC-8):

- ``text`` — write the raw output verbatim to a single file.
- ``json`` — parse the response as JSON, validate, write the JSON to
  the file.
- ``split`` — split the response on a regex (one match per file) and
  render the file name from each match's capture groups.

Rules (per spec §5.4):

- Final suffix ``.json`` ⇒ ``json`` (when ``split`` is not set).
- ``split`` field ⇒ ``split`` (incompatible with ``.json`` suffix,
  enforced in :mod:`novelforge.config`).
- Otherwise ⇒ ``text``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..config import ProduceSpec
from ..errors import OutputParseError, SchemaInvalid, VerifyFailed
from ..utils.fs import atomic_write, ensure_dir
from ..utils.log import get_logger

log = get_logger("claude.output_parser")

# OutputForm kept as a string alias for backwards compatibility.
OutputForm = str
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


# --------------------------------------------------------------------------- #
# Inference (pure function over a single path string)
# --------------------------------------------------------------------------- #


def infer_form(path_template: str) -> str:
    """Return ``"text"`` or ``"json"`` based on the path's suffix.

    Note (spec §AC-16): ``split`` is *not* inferred from the path; it
    is triggered by the ``produces[].split`` field.  This function
    keeps the v3 signature (single path string) so existing call sites
    that don't care about split still work.
    """

    if not isinstance(path_template, str) or not path_template.strip():
        raise ValueError("output template must be a non-empty string")
    if path_template.rstrip().endswith(".json"):
        return "json"
    return "text"


# --------------------------------------------------------------------------- #
# ParseResult
# --------------------------------------------------------------------------- #


@dataclass
class ProduceParseResult:
    """Per-produce parse outcome."""

    alias: str
    form: str  # "text" | "json" | "split"
    paths: list[Path] = field(default_factory=list)
    parsed: Optional[dict[str, Any]] = None  # JSON form only
    segments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ParseResult:
    """Aggregate parse result for one stage invocation."""

    produces: list[ProduceParseResult] = field(default_factory=list)

    @property
    def all_paths(self) -> list[Path]:
        out: list[Path] = []
        for p in self.produces:
            out.extend(p.paths)
        return out


# --------------------------------------------------------------------------- #
# Path template rendering (placeholder substitution)
# --------------------------------------------------------------------------- #


_FILTER_SLUG = re.compile(r"[^A-Za-z0-9\-_]+")


def _slugify(value: str, max_len: int = 60) -> str:
    s = _FILTER_SLUG.sub("-", value.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "untitled")[:max_len]


def _format_value(raw: str, value: Any) -> str:
    """Apply a single placeholder format spec to ``value``."""

    if "|" in raw:
        head, _, filt = raw.partition("|")
        head = head.strip()
        filt = filt.strip()
    else:
        head, filt = raw.strip(), ""
    if filt == "slug":
        return _slugify("" if value is None else str(value))
    spec = head
    if ":" in spec:
        spec = spec.split(":", 1)[1].strip()
    m = re.match(r"^0?(\d+)d$", spec)
    if m:
        try:
            ivalue = int(value)
        except (TypeError, ValueError) as exc:
            raise OutputParseError(
                f"placeholder {raw!r} expects an integer value, got {value!r}"
            ) from exc
        return f"{ivalue:0{int(m.group(1))}d}"
    return "" if value is None else str(value)


def render_path_template(
    path_template: str,
    values: Optional[Mapping[str, Any]] = None,
) -> str:
    """Substitute ``{{var}}`` / ``{{var:03d}}`` / ``{{var|slug}}`` markers."""

    values = values or {}

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        name = raw
        for sep in (":", "|"):
            if sep in name:
                name = name.split(sep, 1)[0].strip()
                break
        if name not in values:
            raise OutputParseError(
                f"output template references unknown variable {name!r}; "
                f"defined: {sorted(values)}"
            )
        return _format_value(raw, values[name])

    return PLACEHOLDER_RE.sub(repl, path_template)


# --------------------------------------------------------------------------- #
# Per-form writers
# --------------------------------------------------------------------------- #


def _write_text_file(target: Path, raw_output: str) -> Path:
    ensure_dir(target.parent)
    atomic_write(target, raw_output)
    return target


def _write_json_file(
    target: Path,
    raw_output: str,
    *,
    stage_id: str,
) -> tuple[Path, dict[str, Any]]:
    # Empty output is a Tier C "model incomplete" case (spec §4.3:
    # "没写产物 / 写残") — the model produced nothing, so the stage
    # deserves a whole-stage retry with an ``attempt_hint`` rather than
    # an immediate Tier B pause.  Raising VerifyFailed routes through
    # the same C-tier loop as the second-layer check failures.
    if not (raw_output or "").strip():
        raise VerifyFailed(
            f"stage {stage_id!r}: output is declared as .json but the "
            f"model returned empty output",
            stage_id=stage_id,
            target=str(target),
            kind="json_field",
            expected="valid JSON object",
            actual="empty",
        )
    payload, err = _parse_json_object(raw_output)
    if err is not None:
        raise SchemaInvalid(
            f"stage {stage_id!r}: output is declared as .json but the "
            f"model returned a non-JSON payload: {err}"
        )
    ensure_dir(target.parent)
    atomic_write(
        target,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
    )
    return target, payload


def _parse_json_object(
    raw_output: str,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    raw = (raw_output or "").strip()
    if not raw:
        return None, "empty output"
    candidate = raw
    if "```" in raw:
        start = raw.rfind("```")
        if start != -1:
            fence = raw.find("\n", start)
            if fence != -1:
                end = raw.rfind("```", fence)
                if end != -1 and end > fence:
                    candidate = raw[fence + 1 : end]
    if not (candidate.startswith("{") and candidate.endswith("}")):
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            candidate = raw[first : last + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc.msg}"
    if not isinstance(obj, dict):
        return None, "JSON payload is not an object"
    return obj, None


def _strip_completion_signal(raw_output: str, signal: Optional[str]) -> str:
    """Remove the trailing completion signal line so it isn't written to file."""

    if not signal:
        return raw_output
    # Remove the signal anywhere it appears as a line of its own.  The
    # mock adapter always puts it on its own line; real models may do
    # the same.
    lines = (raw_output or "").splitlines()
    kept = [ln for ln in lines if signal not in ln]
    return "\n".join(kept).rstrip()


# --------------------------------------------------------------------------- #
# Public parse()
# --------------------------------------------------------------------------- #


def parse(
    raw_output: str,
    produces: Sequence[ProduceSpec],
    *,
    project_root: Path,
    stage_id: str,
    placeholder_values: Optional[Mapping[str, Any]] = None,
    completion_signal: Optional[str] = None,
) -> ParseResult:
    """Persist ``raw_output`` according to the declared ``produces``.

    Args:
        raw_output: the model's response text.
        produces: the stage's :class:`ProduceSpec` records (one or more).
        project_root: file destination root.
        stage_id: used in error messages only.
        placeholder_values: values for the ``{{var}}`` placeholders in
            each ``produces[].path`` (e.g. ``num`` for batch items).
        completion_signal: when set, the matching marker is stripped
            from the body before writing so it never lands on disk.
    """

    placeholders = dict(placeholder_values or {})
    body_for_files = _strip_completion_signal(raw_output, completion_signal)
    result = ParseResult()

    for produce in produces:
        if produce.split:
            sub = _parse_split_produce(
                body_for_files,
                produce,
                project_root=project_root,
                stage_id=stage_id,
                placeholders=placeholders,
            )
            result.produces.append(sub)
            continue
        form = produce.form  # "text" | "json"
        if form == "json":
            target_rel = render_path_template(produce.path, placeholders)
            target = (Path(project_root) / target_rel).resolve()
            path, payload = _write_json_file(target, body_for_files, stage_id=stage_id)
            result.produces.append(
                ProduceParseResult(
                    alias=produce.alias,
                    form="json",
                    paths=[path],
                    parsed=payload,
                )
            )
        else:
            target_rel = render_path_template(produce.path, placeholders)
            target = (Path(project_root) / target_rel).resolve()
            path = _write_text_file(target, body_for_files)
            result.produces.append(
                ProduceParseResult(
                    alias=produce.alias,
                    form="text",
                    paths=[path],
                )
            )
    return result


def _parse_split_produce(
    raw_output: str,
    produce: ProduceSpec,
    *,
    project_root: Path,
    stage_id: str,
    placeholders: Mapping[str, Any],
) -> ProduceParseResult:
    """Apply the produce's split regex and write each match as its own file."""

    try:
        pattern = re.compile(produce.split or "", re.MULTILINE)
    except re.error as exc:
        raise OutputParseError(
            f"stage {stage_id!r}: split regex for alias {produce.alias!r} "
            f"is invalid: {exc}"
        ) from exc
    matches = list(pattern.finditer(raw_output or ""))
    if not matches:
        raise OutputParseError(
            f"stage {stage_id!r}: split regex for alias {produce.alias!r} "
            f"did not match the model output "
            f"(first 200 chars): {(raw_output or '')[:200]!r}"
        )
    written: list[Path] = []
    segments: list[dict[str, Any]] = []
    for idx, m in enumerate(matches, start=1):
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        if "num" not in groups:
            groups["num"] = idx
        merged = dict(placeholders)
        merged.update(groups)
        rendered_name = render_path_template(produce.path, merged)
        target = (Path(project_root) / rendered_name).resolve()
        start = m.end()
        end = matches[idx].start() if idx < len(matches) else len(raw_output)
        body = (raw_output or "")[start:end].strip()
        path = _write_text_file(target, body + "\n")
        written.append(path)
        segments.append(
            {
                "raw_text": body,
                "matches": groups,
                "path": path,
            }
        )
    return ProduceParseResult(
        alias=produce.alias,
        form="split",
        paths=written,
        segments=segments,
    )


__all__ = [
    "OutputForm",
    "ParseResult",
    "ProduceParseResult",
    "infer_form",
    "parse",
    "render_path_template",
]
