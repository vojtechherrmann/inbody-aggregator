#!/usr/bin/env python3
# ruff: noqa: E501 -- long human-readable diagnostics and schema descriptions
"""Offline validator for InBody result-sheet extractions. Stdlib only, NO network, NO API calls.

The field REGISTRY below is the single source of truth. Both JSON schemas are generated from it:

* `inbody_sheet/schema.json`             -- the WIRE schema Claude fills (`output_config.format`).
  Sparse: one `observations[]` entry per printed number, so it has 0 optional and 0 union-typed
  parameters (strict structured outputs allow at most 24 optional / 16 union parameters, so a
  flat 200-field nullable object cannot compile).
* `inbody_sheet/measurement.schema.json` -- the CANONICAL flat measurement (SI units, every field
  nullable except `measured_date` + `weight_kg`). One row per measurement; what the SQL
  `extraction` / `measurement` tables store; what the CSV parser emits.

Commands (run from the repo root):

  .venv/bin/python extraction/validate.py check RESPONSE.json [--golden GOLDEN.json]
      wire schema + sanity rules on a model response, then a field-by-field diff to the golden
      (default golden: samples/expected/<RESPONSE basename>.json when it exists).
  .venv/bin/python extraction/validate.py normalize RESPONSE.json      -> canonical rows (stdout)
  .venv/bin/python extraction/validate.py csv EXPORT.csv               -> canonical rows (stdout)
  .venv/bin/python extraction/validate.py emit-schema                  regenerate both schemas
  .venv/bin/python extraction/validate.py selftest                     everything in samples/expected
  .venv/bin/pytest extraction/validate.py                              same, as pytest tests

`normalize()`, `sanity()`, `validate_schema()` and `CSV_COLUMNS` are importable by the app.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE = HERE / "inbody_sheet"
REPO = HERE.parent
EXPECTED = REPO / "samples" / "expected"
VERSION = (PACKAGE / "VERSION").read_text().strip() if (PACKAGE / "VERSION").exists() else "0"
SHEET_PARSER_VERSION = f"inbody_sheet/{VERSION}"
CSV_PARSER_VERSION = f"inbody_app_csv/{VERSION}"

# ------------------------------------------------------------------------------ units & kinds

LB_TO_KG = 0.45359237
IN_TO_CM = 2.54
KJ_TO_KCAL = 1 / 4.184

# kind -> (canonical unit, {accepted source unit: factor to canonical}, decimals after conversion)
KINDS: dict[str, tuple[str, dict[str, float], int | None]] = {
    "mass": ("kg", {"kg": 1.0, "lb": LB_TO_KG}, 3),
    "water": ("L", {"L": 1.0, "kg": 1.0, "lb": LB_TO_KG}, 3),  # 1 kg of body water ~ 1 L
    "length": ("cm", {"cm": 1.0, "in": IN_TO_CM}, 2),
    "energy": ("kcal", {"kcal": 1.0, "kJ": KJ_TO_KCAL}, 0),
    "area": ("cm2", {"cm2": 1.0, "in2": IN_TO_CM**2}, 2),
    "percent": ("percent", {"percent": 1.0}, None),
    "ratio": ("ratio", {"ratio": 1.0}, None),
    "kg_m2": ("kg/m2", {"kg/m2": 1.0}, None),
    "ohm": ("ohm", {"ohm": 1.0}, None),
    "level": ("level", {"level": 1.0}, None),
    "points": ("points", {"points": 1.0}, None),
    "deg": ("deg", {"deg": 1.0}, None),
    "years": ("years", {"years": 1.0}, None),
    "code": ("code", {"code": 1.0}, None),
}
WIRE_UNITS = sorted({u for _, units, _ in KINDS.values() for u in units})

SEGMENTS = ("right_arm", "left_arm", "trunk", "right_leg", "left_leg")
FREQS_KHZ = (1, 5, 20, 50, 100, 250, 500, 1000)

# ------------------------------------------------------------------------------ field registry
# numeric field -> (kind, sanity_low, sanity_high). Sanity bounds are in CANONICAL units and are
# deliberately wide: they catch misreads (decimal comma lost, lb taken as kg), not unhealthy values.

NUMERIC: dict[str, tuple[str, float, float]] = {
    "subject_height_cm": ("length", 100, 230),
    "subject_age_years": ("years", 5, 110),
    "weight_kg": ("mass", 20, 300),
    "total_body_water_l": ("water", 10, 120),
    "intracellular_water_l": ("water", 5, 80),
    "extracellular_water_l": ("water", 3, 50),
    "ecw_tbw_ratio": ("ratio", 0.30, 0.45),
    "protein_kg": ("mass", 2, 40),
    "minerals_kg": ("mass", 0.5, 10),
    "bone_mineral_content_kg": ("mass", 0.5, 8),
    "dry_lean_mass_kg": ("mass", 3, 50),
    "body_fat_mass_kg": ("mass", 1, 200),
    "fat_free_mass_kg": ("mass", 15, 150),
    "soft_lean_mass_kg": ("mass", 15, 140),
    "skeletal_muscle_mass_kg": ("mass", 5, 80),
    "body_cell_mass_kg": ("mass", 5, 100),
    "bmi": ("kg_m2", 10, 70),
    "percent_body_fat": ("percent", 2, 75),
    "smi_kg_m2": ("kg_m2", 3, 15),
    "waist_hip_ratio": ("ratio", 0.5, 1.5),
    "waist_circumference_cm": ("length", 40, 200),
    "visceral_fat_level": ("level", 1, 30),
    "visceral_fat_area_cm2": ("area", 5, 400),
    "bmr_kcal": ("energy", 700, 4000),
    "recommended_intake_kcal": ("energy", 800, 6000),
    "obesity_degree_pct": ("percent", 50, 300),
    "inbody_score": ("points", 0, 120),
    "phase_angle_deg": ("deg", 2, 15),
    "leg_muscle_level": ("level", 0, 10),
    "leg_lean_mass_kg": ("mass", 5, 60),
    "target_weight_kg": ("mass", 20, 300),
    "weight_control_kg": ("mass", -150, 150),
    "fat_control_kg": ("mass", -150, 150),
    "muscle_control_kg": ("mass", -50, 50),
    "lbm_control_kg": ("mass", -50, 50),
    "balance_upper": ("code", 0, 9),
    "balance_lower": ("code", 0, 9),
    "balance_upper_lower": ("code", 0, 9),
}
RANGED = (
    "weight_kg",
    "total_body_water_l",
    "intracellular_water_l",
    "extracellular_water_l",
    "protein_kg",
    "minerals_kg",
    "body_fat_mass_kg",
    "fat_free_mass_kg",
    "soft_lean_mass_kg",
    "skeletal_muscle_mass_kg",
    "bmr_kcal",
    "obesity_degree_pct",
)
for _f in RANGED:
    _kind, _lo, _hi = NUMERIC[_f]
    NUMERIC[f"{_f}_low"] = (_kind, _lo * 0.3 if _lo > 0 else _lo, _hi)
    NUMERIC[f"{_f}_high"] = (_kind, _lo * 0.3 if _lo > 0 else _lo, _hi)
for _s in SEGMENTS:
    _lean = (5, 60) if _s == "trunk" else ((2, 30) if _s.endswith("leg") else (0.5, 15))
    NUMERIC[f"seg_lean_{_s}_kg"] = ("mass", *_lean)
    NUMERIC[f"seg_lean_{_s}_pct"] = ("percent", 30, 250)
    NUMERIC[f"seg_fat_{_s}_kg"] = ("mass", 0, 80)
    NUMERIC[f"seg_fat_{_s}_pct"] = ("percent", 10, 1000)
    NUMERIC[f"seg_ecw_ratio_{_s}"] = ("ratio", 0.30, 0.45)
for _f in FREQS_KHZ:
    for _s in SEGMENTS:
        NUMERIC[f"z_{_f}khz_{_s}_ohm"] = ("ohm", *((3, 80) if _s == "trunk" else (50, 1000)))

EVAL3 = ["under", "normal", "over"]
EVAL4 = ["under", "normal", "slightly_over", "over"]
CATEGORICAL: dict[str, list[str]] = {
    "subject_sex": ["male", "female"],
    "bmi_eval": EVAL4,
    "pbf_eval": EVAL4,
}
for _s in SEGMENTS:
    CATEGORICAL[f"seg_lean_{_s}_eval"] = EVAL3
    CATEGORICAL[f"seg_fat_{_s}_eval"] = EVAL3
CATEGORY_VALUES = ["male", "female", "under", "normal", "slightly_over", "over"]

HISTORY_FIELDS = (
    "weight_kg",
    "skeletal_muscle_mass_kg",
    "percent_body_fat",
    "body_fat_mass_kg",
    "ecw_tbw_ratio",
)

# canonical metadata columns (all nullable except the first; JSON columns marked)
META: dict[str, dict] = {
    "measured_date": {
        "type": "string",
        "description": "Date of the test as printed/exported, 'YYYY-MM-DD'. The app keeps ONE measurement per user per day.",
    },
    "measured_time": {
        "type": ["string", "null"],
        "description": "Time as printed/exported, 'HH:MM' or 'HH:MM:SS', no timezone. Informational only: never used for matching (the InBody app sometimes drops PM).",
    },
    "origin": {"enum": ["sheet", "sheet_history", "csv"]},
    "from_history": {"type": "boolean"},
    "parser_version": {"type": "string"},
    "source_name": {
        "type": ["string", "null"],
        "description": "Gym / facility / app name. Never contact details.",
    },
    "device_model": {"type": ["string", "null"]},
    "device_serial": {"type": ["string", "null"]},
    "software_version": {"type": ["string", "null"]},
    "sheet_template": {
        "type": ["string", "null"],
        "description": "Footer template code, e.g. BR-Czech-F3/230-B-140703. NOT the device model.",
    },
    "sheet_language": {"type": ["string", "null"]},
    "source_unit_system": {"type": ["string", "null"], "enum": ["metric", "us", "mixed", None]},
}
JSON_COLUMNS = ("exercise_kcal_30min", "converted_from", "warnings")

DATE_ORDERS = ["DMY", "YMD", "MDY", "unknown"]
CAPTURES = ["digital_render", "app_screenshot", "photo", "scan", "pdf", "unknown"]
UNIT_SYSTEMS = ["metric", "us", "mixed", "unknown"]
WARNING_CODES = [
    "unreadable",
    "truncated_value",
    "ambiguous",
    "inconsistent",
    "not_printed",
    "other",
]
MEASURED_AT_RE = r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})?$"

# ------------------------------------------------------------------------------ schema generation


def _obj(props: dict, description: str | None = None) -> dict:
    out = {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }
    if description:
        out["description"] = description
    return out


def wire_schema() -> dict:
    """What Claude returns. Every property required, no nullable unions (see module docstring)."""
    observation = _obj(
        {
            "field": {"type": "string", "enum": sorted(NUMERIC)},
            "value": {
                "type": "number",
                "description": "The number as printed, in `unit` (decimal comma -> dot, sign kept). No conversion.",
            },
            "unit": {"type": "string", "enum": WIRE_UNITS},
            "printed": {
                "type": "string",
                "description": "The value exactly as printed, including its unit if printed next to it.",
            },
        }
    )
    label = _obj(
        {
            "field": {"type": "string", "enum": sorted(CATEGORICAL)},
            "value": {"type": "string", "enum": CATEGORY_VALUES},
            "printed": {"type": "string"},
        }
    )
    history_point = _obj(
        {
            "measured_at_local": {"type": "string", "pattern": MEASURED_AT_RE},
            "printed_date": {"type": "string"},
            "is_current_test": {"type": "boolean"},
            "values": {"type": "array", "items": {"$ref": "#/$defs/observation"}},
        }
    )
    sheet = _obj(
        {
            "measured_at_local": {"type": "string", "pattern": MEASURED_AT_RE},
            "measured_at_printed": {"type": "string"},
            "date_order": {"type": "string", "enum": DATE_ORDERS},
            "language": {
                "type": "string",
                "description": "BCP-47 code of the label language, e.g. cs, en.",
            },
            "unit_system": {"type": "string", "enum": UNIT_SYSTEMS},
            "device_model": {
                "type": "string",
                "description": "Only if printed as the device, e.g. [InBody270]. Empty string if not printed.",
            },
            "device_serial": {"type": "string"},
            "software_version": {"type": "string"},
            "sheet_template": {"type": "string"},
            "source_name": {"type": "string"},
            "capture": {"type": "string", "enum": CAPTURES},
        }
    )
    exercise = _obj(
        {
            "activity": {"type": "string"},
            "printed_label": {"type": "string"},
            "kcal": {"type": "number"},
        }
    )
    warning = _obj(
        {
            "code": {"type": "string", "enum": WARNING_CODES},
            "field": {
                "type": "string",
                "description": "Field id, or empty string for the whole sheet.",
            },
            "note": {"type": "string"},
        }
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"InBody result sheet extraction (wire format) {VERSION}",
        "description": "GENERATED by extraction/validate.py emit-schema from its REGISTRY. Do not edit by hand.",
        "type": "object",
        "properties": {
            "is_inbody_result_sheet": {"type": "boolean"},
            "sheet": {"$ref": "#/$defs/sheet"},
            "observations": {"type": "array", "items": {"$ref": "#/$defs/observation"}},
            "labels": {"type": "array", "items": {"$ref": "#/$defs/label"}},
            "exercise": {"type": "array", "items": {"$ref": "#/$defs/exercise"}},
            "history": {"type": "array", "items": {"$ref": "#/$defs/history_point"}},
            "warnings": {"type": "array", "items": {"$ref": "#/$defs/warning"}},
        },
        "required": [
            "is_inbody_result_sheet",
            "sheet",
            "observations",
            "labels",
            "exercise",
            "history",
            "warnings",
        ],
        "additionalProperties": False,
        "$defs": {
            "sheet": sheet,
            "observation": observation,
            "label": label,
            "exercise": exercise,
            "history_point": history_point,
            "warning": warning,
        },
    }


def canonical_schema() -> dict:
    props: dict[str, dict] = {}
    for name, spec in META.items():
        props[name] = dict(spec)
    for name, (kind, lo, hi) in NUMERIC.items():
        unit = KINDS[kind][0]
        props[name] = {"type": ["number", "null"], "description": f"{unit}; sanity {lo}..{hi}"}
    props["weight_kg"]["type"] = "number"
    for name, values in CATEGORICAL.items():
        props[name] = {"enum": [*values, None]}
    props["exercise_kcal_30min"] = {
        "type": ["array", "null"],
        "items": _obj(
            {
                "activity": {"type": "string"},
                "printed_label": {"type": "string"},
                "kcal": {"type": "number"},
            }
        ),
    }
    props["converted_from"] = {
        "type": "array",
        "description": "Source value + unit for every field that was converted to SI.",
        "items": _obj(
            {"field": {"type": "string"}, "value": {"type": "number"}, "unit": {"type": "string"}}
        ),
    }
    props["warnings"] = {
        "type": "array",
        "items": _obj(
            {"code": {"type": "string"}, "field": {"type": "string"}, "note": {"type": "string"}}
        ),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"Canonical InBody measurement {VERSION}",
        "description": "GENERATED by extraction/validate.py emit-schema. One row per measurement; SI units; "
        "all fields nullable except measured_date and weight_kg. Column names are SQL-safe.",
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def emit_schema() -> None:
    for name, schema in (
        ("schema.json", wire_schema()),
        ("measurement.schema.json", canonical_schema()),
    ):
        (PACKAGE / name).write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {PACKAGE / name}")


# ------------------------------------------------------------------------------ mini JSON-schema validator
# Covers exactly the subset used above (type, enum, const, properties, required,
# additionalProperties:false, items, $ref, pattern). Avoids a jsonschema dependency.


def validate_schema(instance, schema: dict, root: dict | None = None, path: str = "$") -> list[str]:
    root = root or schema
    errs: list[str] = []
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return validate_schema(instance, target, root, path)
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        ok = any(_is_type(instance, t) for t in types)
        if not ok:
            return [f"{path}: expected {types}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errs.append(f"{path}: {instance!r} not in enum")
    if "const" in schema and instance != schema["const"]:
        errs.append(f"{path}: {instance!r} != const")
    if (
        "pattern" in schema
        and isinstance(instance, str)
        and not re.search(schema["pattern"], instance)
    ):
        errs.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, dict) and "properties" in schema:
        for key in schema.get("required", []):
            if key not in instance:
                errs.append(f"{path}: missing {key}")
        for key, value in instance.items():
            if key in schema["properties"]:
                errs += validate_schema(value, schema["properties"][key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected property {key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errs += validate_schema(item, schema["items"], root, f"{path}[{i}]")
    return errs


def _is_type(v, t: str) -> bool:
    return {
        "object": isinstance(v, dict),
        "array": isinstance(v, list),
        "string": isinstance(v, str),
        "boolean": isinstance(v, bool),
        "null": v is None,
        "integer": isinstance(v, int) and not isinstance(v, bool),
        "number": isinstance(v, (int, float)) and not isinstance(v, bool),
    }[t]


# ------------------------------------------------------------------------------ normalization


def to_canonical(field: str, value: float, unit: str) -> tuple[float, bool]:
    """(canonical value, converted?) -- raises ValueError on a unit the field does not accept."""
    kind = NUMERIC[field][0]
    canon_unit, factors, decimals = KINDS[kind]
    if unit not in factors:
        raise ValueError(f"{field}: unit {unit!r} not accepted (expected one of {sorted(factors)})")
    if unit == canon_unit:
        return value, False
    out = value * factors[unit]
    if decimals is not None:
        out = round(out, decimals)
        if decimals == 0:
            out = int(out)
    return out, True


def empty_row() -> dict:
    row: dict = {k: None for k in META}
    row.update({k: None for k in NUMERIC})
    row.update({k: None for k in CATEGORICAL})
    row.update({"exercise_kcal_30min": None, "converted_from": [], "warnings": []})
    return row


def _apply_observations(row: dict, observations: list[dict], problems: list[str]) -> None:
    seen = set()
    for ob in observations:
        f = ob["field"]
        if f in seen:
            problems.append(f"duplicate observation for {f}")
            continue
        seen.add(f)
        try:
            value, converted = to_canonical(f, ob["value"], ob["unit"])
        except ValueError as exc:
            problems.append(str(exc))
            continue
        row[f] = value
        if converted:
            row["converted_from"].append({"field": f, "value": ob["value"], "unit": ob["unit"]})


def normalize(wire: dict) -> tuple[list[dict], list[str]]:
    """Wire response -> canonical rows: [main measurement, *history points]. Plus problems found.

    The history point flagged is_current_test is NOT emitted (it duplicates the main row); it is
    used by sanity() to confirm the history date order instead."""
    problems: list[str] = []
    if not wire.get("is_inbody_result_sheet"):
        return [], ["not an InBody result sheet"]
    sheet = wire["sheet"]
    meta = {
        "parser_version": SHEET_PARSER_VERSION,
        "source_name": sheet["source_name"] or None,
        "device_model": sheet["device_model"] or None,
        "device_serial": sheet["device_serial"] or None,
        "software_version": sheet["software_version"] or None,
        "sheet_template": sheet["sheet_template"] or None,
        "sheet_language": sheet["language"] or None,
        "source_unit_system": sheet["unit_system"] if sheet["unit_system"] != "unknown" else None,
    }
    main = empty_row()
    main.update(meta)
    main.update(
        {**split_local(sheet["measured_at_local"]), "origin": "sheet", "from_history": False}
    )
    _apply_observations(main, wire["observations"], problems)
    seen_labels = set()
    for lab in wire["labels"]:
        if lab["field"] in seen_labels:
            problems.append(f"duplicate label for {lab['field']}")
            continue
        seen_labels.add(lab["field"])
        if lab["value"] not in CATEGORICAL[lab["field"]]:
            problems.append(
                f"{lab['field']}: {lab['value']!r} not allowed (allowed {CATEGORICAL[lab['field']]})"
            )
            continue
        main[lab["field"]] = lab["value"]
    if wire["exercise"]:
        main["exercise_kcal_30min"] = [dict(e) for e in wire["exercise"]]
    main["warnings"] = [dict(w) for w in wire["warnings"]]
    rows = [main]
    for point in wire["history"]:
        bad = [v["field"] for v in point["values"] if v["field"] not in HISTORY_FIELDS]
        if bad:
            problems.append(f"history {point['measured_at_local']}: unexpected fields {bad}")
        if point["is_current_test"]:
            continue
        h = empty_row()
        h.update({k: meta[k] for k in ("parser_version", "sheet_language", "source_unit_system")})
        h.update(
            {
                **split_local(point["measured_at_local"]),
                "origin": "sheet_history",
                "from_history": True,
            }
        )
        _apply_observations(
            h, [v for v in point["values"] if v["field"] in HISTORY_FIELDS], problems
        )
        rows.append(h)
    return rows, problems


# ------------------------------------------------------------------------------ sanity rules


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol + 1e-9


def split_local(s: str) -> dict:
    """Wire `YYYY-MM-DDTHH:MM[:SS]` -> canonical {measured_date, measured_time} (no timezone)."""
    if not s:
        return {"measured_date": None, "measured_time": None}
    date, _, time = s.partition("T")
    return {"measured_date": date, "measured_time": time or None}


def row_local(row: dict) -> str:
    """Canonical row -> `YYYY-MM-DDTHH:MM[:SS]` (for display / keys in this tool only)."""
    if not row.get("measured_date"):
        return ""
    return row["measured_date"] + (
        f"T{row['measured_time']}" if row.get("measured_time") else "T00:00"
    )


def parse_local(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except (TypeError, ValueError):
            pass
    return None


def sanity(row: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for one canonical row. Errors = do not store without review."""
    err: list[str] = []
    warn: list[str] = []
    g = row.get
    t = parse_local(row_local(row))
    if t is None:
        err.append(f"measured_date/time unparseable: {g('measured_date')!r} {g('measured_time')!r}")
    elif not (datetime(1996, 1, 1) <= t <= datetime.now().replace(year=datetime.now().year + 1)):
        err.append(f"measured_date implausible: {t}")
    if g("weight_kg") is None:
        err.append("weight_kg missing")
    for name, (_, lo, hi) in NUMERIC.items():
        v = g(name)
        if v is not None and not (lo <= v <= hi):
            err.append(f"{name}={v} outside sanity range {lo}..{hi}")
    w = g("weight_kg")
    if w is None:
        return err, warn
    # weight = sum of components (InBody prints the table as "sum of the above")
    tbw, prot, mins, bfm, dlm = (
        g(k)
        for k in (
            "total_body_water_l",
            "protein_kg",
            "minerals_kg",
            "body_fat_mass_kg",
            "dry_lean_mass_kg",
        )
    )
    tol = max(0.3, 0.005 * w)
    if None not in (tbw, prot, mins, bfm) and not _close(w, tbw + prot + mins + bfm, tol):
        err.append(
            f"weight {w} != water+protein+minerals+fat {tbw + prot + mins + bfm:.2f} (tol {tol:.2f})"
        )
    if None not in (tbw, dlm, bfm) and not _close(w, tbw + dlm + bfm, tol):
        err.append(f"weight {w} != water+dry lean+fat {tbw + dlm + bfm:.2f} (tol {tol:.2f})")
    ffm = g("fat_free_mass_kg")
    if None not in (ffm, bfm) and not _close(w, ffm + bfm, 0.25):
        err.append(f"weight {w} != fat-free {ffm} + fat {bfm}")
    pbf = g("percent_body_fat")
    if None not in (pbf, bfm) and not _close(pbf, 100 * bfm / w, 0.3):
        err.append(f"PBF {pbf} != 100*fat/weight {100 * bfm / w:.2f}")
    h = g("subject_height_cm")
    bmi = g("bmi")
    if None not in (h, bmi) and not _close(bmi, w / (h / 100) ** 2, 0.3):
        err.append(f"BMI {bmi} != weight/height^2 {w / (h / 100) ** 2:.2f}")
    if None not in (bmi, g("obesity_degree_pct")) and not _close(
        g("obesity_degree_pct"), 100 * bmi / 22, 1.5
    ):
        warn.append(f"obesity degree {g('obesity_degree_pct')} != 100*BMI/22 {100 * bmi / 22:.1f}")
    if None not in (ffm, g("bmr_kcal")) and not _close(g("bmr_kcal"), 370 + 21.6 * ffm, 5):
        warn.append(
            f"BMR {g('bmr_kcal')} != 370+21.6*FFM {370 + 21.6 * ffm:.0f} (Katch-McArdle, as InBody)"
        )
    tw, wc = g("target_weight_kg"), g("weight_control_kg")
    if None not in (tw, wc) and not _close(tw, w + wc, 0.15):
        err.append(f"target weight {tw} != weight + weight control {w + wc:.2f}")
    fc, mc = g("fat_control_kg"), g("muscle_control_kg")
    if None not in (wc, fc, mc) and not _close(wc, fc + mc, 0.15):
        err.append(f"weight control {wc} != fat control + muscle control {fc + mc:.2f}")
    limbs = [g(f"seg_lean_{s}_kg") for s in ("right_arm", "left_arm", "right_leg", "left_leg")]
    if (
        None not in limbs
        and None not in (h, g("smi_kg_m2"))
        and not _close(g("smi_kg_m2"), sum(limbs) / (h / 100) ** 2, 0.15)
    ):
        warn.append(
            f"SMI {g('smi_kg_m2')} != appendicular lean/height^2 {sum(limbs) / (h / 100) ** 2:.2f}"
        )
    seg = [g(f"seg_lean_{s}_kg") for s in SEGMENTS]
    lean_ref = ffm if ffm is not None else (w - bfm if bfm is not None else None)
    if None not in seg and lean_ref and not (0.75 <= sum(seg) / lean_ref <= 0.95):
        warn.append(
            f"segmental lean sum {sum(seg):.2f} is {sum(seg) / lean_ref:.2f} of fat-free mass (expected 0.75..0.95)"
        )
    for s in SEGMENTS:
        lo_f, hi_f = g(f"z_100khz_{s}_ohm"), g(f"z_20khz_{s}_ohm")
        if None not in (lo_f, hi_f) and not lo_f < hi_f:
            err.append(f"impedance {s}: 100 kHz {lo_f} not below 20 kHz {hi_f}")
    for f in RANGED:
        lo, hi = g(f"{f}_low"), g(f"{f}_high")
        if None not in (lo, hi) and not lo < hi:
            err.append(f"range {f}: low {lo} >= high {hi}")
    return err, warn


