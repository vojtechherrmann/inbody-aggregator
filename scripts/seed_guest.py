"""Seed (or re-seed) the public guest account "guest" with FICTIONAL sample data.

    .venv/bin/python scripts/seed_guest.py            # local SQLite (SQL_BACKEND=sqlite, default)

    cd /Users/vherrmann/SRC/vojtechherrmann/inbody-aggregator && \\
        AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal SQL_BACKEND=azure \\
        .venv/bin/python scripts/seed_guest.py        # Azure SQL, as the Entra admin (az token)

What it does (idempotent -- running it again replaces the guest's data with an identical set):
* creates the user `config.GUEST_USER_ID` ("guest") if missing: NO usable password (the hash is the
  `security.UNUSABLE_PASSWORD_HASH` marker, so no password verifies), not admin. Visitors enter
  through "Log in as Guest" (POST /login/guest). An existing row keeps its `is_disabled` flag, so
  `users.py disable guest` stays a working off switch across re-seeds.
* deletes ALL of the guest's data rows (measurement, extraction, upload, usage, shares, grants) and
  inserts the sample set again with deterministic ids. Sessions are left alone (open guest
  sessions keep working). Nothing of any other user is touched.
* the data is the SAME fictional set as the design prototype (docs/prototype/build_example.py,
  fixed seed, imported from there so there is ONE generator): 12 measurements 2024-2026, mixed
  sources (Czech sheet, app screenshot, app CSV exports), long pauses, all canonical columns each
  source has. Never real data.

How the rows are made: one `upload` per source file (consecutive same-source measurements form
one file, like a real CSV export), one `extraction` per measurement through the app's own
`fields.checked` + `pipeline.extraction_values`, then `pipeline.refresh_user` runs the real day
check, so `measurement` rows, upload statuses and reports are exactly what a real upload yields.

No blob is written (and no Claude call is made): the seeded uploads point at paths that do not
exist, so `GET /files/<id>` is a plain 404, and `pipeline.process_upload` / `select_uploads` /
`restore_upload` leave the guest's rows alone.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "web"))

import config  # noqa: E402
import data  # noqa: E402
import db  # noqa: E402
import fields  # noqa: E402
import pipeline  # noqa: E402
import security  # noqa: E402
from sheetpkg import CSV_PARSER_VERSION, SHEET_PARSER_VERSION, validate  # noqa: E402
from sqlalchemy import delete, or_, select, update  # noqa: E402

# Deterministic ids: the same set on every run. They also depend on the guest id, so a renamed
# guest never collides with the rows of the old one.
_NS = uuid.UUID("0b6f2f0e-5a44-4c52-9e0d-6f2c1d7a9b31")
_GENERATOR = REPO / "docs" / "prototype" / "build_example.py"
_SHEET_KEYS = ("measured_date", "measured_time", "origin", "id", "gym")


def load_generator():
    """docs/prototype/build_example.py as a module (its `__main__` block does not run)."""
    spec = importlib.util.spec_from_file_location("prototype_build_example", _GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_rows() -> list[dict]:
    """Alex's measurements of the prototype's example data, oldest first (full set)."""
    example = load_generator().example_data()
    alex = next(p for p in example["people"] if p["id"] == "alex")
    return sorted(alex["measurements"], key=lambda r: r["measured_date"])


def source_of(rec: dict) -> str:
    """csv | sheet (Czech sheet, everything) | app (US app screenshot: no score, no seg. fat)."""
    if rec["origin"] == "csv":
        return "csv"
    return "sheet" if "protein_kg" in rec else "app"


def canonical_row(rec: dict) -> dict:
    """One generated record -> a canonical row (validate.empty_row() shape) with source metadata
    the way the real CSV parser / sheet normalizer would fill it."""
    source = source_of(rec)
    row = validate.empty_row()
    row.update({k: v for k, v in rec.items() if k not in _SHEET_KEYS})
    unknown = set(row) - set(validate.empty_row())
    assert not unknown, f"generator column not in the canonical registry: {sorted(unknown)}"
    row.update(
        measured_date=rec["measured_date"],
        measured_time=rec["measured_time"],
        origin=rec["origin"],
        from_history=False,
    )
    if source == "csv":  # what validate.parse_csv sets
        row.update(
            parser_version=CSV_PARSER_VERSION,
            source_name="InBody app export",
            device_model="InBody270",
            source_unit_system="us",
        )
    else:
        row.update(
            parser_version=SHEET_PARSER_VERSION,
            source_name=rec["gym"],
            sheet_language="cs" if source == "sheet" else "en",
            source_unit_system="metric" if source == "sheet" else "us",
        )
    return row


def file_groups(rows: list[dict]) -> list[dict]:
    """Consecutive measurements of one source form one file (a CSV export holds many days)."""
    groups: list[dict] = []
    for rec in rows:
        source = source_of(rec)
        if groups and groups[-1]["source"] == source == "csv":
            groups[-1]["recs"].append(rec)
        else:
            groups.append({"source": source, "recs": [rec]})
    for n, g in enumerate(groups, start=1):
        last = g["recs"][-1]["measured_date"]
        kind, stem = {"csv": ("csv", "inbody-export"), "sheet": ("pdf", "inbody-sheet")}.get(
            g["source"], ("png", "inbody-app")
        )
        g.update(
            id=str(uuid.uuid5(_NS, f"upload:{config.GUEST_USER_ID}:{n}")),
            kind=kind,
            name=f"{stem}-{last}.{kind}",
            gym=g["recs"][0]["gym"],
            created_at=datetime.combine(datetime.fromisoformat(last).date(), time(20, 0)),
        )
    return groups


def clear_guest_data(conn) -> None:
    """Delete every data row of the guest (FK order). Sessions and the user row stay."""
    uid = config.GUEST_USER_ID
    for table in (
        db.measurement,
        db.extraction,
        db.usage_allocation,
        db.extra_usage,
    ):
        conn.execute(delete(table).where(table.c.user_id == uid))
    conn.execute(delete(db.api_usage).where(db.api_usage.c.user_id == uid))
    conn.execute(delete(db.upload).where(db.upload.c.user_id == uid))
    conn.execute(
        delete(db.share).where(or_(db.share.c.owner_id == uid, db.share.c.viewer_id == uid))
    )


def ensure_guest_user(conn) -> None:
    uid = config.GUEST_USER_ID
    if data.user_exists(conn, uid):
        conn.execute(
            update(db.app_user)
            .where(db.app_user.c.id == uid)
            .values(
                is_admin=False,
                password_hash=security.UNUSABLE_PASSWORD_HASH,
                password_admin_set=False,
                failed_login_count=0,
                locked_until=None,
            )
        )
    else:
        data.create_user(conn, uid, security.UNUSABLE_PASSWORD_HASH, guest=True)


def seed(conn) -> dict:
    """Replace the guest's data. Returns counts (never values)."""
    rows = sample_rows()
    now = db.utcnow()
    ensure_guest_user(conn)
    clear_guest_data(conn)
    uid = config.GUEST_USER_ID
    groups = file_groups(rows)
    for g in groups:
        assert g["created_at"] < now, "sample data must lie in the past"
        digest = uuid.uuid5(_NS, f"sha:{g['id']}").hex * 2  # 64 hex chars, no file behind it
        blob = f"raw/{uid}/{g['created_at']:%Y-%m}/{g['id']}-{g['name']}"  # never written
        pipeline._insert_upload(
            conn,
            g["id"],
            uid,
            g["kind"],
            g["name"],
            blob,
            digest,
            4096,
            g["created_at"],
            status="done",
            source_name=g["gym"] if g["source"] == "csv" else None,
        )
        for n, rec in enumerate(g["recs"]):
            e = fields.checked(canonical_row(rec))
            assert not e.invalid, f"sample row {rec['measured_date']}: nulled {e.invalid}"
            values = pipeline.extraction_values(
                e, g["id"], uid, e.row["parser_version"], g["created_at"], n
            )
            conn.execute(db.extraction.insert().values(**values))
    pipeline.refresh_user(conn, uid)  # the real day check -> measurements, statuses, reports
    live = conn.execute(select(db.measurement.c.id).where(db.measurement.c.user_id == uid)).all()
    assert len(live) == len(rows), f"{len(live)} measurements for {len(rows)} sample rows"
    return {"uploads": len(groups), "measurements": len(live)}


def main() -> None:
    if config.SQL_BACKEND != "azure":
        print(f"[seed_guest] local database: {config.SQLITE_PATH}")
    with db.engine().begin() as conn:
        counts = seed(conn)
    print(
        f"seeded {config.GUEST_USER_ID}: {counts['uploads']} uploads, "
        f"{counts['measurements']} measurements ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)"
    )


if __name__ == "__main__":
    main()
