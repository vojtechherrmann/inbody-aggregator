"""Golden files from the extraction package (samples/ is gitignored health data: these tests SKIP
when it is absent, e.g. in CI). Failure messages name fields / statuses / counts only -- never a
measured value, so a red run cannot leak health data into logs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "samples"
EXPECTED = SAMPLES / "expected"
CSV = SAMPLES / "sample_csv.csv"
SHEETS = (
    "sample_us_app.png",
    "sample_sheet_a.jpg",
    "sample_sheet_b.jpg",
)


def _day(stem: str) -> str:
    """The real test day of a sample sheet (local, gitignored: samples/expected/dates.json)."""
    return json.loads((EXPECTED / "dates.json").read_text())[stem]


needs_samples = pytest.mark.skipif(
    not EXPECTED.exists(), reason="no samples/ (health data, not in git)"
)


def _fields_only(diffs: list[str]) -> list[str]:
    """validate.diff_rows lines look like '<time> <field>: got .., want ..' -> keep the field."""
    return sorted({d.split()[1].rstrip(":") if len(d.split()) > 1 else "?" for d in diffs})


@needs_samples
def test_csv_parser_reproduces_the_golden():
    import csv_parsers
    from sheetpkg import validate

    rows = csv_parsers.parse_csv(CSV.read_text(encoding="utf-8"))
    golden = json.loads((EXPECTED / "sample_csv.json").read_text())
    diffs = validate.diff_rows(rows, golden)
    assert not diffs, f"{len(diffs)} differences in {_fields_only(diffs)}"
    assert len(rows) == len(golden)


@needs_samples
@pytest.mark.parametrize("sheet", SHEETS)
def test_sheet_normalization_reproduces_the_canonical_golden(sheet):
    import sheet_schema
    from sheetpkg import validate

    stem = sheet.rsplit(".", 1)[0]
    wire = json.loads((EXPECTED / f"{stem}.json").read_text())
    rows, problems, wire_errors = sheet_schema.normalize(wire)
    golden = json.loads((EXPECTED / f"{stem}.canonical.json").read_text())
    diffs = validate.diff_rows(rows, golden)
    assert not diffs, f"{len(diffs)} differences in {_fields_only(diffs)}"
    assert not problems and not wire_errors, (len(problems), len(wire_errors))


@needs_samples
def test_four_samples_in_order_give_twelve_measurements():
    """CSV, then the Gym C screenshot (its test day + all its history days are CSV days -> REJECTED;
    since 2026-11-17 the screenshot of the SAME measurement still ENRICHES the CSV day with the
    values the CSV lacks), then Gym A + Gym B (new days -> ACCEPTED). 10 + 1 + 1 = 12.
    The sheets' 'model answers' are the golden wire files -- no API call."""
    import db
    import pipeline
    from helpers import FakeClient
    from sqlalchemy import select

    statuses = {}
    csv_id = pipeline.admit("vojta", CSV.name, CSV.read_bytes())
    statuses[CSV.name] = pipeline.process_upload(csv_id)
    reports = {}
    for name in SHEETS:
        stem = name.rsplit(".", 1)[0]
        answer = json.loads((EXPECTED / f"{stem}.json").read_text())
        upload_id = pipeline.admit("vojta", name, (SAMPLES / name).read_bytes())
        statuses[name] = pipeline.process_upload(upload_id, FakeClient(answer))
        with db.engine().connect() as conn:
            reports[name] = json.loads(
                conn.execute(
                    select(db.upload.c.report_json).where(db.upload.c.id == upload_id)
                ).scalar()
            )
    assert statuses[CSV.name] in ("done", "partial"), statuses
    assert statuses["sample_us_app.png"] == "done", statuses  # it completed the CSV day
    assert statuses["sample_sheet_a.jpg"] in ("done", "partial"), statuses
    assert statuses["sample_sheet_b.jpg"] in ("done", "partial"), statuses

    us_app = reports["sample_us_app.png"]["measurements"]
    assert us_app and all(
        m["outcome"] == "rejected" and m["reason"] == "day_exists" for m in us_app
    ), [(m["from_history"], m["outcome"], m["reason"]) for m in us_app]
    assert any(m["date"] == _day("sample_us_app") and not m["from_history"] for m in us_app)
    test_day = next(
        m for m in us_app if m["date"] == _day("sample_us_app") and not m["from_history"]
    )
    assert test_day["enriched"], "the screenshot should add the values the CSV does not have"
    assert not any(
        m["enriched"] for m in us_app if m is not test_day
    )  # history points never enrich
    for name, day in (
        ("sample_sheet_a.jpg", _day("sample_sheet_a")),
        ("sample_sheet_b.jpg", _day("sample_sheet_b")),
    ):
        main = [m for m in reports[name]["measurements"] if not m["from_history"]]
        assert [(m["date"], m["outcome"]) for m in main] == [(day, "accepted")], name

    with db.engine().connect() as conn:
        total = conn.execute(select(db.measurement.c.id)).all()
    assert len(total) == 12, len(total)