def sanity_wire(wire: dict) -> tuple[list[str], list[str]]:
    """Cross-row checks that need the wire form (history strip)."""
    err: list[str] = []
    warn: list[str] = []
    if not wire.get("is_inbody_result_sheet"):
        return err, warn
    main_t = wire["sheet"]["measured_at_local"]
    hist = wire["history"]
    times = [p["measured_at_local"] for p in hist]
    if times != sorted(times) or len(set(times)) != len(times):
        err.append(f"history dates not strictly ascending: {times} (wrong yy/dd order?)")
    current = [p for p in hist if p["is_current_test"]]
    if hist and len(current) != 1:
        err.append(f"history: expected exactly one is_current_test point, got {len(current)}")
    if current:
        if current[0]["measured_at_local"] != main_t:
            err.append(
                f"history current point {current[0]['measured_at_local']} != sheet date {main_t} (date order misread)"
            )
        main_vals = {o["field"]: (o["value"], o["unit"]) for o in wire["observations"]}
        for v in current[0]["values"]:
            if v["field"] in main_vals and (
                abs(main_vals[v["field"]][0] - v["value"]) > 0.05
                or main_vals[v["field"]][1] != v["unit"]
            ):
                err.append(
                    f"history current point {v['field']}={v['value']} != main {main_vals[v['field']]}"
                )
    if any(t > main_t for t in times):
        err.append("history point later than the sheet's own test")
    return err, warn


