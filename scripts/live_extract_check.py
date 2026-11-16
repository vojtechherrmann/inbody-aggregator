"""LIVE check of the sheet extraction against the golden files -- SENDS HEALTH DATA TO ANTHROPIC.

Runs the 3 sample sheets in samples/ through the real path (pre-checks -> budget -> Claude ->
post-checks, as in `pipeline.parse_sheet`), diffs the canonical rows against
samples/expected/<stem>.canonical.json and prints the cost of every sheet.

    cd /Users/vherrmann/SRC/vojtechherrmann/inbody-aggregator && \\
        .venv/bin/python scripts/live_extract_check.py --yes-send-health-data

* Requires the explicit flag above (the user approved one live run, 2026-11-16).
* The Anthropic key is read from .apikeys/anthropic.env (`ANTHROPIC_API_KEY=...`), never printed.
* Uses a throw-away local SQLite DB + local blob dir (in a temp directory): the spend limits
  apply (the worst case is checked before each call) but nothing touches Azure.
* Output: per sheet the diff as FIELD NAMES only (no measured values), the review codes if the
  defensive checks fire, and the cost. Expected cost ~$0.10-0.15 per sheet, < $0.50 in total.
* Comparison = the package's semantics (validate.diff_wire): the canonical columns value by
  value, the exercise table as printed label -> kcal (the English `activity` name is free text and
  not compared), warnings as (code, field) -- a different note text is not a difference.
* `--keep-output DIR` (must be inside samples/, which is gitignored) writes each sheet's raw model
  answer (`<stem>.wire.json`) and canonical rows (`<stem>.rows.json`) for offline analysis.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SAMPLES = REPO / "samples"
EXPECTED = SAMPLES / "expected"
SHEETS = (
    ("sample_us_app.png", "image/png"),
    ("sample_sheet_a.jpg", "image/jpeg"),
    ("sample_sheet_b.jpg", "image/jpeg"),
)


def _load_key() -> None:
    path = REPO / ".apikeys" / "anthropic.env"
    if not path.exists():
        raise SystemExit(f"missing {path} (one line: ANTHROPIC_API_KEY=...)")
    for line in path.read_text().splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip().strip('"')
            return
    raise SystemExit(f"{path} has no ANTHROPIC_API_KEY= line")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--yes-send-health-data", action="store_true", required=True)
    ap.add_argument("--keep-output", metavar="DIR", help="dir inside samples/ for raw answers")
    args = ap.parse_args()
    keep = None
    if args.keep_output:
        keep = Path(args.keep_output).resolve()
        if SAMPLES.resolve() not in (keep, *keep.parents):
            raise SystemExit("--keep-output must be inside samples/ (gitignored health data)")
        keep.mkdir(parents=True, exist_ok=True)
    if not EXPECTED.exists():
        raise SystemExit("samples/expected/ not found")
    _load_key()

    tmp = tempfile.mkdtemp(prefix="inbody-live-")
    os.environ.update(
        SQL_BACKEND="sqlite",
        SQLITE_PATH=str(Path(tmp) / "live.db"),
        STORAGE_BACKEND="local",
        LOCAL_STORAGE_DIR=str(Path(tmp) / "blobs"),
        WORKER_ENABLED="0",
        INBODY_NTFY_TOPIC="",
    )
    sys.path.insert(0, str(REPO / "src" / "web"))
    import data  # noqa: E402
    import db  # noqa: E402
    import extract  # noqa: E402
    import pipeline  # noqa: E402
    import security  # noqa: E402
    from sheetpkg import validate  # noqa: E402
    from sqlalchemy import select  # noqa: E402

    with db.engine().begin() as conn:
        data.create_user(conn, "live-check", security.hash_password(security.generate_password()))
    client = extract.make_client()
    failures, total_cost = 0, 0
    for name, mime in SHEETS:
        stem = name.rsplit(".", 1)[0]
        golden = json.loads((EXPECTED / f"{stem}.canonical.json").read_text())
        before = len(_usage(db, select))
        content = (SAMPLES / name).read_bytes()
        wire, rows, verdict = None, [], "parsed"
        try:
            pipeline.prechecks.check(name.rsplit(".", 1)[1].replace("jpeg", "jpg"), content)
            wire = extract.call_claude(
                content, mime, user_id="live-check", upload_id=None, client=client
            )
            rows = [e.row for e in pipeline.rows_from_wire(wire)]
        except pipeline._Review as exc:
            rows, verdict = [e.row for e in exc.exts], f"needs_review {exc.codes}"
        except pipeline._Failed as exc:
            verdict = f"failed {exc.code}"
        except (
            extract.ExtractionFailed,
            extract.NotInBodySheet,
            pipeline.prechecks.PrecheckFailed,
        ) as exc:
            verdict = f"failed {type(exc).__name__}"
        if keep and wire is not None:
            (keep / f"{stem}.wire.json").write_text(json.dumps(wire, indent=1, ensure_ascii=False))
            (keep / f"{stem}.rows.json").write_text(
                json.dumps(rows, indent=1, ensure_ascii=False, default=str)
            )
        usage = _usage(db, select)[before:]
        cost = sum(u.cost_microusd for u in usage)
        total_cost += cost
        bad, notes = compare(validate, rows, golden, wire, EXPECTED / f"{stem}.json")
        ok = verdict == "parsed" and not bad
        failures += not ok
        tokens = ", ".join(f"{u.model} in={u.input_tokens} out={u.output_tokens}" for u in usage)
        print(f"{'OK  ' if ok else 'DIFF'} {name}: {verdict}; ${cost / 1e6:.4f} ({tokens})")
        for line in bad:
            print(f"     DIFF {line}")
        for line in notes:
            print(f"     note {line}")
    print(f"total ${total_cost / 1e6:.4f}; {failures} sheet(s) not matching the golden")
    return 1 if failures else 0


JSON_FREE_TEXT = ("exercise_kcal_30min", "warnings", "converted_from")


def compare(validate, rows, golden, wire, golden_wire_path) -> tuple[list[str], list[str]]:
    """(differences, notes) -- field / activity / warning-code NAMES only, never values."""
    if not rows:
        return ["no rows"], []

    def strip(rs):
        return [{k: v for k, v in r.items() if k not in JSON_FREE_TEXT} for r in rs]

    diffs = validate.diff_rows(strip(rows), strip(golden))
    bad = [
        f"column {f}"
        for f in sorted({d.split()[1].rstrip(":") for d in diffs if len(d.split()) > 1})
    ]
    notes: list[str] = []
    got_main, want_main = rows[0], golden[0]

    def labels(r):
        return {
            validate._norm_str(e["printed_label"]): e for e in (r.get("exercise_kcal_30min") or [])
        }

    ge, we = labels(got_main), labels(want_main)
    for key in sorted(ge.keys() | we.keys()):
        g, w = ge.get(key), we.get(key)
        name = (w or g)["activity"]
        if g is None or w is None:
            bad.append(f"exercise row {name}: {'missing' if g is None else 'extra'}")
        elif g["kcal"] != w["kcal"]:
            bad.append(f"exercise row {name}: kcal differs")
        elif g["activity"] != w["activity"]:
            notes.append(f"exercise row {name}: activity name {g['activity']!r} (free text)")
    gw = {(w["code"], w["field"]) for w in got_main.get("warnings") or []}
    ww = {(w["code"], w["field"]) for w in want_main.get("warnings") or []}
    if gw != ww:
        notes.append(f"warnings (code, field) missing {sorted(ww - gw)}, extra {sorted(gw - ww)}")
    return bad, notes


def _usage(db, select):
    with db.engine().connect() as conn:
        return conn.execute(select(db.api_usage).order_by(db.api_usage.c.created_at)).all()


if __name__ == "__main__":
    sys.exit(main())
