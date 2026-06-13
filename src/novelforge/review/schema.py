"""JSON schema for review gate output.

The model must return exactly this structure (modulo extra keys) for
the review stage.  Any deviation is treated as ``FUNDAMENTAL_ISSUE`` by
the gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import jsonschema
from jsonschema import Draft202012Validator

REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["passed", "route"],
    "properties": {
        "passed": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
        },
        "required_changes": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
        },
        "route": {
            "type": "string",
            "enum": ["APPROVED", "NEEDS_REWRITE", "FUNDAMENTAL_ISSUE"],
        },
        "summary": {"type": "string"},
        "scores": {
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
    },
}


def get_validator() -> Draft202012Validator:
    """Return a compiled validator for the review schema."""

    return Draft202012Validator(REVIEW_OUTPUT_SCHEMA)


def validate_review_payload(payload: Any) -> list[str]:
    """Return a list of validation errors.  Empty list means OK.

    Used by both the gate and the test suite.
    """

    validator = get_validator()
    return [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in validator.iter_errors(payload)
    ]


def parse_review_payload(raw: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Best-effort JSON parse + schema validation.

    Returns ``(parsed_dict, error_message)``.  On success, ``error_message``
    is ``None``; on failure it describes the problem.
    """

    raw = (raw or "").strip()
    if not raw:
        return None, "empty output"
    # Try a few patterns: pure JSON, last ```json``` block, last { ... } block.
    candidate = raw
    if "```" in raw:
        # Find the last fenced code block.
        start = raw.rfind("```")
        if start != -1:
            fence = raw.find("\n", start)
            if fence != -1:
                end = raw.rfind("```", fence)
                if end != -1 and end > fence:
                    candidate = raw[fence + 1 : end]
    # Try to extract a JSON object from the text by finding the matching braces.
    if not (candidate.startswith("{") and candidate.endswith("}")):
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            candidate = raw[first : last + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc.msg}"
    errors = validate_review_payload(obj)
    if errors:
        return None, "; ".join(errors)
    if not isinstance(obj, dict):
        return None, "review payload is not a JSON object"
    return obj, None