# ------------------------------------------------------------------------------ CSV (InBody app export)
# Reference mapping for the deterministic parser the APP owns (src/web/csv_parsers.py). Header
# names are matched exactly; units live in the header names. factor None = identity.

CSV_COLUMNS: dict[str, tuple[str | None, str | None]] = {
    # header: (canonical field, source unit as wire unit) ; field None = metadata handled separately
    "date": (None, None),
    "Measurement device.": (None, None),
    "Weight(lb)": ("weight_kg", "lb"),
    "Skeletal Muscle Mass(lb)": ("skeletal_muscle_mass_kg", "lb"),
    "Soft Lean Mass(lb)": ("soft_lean_mass_kg", "lb"),
    "Body Fat Mass(lb)": ("body_fat_mass_kg", "lb"),
    "BMI(kg/m²)": ("bmi", "kg/m2"),
    "Percent Body Fat(%)": ("percent_body_fat", "percent"),
    "Basal Metabolic Rate(kJ)": ("bmr_kcal", "kJ"),
    "InBody Score": ("inbody_score", "points"),
    "Right Arm Lean Mass(lb)": ("seg_lean_right_arm_kg", "lb"),
    "Left Arm Lean Mass(lb)": ("seg_lean_left_arm_kg", "lb"),
    "Trunk Lean Mass(lb)": ("seg_lean_trunk_kg", "lb"),
    "Right Leg Lean Mass(lb)": ("seg_lean_right_leg_kg", "lb"),
    "Left leg Lean Mass(lb)": ("seg_lean_left_leg_kg", "lb"),  # sic: lower-case "leg" in the export
    "Right Arm Fat Mass(lb)": ("seg_fat_right_arm_kg", "lb"),
    "Left Arm Fat Mass(lb)": ("seg_fat_left_arm_kg", "lb"),
    "Trunk Fat Mass(lb)": ("seg_fat_trunk_kg", "lb"),
    "Right Leg Fat Mass(lb)": ("seg_fat_right_leg_kg", "lb"),
    "Left Leg Fat Mass(lb)": ("seg_fat_left_leg_kg", "lb"),
    "Right Arm ECW Ratio": ("seg_ecw_ratio_right_arm", "ratio"),
    "Left Arm ECW Ratio": ("seg_ecw_ratio_left_arm", "ratio"),
    "Trunk ECW Ratio": ("seg_ecw_ratio_trunk", "ratio"),
    "Right Leg ECW Ratio": ("seg_ecw_ratio_right_leg", "ratio"),
    "Left Leg ECW Ratio": ("seg_ecw_ratio_left_leg", "ratio"),
    "Waist Hip Ratio": ("waist_hip_ratio", "ratio"),
    "Waist Circumference(inch)": ("waist_circumference_cm", "in"),
    "Visceral Fat Area(cm²)": ("visceral_fat_area_cm2", "cm2"),
    "Visceral Fat Level(Level)": ("visceral_fat_level", "level"),
    "Total Body Water(L)": ("total_body_water_l", "L"),
    "Intracellular Water(L)": ("intracellular_water_l", "L"),
    "Extracellular Water(L)": ("extracellular_water_l", "L"),
    "ECW Ratio": ("ecw_tbw_ratio", "ratio"),
    "Upper-Lower": ("balance_upper_lower", "code"),
    "Upper": ("balance_upper", "code"),
    "Lower": ("balance_lower", "code"),
    "Leg Muscle Level(Level)": ("leg_muscle_level", "level"),
    "Leg Lean Mass(lb)": ("leg_lean_mass_kg", "lb"),
    "Protein(lb)": ("protein_kg", "lb"),
    "Mineral(lb)": ("minerals_kg", "lb"),
    "Bone Mineral Content(lb)": ("bone_mineral_content_kg", "lb"),
    "Body Cell Mass(lb)": ("body_cell_mass_kg", "lb"),
    "SMI(kg/m²)": ("smi_kg_m2", "kg/m2"),
    "Whole Body Phase Angle(°)": ("phase_angle_deg", "deg"),
}


