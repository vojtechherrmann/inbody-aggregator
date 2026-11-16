"""WHAT Claude extracts from an InBody result sheet -- a thin adapter over the extraction package
(`extraction/`, see its README): prompt `inbody_sheet/prompt.md`, wire schema
`inbody_sheet/schema.json`, normalization `validate.normalize()`.

Claude returns a SPARSE wire answer (one entry per printed number, its printed unit, the printed
text, labels, history strip, warnings) -- 0 optional / 0 nullable parameters, so strict
structured outputs compile. Deterministic code converts units and builds the canonical rows.

SCHEMA_VERSION (= the package VERSION) keys the cache of Claude answers (`upload.llm_json`):
bumping it makes the next reprocess call Claude again, under the spend limits.
"""

from __future__ import annotations

from sheetpkg import PROMPT, SHEET_PARSER_VERSION, WIRE_SCHEMA, validate

SCHEMA_VERSION = SHEET_PARSER_VERSION
SYSTEM_PROMPT = PROMPT
OUTPUT_SCHEMA = WIRE_SCHEMA
USER_INSTRUCTION = "Transcribe this InBody result sheet."

# Defensive checks (docs/upload-pipeline.md, SV). Layouts we have verified goldens for: the
# Czech LookinBody120 sheets and the US InBody-app sheet. Anything else -> needs_review.
KNOWN_LANGUAGES = {"cs", "en"}
# Model warnings that send the upload to review. `truncated_value` (e.g. impedance decimals cut
# off by a narrow column) and `not_printed` (e.g. no device model on the Gym B sheet) are
# expected on known sheets and recorded in `warnings` instead -- otherwise those sheets could
# never be accepted. Interpretation of "any model warning" (2026-11-16); confirm with the user.
REVIEW_WARNING_CODES = {"unreadable", "ambiguous", "inconsistent", "other"}
NON_BLOCKING_WARNING_CODES = {"truncated_value", "not_printed"}  # confirmed by the user 2026-11-16
# The exercise table (kcal per 30 min of an activity, derived from weight) is informational: it is
# stored but NEVER a reason for needs_review -- a warning about it does not block the upload.
NON_CRITICAL_FIELD_PREFIXES = ("exercise",)


def non_critical(field: str | None) -> bool:
    return bool(field) and field.startswith(NON_CRITICAL_FIELD_PREFIXES)


class SheetInvalid(Exception):
    """The answer does not fit the wire schema. Retryable at call time."""


class NotInBodySheet(Exception):
    """The model says the input is not an InBody result. Final: failed + CSV tip."""


def validate_wire(payload) -> dict:
    if not isinstance(payload, dict):
        raise SheetInvalid("invalid model response")
    errors = validate.validate_schema(payload, WIRE_SCHEMA)
    if errors:
        raise SheetInvalid("invalid model response: " + "; ".join(errors[:3]))
    if not payload["is_inbody_result_sheet"]:
        raise NotInBodySheet("this does not look like an InBody result sheet")
    return payload


def normalize(wire: dict) -> tuple[list[dict], list[str], list[str]]:
    """Wire -> (canonical rows [main, *history], problems, wire-level errors). The rows are
    exactly `validate.normalize()` (the golden *.canonical.json); nothing is stamped here."""
    validate_wire(wire)
    rows, problems = validate.normalize(wire)
    wire_errors, _ = validate.sanity_wire(wire)
    return rows, problems, wire_errors
