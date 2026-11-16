"""Deterministic CSV parsing -- no Claude call, no spend.

Known format: the InBody app export (UTF-8 with BOM, comma, `-` = missing, `date` as
`yyyymmddHHMMSS`, units in the header names: lb, inch, cm2, L, kJ). The mapping and the unit
conversion are the extraction package's (`validate.CSV_COLUMNS` / `validate.parse_csv`), so the
output equals `samples/expected/sample_csv.json` by construction
(tests/web/test_golden.py). Columns are matched by header NAME; an unknown header fails the whole
file -- a silently dropped column would corrupt the history unnoticed. Anything unexpected
(unknown header / unit, bad date, non-numeric value, two rows on one day) -> CsvNeedsReview ->
`needs_review` + a push to Vojta, nothing is written (docs/upload-pipeline.md, "Be defensive").

Pluggable: PARSERS holds `(text) -> list[canonical row] | None` functions tried in order (None =
not my format). No match -> CsvNotSupported. A metric export (`Weight(kg)` ...) would be a new
parser or new CSV_COLUMNS entries -- never a guess.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from datetime import datetime

from sheetpkg import CSV_PARSER_VERSION, validate

PARSER_VERSION = CSV_PARSER_VERSION
DATE_HEADER = "date"


class CsvNotSupported(Exception):
    pass


class CsvNeedsReview(Exception):
    """The file is an InBody export but does not look as expected (docs/upload-pipeline.md, CV):
    do not guess -> `needs_review`. `.codes` are value-free reason codes (safe for the push),
    `.details` may quote the file (admin page only)."""

    def __init__(self, codes: list[str], details: list[str]) -> None:
        super().__init__(", ".join(codes))
        self.codes = codes
        self.details = details


_STAMP = re.compile(r"^\d{14}$")


def precheck_export(text: str) -> None:
    """CV checks BEFORE parsing: every header known (units live in the header names, so this also
    catches an unrecognised unit), every date `yyyymmddHHMMSS`, every value a number or '-', and
    no two rows on the same day (the app keeps one measurement per day; two in one export is not
    a shape we expect)."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    header = [h.strip() for h in next(reader, [])]
    codes: list[str] = []
    details: list[str] = []

    def flag(code: str, detail: str) -> None:
        if code not in codes:
            codes.append(code)
        if len(details) < 20:
            details.append(detail)

    unknown = [h for h in header if h not in validate.CSV_COLUMNS]
    if unknown:
        flag("csv_unknown_header", f"unknown columns: {unknown}")
    days: dict[str, int] = {}
    for line_no, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        if len(cells) != len(header):
            flag("csv_row_shape", f"line {line_no}: {len(cells)} cells, header has {len(header)}")
            continue
        rec = dict(zip(header, cells, strict=True))
        stamp = rec.get(DATE_HEADER, "").strip()
        try:
            if not _STAMP.match(stamp):
                raise ValueError
            day = datetime.strptime(stamp, "%Y%m%d%H%M%S").date().isoformat()
        except ValueError:
            flag("csv_bad_date", f"line {line_no}: date {stamp!r}")
            continue
        if day in days:
            flag("csv_duplicate_date", f"line {line_no}: {day} also on line {days[day]}")
        days.setdefault(day, line_no)
        for name, raw in rec.items():
            if name in (DATE_HEADER, "Measurement device.") or name not in validate.CSV_COLUMNS:
                continue
            value = raw.strip()
            if value == "-":
                continue
            try:
                float(value)
            except ValueError:
                flag("csv_bad_value", f"line {line_no}: {name} = {value[:20]!r}")
    if codes:
        raise CsvNeedsReview(codes, details)


def inbody_app_export(text: str) -> list[dict] | None:
    first_line = text.lstrip("﻿").split("\n", 1)[0]
    if first_line.split(",", 1)[0].strip().strip('"') != DATE_HEADER:
        return None  # not this format
    precheck_export(text)  # CsvNeedsReview when the shape is off -- never a best-effort parse
    try:
        rows = validate.parse_csv(text)
    except ValueError as exc:  # unknown header, bad value, bad date
        raise CsvNotSupported(str(exc)[:400]) from exc
    if not rows:
        raise CsvNotSupported("no data rows")
    return rows


Parser = Callable[[str], list[dict] | None]
PARSERS: list[Parser] = [inbody_app_export]


def parse_csv(text: str) -> list[dict]:
    for parser in PARSERS:
        rows = parser(text)
        if rows is not None:
            return rows
    raise CsvNotSupported("csv format not yet supported")