def parse_csv(text: str) -> list[dict]:
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    unknown = [h for h in reader.fieldnames or [] if h not in CSV_COLUMNS]
    if unknown:
        raise ValueError(f"unknown CSV columns: {unknown}")
    rows = []
    for rec in reader:
        row = empty_row()
        stamp = datetime.strptime(rec["date"], "%Y%m%d%H%M%S")
        device = (rec.get("Measurement device.") or "").strip()
        row.update(
            {
                "measured_date": stamp.strftime("%Y-%m-%d"),
                "measured_time": stamp.strftime("%H:%M:%S"),
                "origin": "csv",
                "from_history": False,
                "parser_version": CSV_PARSER_VERSION,
                "source_name": "InBody app export",
                "device_model": f"InBody{device}" if device and device != "-" else None,
                "source_unit_system": "us" if any("(lb)" in h for h in rec) else "metric",
            }
        )
        observations = []
        for header, raw in rec.items():
            field, unit = CSV_COLUMNS[header]
            if field is None or raw is None or raw.strip() in ("", "-"):
                continue
            num = float(raw)
            if unit == "code":
                num = int(num)
            observations.append({"field": field, "value": num, "unit": unit})
        problems: list[str] = []
        _apply_observations(row, observations, problems)
        if problems:
            raise ValueError(f"{rec['date']}: {problems}")
        rows.append(row)
    return rows


