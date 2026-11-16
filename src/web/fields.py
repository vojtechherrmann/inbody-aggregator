"""The canonical measurement columns, DERIVED from the extraction package's field registry
(`extraction/validate.py`, via sheetpkg) -- the app never keeps its own field list.

Every column is nullable. Groups:
* NUMERIC (127, SI units, FLOAT) and CATEGORICAL (13, short strings);
* SOURCE_META -- device / source metadata strings;
* JSON_COLUMNS -- exercise_kcal_30min, converted_from, warnings;
* row metadata: measured_date (DATE -- the matching key: one measurement per user per day),
  measured_time (TIME as printed, informational, no timezone), origin (csv | sheet |
  sheet_history), from_history, parser_version.
Adding a field = edit the REGISTRY there, `emit-schema`, bump its VERSION, and add a SQL
migration here (`scripts/gen_sql.py` prints the DDL; tests/web/test_schema_sync.py checks it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time

from sheetpkg import validate as V

NUMERIC: tuple[str, ...] = tuple(V.NUMERIC)
CATEGORICAL: tuple[str, ...] = tuple(V.CATEGORICAL)
FIELDS: tuple[str, ...] = NUMERIC + CATEGORICAL  # the value columns
SOURCE_META: tuple[str, ...] = (  # where the measurement came from (strings)
    "source_name",
    "device_model",
    "device_serial",
    "software_version",
    "sheet_template",
    "sheet_language",
    "source_unit_system",
)
JSON_COLUMNS: tuple[str, ...] = tuple(V.JSON_COLUMNS)
ROW_META = ("measured_date", "measured_time", "origin", "from_history", "parser_version")
assert set(V.META) == set(ROW_META) | set(SOURCE_META), "registry META changed: update fields.py"

ORIGIN_CSV, ORIGIN_SHEET, ORIGIN_HISTORY = "csv", "sheet", "sheet_history"

# The core metrics: a full extraction missing one of these makes the upload `partial`.
REQUIRED_CORE = ("weight_kg", "skeletal_muscle_mass_kg", "body_fat_mass_kg", "percent_body_fat")
# Report "what's missing": core fields worth naming when null.
CORE = (
    "weight_kg",
    "skeletal_muscle_mass_kg",
    "body_fat_mass_kg",
    "percent_body_fat",
    "bmi",
    "bmr_kcal",
    "visceral_fat_level",
    "total_body_water_l",
    "inbody_score",
)
CHARTED = ("weight_kg", "percent_body_fat", "skeletal_muscle_mass_kg", "visceral_fat_level")
SEGMENTAL = tuple(
    f for f in NUMERIC if f.startswith(("seg_lean_", "seg_fat_")) and f.endswith("_kg")
)
IMPEDANCE = tuple(f for f in NUMERIC if f.startswith("z_"))
REPORT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("weight", ("weight_kg",)),
    ("smm", ("skeletal_muscle_mass_kg",)),
    ("bfm", ("body_fat_mass_kg",)),
    ("pbf", ("percent_body_fat",)),
    ("bmi", ("bmi",)),
    ("bmr", ("bmr_kcal",)),
    ("visceral", ("visceral_fat_level", "visceral_fat_area_cm2")),
    ("water", ("total_body_water_l", "ecw_tbw_ratio")),
    ("segmental", SEGMENTAL),
    ("impedance", IMPEDANCE),
    ("score", ("inbody_score",)),
)

_RANGE_ERR = re.compile(r"^(\w+)=\S+ outside sanity range")


def parse_date_time(row: dict) -> tuple[date, time | None]:
    """Canonical measured_date (+ optional measured_time as printed). Raises ValueError."""
    day = datetime.strptime(row.get("measured_date") or "", "%Y-%m-%d").date()
    raw = row.get("measured_time")
    if not raw:
        return day, None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return day, datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"unparseable measured_time {raw!r}")


@dataclass
class Extracted:
    """One canonical row (validate.empty_row() shape) + the app's check results."""

    row: dict
    measured_date: date
    measured_time: time | None  # as printed; informational only, never used for matching
    invalid: list[str] = field(default_factory=list)  # nulled by a sanity range check
    errors: list[str] = field(default_factory=list)  # identity / consistency errors (kept)
    check_warnings: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return self.row["origin"]

    @property
    def from_history(self) -> bool:
        return bool(self.row.get("from_history"))


def checked(row: dict, extra_errors: list[str] | None = None) -> Extracted:
    """Run the registry's sanity rules on a canonical row. A field outside its sanity range is
    set to null and reported ("couldn't read X"); identity errors are kept as errors (the upload
    becomes `partial`, the report shows them). The input row is not modified."""
    row = dict(row)
    measured_date, measured_time = parse_date_time(row)
    errors, _ = V.sanity(row)
    invalid = []
    for err in errors:
        m = _RANGE_ERR.match(err)
        if m and m.group(1) in row:
            row[m.group(1)] = None
            invalid.append(m.group(1))
    errors, warnings = V.sanity(row)  # again, without the misread values
    errors = [e for e in errors if e != "weight_kg missing" or row.get("origin") != ORIGIN_HISTORY]
    return Extracted(
        row=row,
        measured_date=measured_date,
        measured_time=measured_time,
        invalid=invalid,
        errors=[*(extra_errors or []), *errors],
        check_warnings=warnings,
    )
