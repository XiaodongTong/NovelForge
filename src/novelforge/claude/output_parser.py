"""Output-form inference + parsing for v4 stages.

The v4 yaml makes ``output:`` a single string template, e.g.::

    output/review/chapter-review.json
    output/chapters/{{num:03d}}-{{title|slug}}.md
    output/summaries/plot.md

The engine needs to know **what to do** with the model's response:

- ``text``  — write the raw output verbatim to a single file.
- ``json``  — parse the response as JSON, validate, write the JSON to
  the file.
- ``split`` — split the response on a regex (one match per file) and
  render the file name from each match's capture groups.

Rules (per spec §5.4):

- Final suffix ``.json`` ⇒ ``json``.
- Contains ``{{x}}`` placeholder ⇒ ``split`` (must have ``split``
  regex).
- Otherwise ⇒ ``text``.
- ``.json`` + ``{{x}}`` is illegal (A15).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from ..errors import OutputParseError, SchemaInvalid
from ..utils.fs import atomic_write, ensure_dir
from ..utils.log import get_logger

log = get_logger("claude.output_parser")

OutputForm = Literal["text", "json", "split"]
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


# --------------------------------------------------------------------------- #
# Inference
# --------------------------------------------------------------------------- #


def infer_form(path_template: str) -> OutputForm:
    """Return the output form implied by the ``output:`` template.

    Raises:
        ValueError: when the template mixes ``.json`` suffix with
            ``{{x}}`` placeholders (spec A15).
    """

    if not isinstance(path_template, str) or not path_template.strip():
        raise ValueError("output template must be a non-empty string")
    ends_with_json = path_template.rstrip().endswith(".json")
    has_placeholder = PLACEHOLDER_RE.search(path_template) is not None
    if ends_with_json and has_placeholder:
        raise ValueError(
            f"output template {path_template!r} mixes .json suffix with "
            f"{{{{...}}}} placeholders; .json output is single-object only. "
            f"Either drop the placeholder (single JSON file) or change the "
            f"suffix (e.g. .md with split)."
        )
    if ends_with_json:
        return "json"
    if has_placeholder:
        return "split"
    return "text"


# --------------------------------------------------------------------------- #
# ParseResult
# --------------------------------------------------------------------------- #


@dataclass
class ParseResult:
    """Result of parsing a model's raw output.

    Field semantics depend on the form:

    - ``text``  → ``written_path`` set, ``text`` set.
    - ``json``  → ``written_path`` set, ``data`` set.
    - ``split`` → ``written_paths`` (>= 1) and ``segments`` (same
      length).  Each ``segments[i]`` carries the raw text + the
      regex match's capture groups under ``matches`` so the
      orchestrator can pull a ``route`` from any segment.

    On failure, callers should raise :class:`OutputParseError` or
    :class:`SchemaInvalid` instead of returning a partial ParseResult.
    """

    form: OutputForm
    written_path: Optional[Path] = None
    written_paths: list[Path] = field(default_factory=list)
    text: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    segments: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Render placeholders
# --------------------------------------------------------------------------- #


_FILTER_SLUG = re.compile(r"[^A-Za-z0-9\-_]+")


def _slugify(value: str, max_len: int = 60) -> str:
    s = _FILTER_SLUG.sub("-", value.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "untitled")[:max_len]


def _format_value(raw: str, value: Any) -> str:
    """Apply a single placeholder format spec to ``value``.

    Supported spec: ``:03d`` style integer padding, ``|slug`` slugify.
    Bare ``{{x}}`` uses ``str(value)``.
    """

    # Split off the filter (everything after ``|``).
    if "|" in raw:
        head, _, filt = raw.partition("|")
        head = head.strip()
        filt = filt.strip()
    else:
        head, filt = raw.strip(), ""
    if filt == "slug":
        return _slugify("" if value is None else str(value))
    # Numeric padding: the head looks like ``name:03d``.  Extract the
    # spec after the colon.
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
    values: Optional[dict[str, Any]] = None,
) -> str:
    """Substitute ``{{var}}`` / ``{{var:03d}}`` / ``{{var|slug}}`` markers.

    Unknown variables raise :class:`OutputParseError` so a typo in the
    yaml is loud rather than silent.
    """

    values = values or {}

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        # Look up the *name* (the part before ``:`` or ``|``).
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
    """Parse ``raw_output`` as JSON, validate, and write to ``target``."""

    payload, err = _parse_json_object(raw_output, stage_id=stage_id)
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
    *,
    stage_id: str,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Best-effort JSON object extraction.

    Mirrors the heuristics in :func:`novelforge.review.schema.parse_review_payload`
    (last fenced code block, last {…} block, full strip) so model
    output that includes prose around a JSON blob still parses.
    """

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


# --------------------------------------------------------------------------- #
# Public parse()
# --------------------------------------------------------------------------- #


def parse(
    raw_output: str,
    form: OutputForm,
    *,
    output_template: str,
    split_regex: Optional[str],
    project_root: Path,
    stage_id: str,
    placeholder_values: Optional[dict[str, Any]] = None,
) -> ParseResult:
    """Persist ``raw_output`` according to the inferred ``form``.

    Args:
        raw_output: the model's response text.
        form: one of ``"text"``, ``"json"``, ``"split"``.
        output_template: the v4 ``output:`` template (used to render
            the target path / paths).
        split_regex: required when ``form == "split"``.  Each match
            must contain a ``num`` capture group (used as the segment
            index) and may contain any other named groups; those are
            passed back in ``ParseResult.segments[i].matches`` so the
            caller can also render file names from them.
        project_root: file destination root.
        stage_id: used in error messages only.
        placeholder_values: values for the ``{{var}}`` placeholders in
            the output template.  ``num`` and ``title`` are the common
            ones for the split form.
    """

    if form == "text":
        rendered = render_path_template(output_template, placeholder_values or {})
        target = project_root / rendered
        path = _write_text_file(target, raw_output)
        return ParseResult(form="text", written_path=path, text=raw_output)
    if form == "json":
        rendered = render_path_template(output_template, placeholder_values or {})
        target = project_root / rendered
        path, data = _write_json_file(target, raw_output, stage_id=stage_id)
        return ParseResult(form="json", written_path=path, data=data)
    if form == "split":
        if not split_regex:
            raise OutputParseError(
                f"stage {stage_id!r}: output template {output_template!r} "
                f"contains placeholders but no `split` regex was given"
            )
        try:
            pattern = re.compile(split_regex, re.MULTILINE)
        except re.error as exc:
            raise OutputParseError(
                f"stage {stage_id!r}: split regex is invalid: {exc}"
            ) from exc
        matches = list(pattern.finditer(raw_output or ""))
        if not matches:
            raise OutputParseError(
                f"stage {stage_id!r}: split regex did not match the model "
                f"output (first 200 chars): "
                f"{(raw_output or '')[:200]!r}"
            )
        written: list[Path] = []
        segments: list[dict[str, Any]] = []
        # Default segment index → start counting at 1 when ``num`` is
        # absent so file names are still unique.
        for idx, m in enumerate(matches, start=1):
            groups = {k: v for k, v in m.groupdict().items() if v is not None}
            if "num" not in groups:
                groups["num"] = idx
            rendered_name = render_path_template(output_template, groups)
            target = project_root / rendered_name
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
        return ParseResult(
            form="split",
            written_paths=written,
            segments=segments,
        )
    raise OutputParseError(f"unknown output form: {form!r}")


__all__ = [
    "OutputForm",
    "ParseResult",
    "infer_form",
    "parse",
    "render_path_template",
]