# ------------------------------------------------------------------------------ diff vs golden


def _norm_str(s: str) -> str:
    return " ".join(s.split()).casefold()


def diff_wire(got: dict, want: dict) -> tuple[list[str], list[str]]:
    """(mismatches, notes). Mismatches fail the check; notes (printed-string / warnings) don't."""
    bad: list[str] = []
    notes: list[str] = []
    if got["is_inbody_result_sheet"] != want["is_inbody_result_sheet"]:
        return [
            f"is_inbody_result_sheet {got['is_inbody_result_sheet']} != {want['is_inbody_result_sheet']}"
        ], notes
    for k, v in want["sheet"].items():
        g = got["sheet"].get(k)
        if k in ("measured_at_local", "date_order", "unit_system", "language"):
            if g != v:
                bad.append(f"sheet.{k}: got {g!r}, want {v!r}")
        elif _norm_str(g or "") != _norm_str(v or ""):
            (bad if k in ("device_model", "source_name") else notes).append(
                f"sheet.{k}: got {g!r}, want {v!r}"
            )

    def by_field(items):
        return {i["field"]: i for i in items}

    for kind, gi, wi in (
        ("observation", by_field(got["observations"]), by_field(want["observations"])),
        ("label", by_field(got["labels"]), by_field(want["labels"])),
    ):
        for f in sorted(wi.keys() - gi.keys()):
            bad.append(f"missing {kind} {f} (want {wi[f]['value']!r})")
        for f in sorted(gi.keys() - wi.keys()):
            bad.append(f"extra {kind} {f}={gi[f]['value']!r}")
        for f in sorted(wi.keys() & gi.keys()):
            a, b = gi[f], wi[f]
            same = (
                math.isclose(a["value"], b["value"], abs_tol=1e-6)
                if kind == "observation"
                else a["value"] == b["value"]
            )
            if not same or a.get("unit") != b.get("unit"):
                bad.append(
                    f"{f}: got {a['value']!r} {a.get('unit', '')}, want {b['value']!r} {b.get('unit', '')}"
                )
            elif _norm_str(a["printed"]) != _norm_str(b["printed"]):
                notes.append(f"{f}: printed {a['printed']!r} vs golden {b['printed']!r}")
    ge = {_norm_str(e["printed_label"]): e["kcal"] for e in got["exercise"]}
    we = {_norm_str(e["printed_label"]): e["kcal"] for e in want["exercise"]}
    for k in sorted(we.keys() | ge.keys()):
        if ge.get(k) != we.get(k):
            bad.append(f"exercise {k!r}: got {ge.get(k)}, want {we.get(k)}")
    gh = {p["measured_at_local"]: p for p in got["history"]}
    wh = {p["measured_at_local"]: p for p in want["history"]}
    for t in sorted(wh.keys() - gh.keys()):
        bad.append(f"missing history point {t}")
    for t in sorted(gh.keys() - wh.keys()):
        bad.append(f"extra history point {t}")
    for t in sorted(wh.keys() & gh.keys()):
        if gh[t]["is_current_test"] != wh[t]["is_current_test"]:
            bad.append(
                f"history {t}: is_current_test {gh[t]['is_current_test']} != {wh[t]['is_current_test']}"
            )
        a = {v["field"]: (v["value"], v["unit"]) for v in gh[t]["values"]}
        b = {v["field"]: (v["value"], v["unit"]) for v in wh[t]["values"]}
        if a != b:
            bad.append(f"history {t}: got {a}, want {b}")
    gw = {(w["code"], w["field"]) for w in got["warnings"]}
    ww = {(w["code"], w["field"]) for w in want["warnings"]}
    if gw != ww:
        notes.append(f"warnings differ: missing {sorted(ww - gw)}, extra {sorted(gw - ww)}")
    return bad, notes


def diff_rows(got: list[dict], want: list[dict]) -> list[str]:
    bad = []
    gi = {row_local(r): r for r in got}
    wi = {row_local(r): r for r in want}
    for t in sorted(wi.keys() ^ gi.keys()):
        bad.append(f"row {t} only in {'golden' if t in wi else 'output'}")
    for t in sorted(wi.keys() & gi.keys()):
        for k in wi[t]:
            a, b = gi[t].get(k), wi[t][k]
            if isinstance(a, float) and isinstance(b, (int, float)) and b is not None:
                if not math.isclose(a, b, abs_tol=1e-6):
                    bad.append(f"{t} {k}: got {a}, want {b}")
            elif a != b:
                bad.append(f"{t} {k}: got {a!r}, want {b!r}")
    return bad


# ------------------------------------------------------------------------------ cross-source check


def minute_key(measured_at_local: str) -> str:
    """Dedupe key: local wall-clock truncated to the minute (sheets print HH:MM, the CSV has :SS;
    InBody truncates, it does not round -- 13:42:50 prints as 13:42)."""
    return measured_at_local[:16]


def cross_check(a: dict, b: dict, tol_kg: float = 0.06) -> list[str]:
    """Same measurement from two sources: every field present in both must agree."""
    bad = []
    for f, (kind, _, _) in NUMERIC.items():
        x, y = a.get(f), b.get(f)
        if x is None or y is None:
            continue
        tol = {"mass": tol_kg, "water": tol_kg, "energy": 1.5, "length": 0.3}.get(kind, 0.051)
        if not _close(x, y, tol):
            bad.append(f"{minute_key(row_local(a))} {f}: {x} vs {y}")
    return bad


# ------------------------------------------------------------------------------ CLI / tests


def check_file(path: Path, golden: Path | None) -> int:
    wire = json.loads(path.read_text())
    status = 0
    schema = wire_schema()
    errs = validate_schema(wire, schema)
    if errs:
        print(f"SCHEMA {path.name}: {len(errs)} error(s)")
        for e in errs[:50]:
            print("  ", e)
        return 2
    rows, problems = normalize(wire)
    e1, w1 = sanity_wire(wire)
    errors, warnings = list(problems) + e1, list(w1)
    for r in rows:
        e, w = sanity(r)
        tag = "" if not r["from_history"] else f" [history {row_local(r)}]"
        errors += [x + tag for x in e]
        warnings += [x + tag for x in w]
    for e in errors:
        print(f"ERROR {path.name}: {e}")
    for w in warnings:
        print(f"warn  {path.name}: {w}")
    if errors:
        status = 1
    if golden and golden.exists() and golden.resolve() != path.resolve():
        bad, notes = diff_wire(wire, json.loads(golden.read_text()))
        for b in bad:
            print(f"DIFF  {path.name}: {b}")
        for n in notes:
            print(f"note  {path.name}: {n}")
        if bad:
            status = 1
        print(f"{path.name}: {len(bad)} difference(s) vs {golden.name}")
    print(f"{path.name}: {'OK' if status == 0 else 'FAILED'} ({len(rows)} canonical row(s))")
    return status


def _golden_files() -> tuple[list[Path], list[Path]]:
    if not EXPECTED.exists():
        return [], []
    sheets = sorted(
        p
        for p in EXPECTED.glob("*.json")
        if not p.name.endswith(".canonical.json")
        and not p.name.startswith("sample_csv")
        and p.name != "dates.json"
    )
    csvs = sorted(EXPECTED.glob("sample_csv*.json"))
    return sheets, csvs


def test_schema_files_up_to_date():
    for name, schema in (
        ("schema.json", wire_schema()),
        ("measurement.schema.json", canonical_schema()),
    ):
        on_disk = json.loads((PACKAGE / name).read_text())
        assert on_disk == json.loads(json.dumps(schema)), (
            f"{name} is stale: run `validate.py emit-schema`"
        )


def test_wire_schema_within_structured_output_limits():
    """Strict structured outputs: <=24 optional and <=16 union-typed parameters (docs, 2026-11)."""
    optional = unions = 0

    def walk(node):
        nonlocal optional, unions
        if isinstance(node, dict):
            if isinstance(node.get("type"), list) or "anyOf" in node:
                unions += 1
            if node.get("type") == "object" and "properties" in node:
                optional += len(set(node["properties"]) - set(node.get("required", [])))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(wire_schema())
    assert optional <= 24 and unions <= 16, (optional, unions)


def test_conversions():
    assert to_canonical("weight_kg", 190.0, "lb") == (86.183, True)
    assert to_canonical("bmr_kcal", 7000, "kJ") == (1673, True)
    assert to_canonical("subject_height_cm", 70.0, "in") == (177.8, True)
    assert to_canonical("total_body_water_l", 100.0, "lb") == (45.359, True)
    assert to_canonical("weight_kg", 80.0, "kg") == (80.0, False)


def test_goldens():
    sheets, csvs = _golden_files()
    if not sheets and not csvs:
        import pytest

        pytest.skip("no samples/expected (health data is not in git)")
    for p in sheets:
        assert check_file(p, None) == 0, p
    for p in csvs:
        rows = json.loads(p.read_text())
        canon = canonical_schema()
        for r in rows:
            assert not validate_schema(r, canon), validate_schema(r, canon)[:5]
            e, _ = sanity(r)
            assert not e, (row_local(r), e)


def test_csv_reference_parser_matches_golden():
    src = REPO / "samples" / "sample_csv.csv"
    golden = EXPECTED / "sample_csv.json"
    if not (src.exists() and golden.exists()):
        import pytest

        pytest.skip("no CSV sample")
    assert not diff_rows(parse_csv(src.read_text(encoding="utf-8")), json.loads(golden.read_text()))


def test_sheet_vs_csv_same_measurement_agree():
    sheets, csvs = _golden_files()
    if not sheets or not csvs:
        import pytest

        pytest.skip("no goldens")
    csv_rows = {minute_key(row_local(r)): r for p in csvs for r in json.loads(p.read_text())}
    matched = 0
    for p in sheets:
        rows, _ = normalize(json.loads(p.read_text()))
        for r in rows:
            other = csv_rows.get(minute_key(row_local(r)))
            if other:
                matched += 1
                assert not cross_check(r, other), cross_check(r, other)
    assert matched > 0


def selftest() -> int:
    failures = 0
    for t in (
        test_schema_files_up_to_date,
        test_wire_schema_within_structured_output_limits,
        test_conversions,
        test_goldens,
        test_csv_reference_parser_matches_golden,
        test_sheet_vs_csv_same_measurement_agree,
    ):
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:  # noqa: BLE001 -- report every failing test, keep going
            if type(exc).__name__ == "Skipped":
                print(f"SKIP {t.__name__}: {exc}")
                continue
            failures += 1
            print(f"FAIL {t.__name__}: {exc!r}")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("response", type=Path)
    c.add_argument("--golden", type=Path)
    n = sub.add_parser("normalize")
    n.add_argument("response", type=Path)
    s = sub.add_parser("csv")
    s.add_argument("csvfile", type=Path)
    sub.add_parser("emit-schema")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        golden = a.golden or EXPECTED / a.response.name
        return check_file(a.response, golden)
    if a.cmd == "normalize":
        rows, problems = normalize(json.loads(a.response.read_text()))
        for p in problems:
            print("PROBLEM", p, file=sys.stderr)
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 1 if problems else 0
    if a.cmd == "csv":
        print(
            json.dumps(
                parse_csv(a.csvfile.read_text(encoding="utf-8")), indent=2, ensure_ascii=False
            )
        )
        return 0
    if a.cmd == "emit-schema":
        emit_schema()
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
