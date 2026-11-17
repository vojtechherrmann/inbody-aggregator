"""One measurement per user per DAY, first come wins (docs/upload-pipeline.md section 3).

* Matching uses ONLY `measured_date`. The printed time is unreliable (the InBody app sometimes
  drops PM), so it is stored as printed and used only for display and to order within a day.
* Inside one upload, several extractions on the same day: a full measurement beats a history
  point, then the earliest printed time; the rest -> REJECTED `same_day_in_file`.
* Across uploads: if the user already has a measurement that day -> REJECTED `day_exists`.
  The day's measurement is never replaced or overwritten, BUT (user decision 2026-11-17) a
  rejected full extraction that looks like the same measurement (`_same_measurement`) ENRICHES it:
  values the day is missing (NULL) are filled in from it, in upload order, never overwriting a value
  that is already there (`enrich`). The extraction stays rejected; `Decision.enriched` lists the
  fields it supplied.
* Every extraction row is kept (accepted / reject_reason / conflicting_measurement_id);
  `measurement` holds the accepted ones, with id = the accepted extraction's id.
* Deterministic rebuild: replay ALL of the user's extractions in upload order
  (`upload.created_at`, then id). Deleted ones (extraction.deleted_at, or upload.deleted) are
  skipped -> reason `deleted` -- so after a delete, the next upload's extraction for that day
  takes the day.
* Deleting never removes a row: a deleted measurement keeps its `measurement` row with
  deleted = 1 (pipeline.delete_*), and the replay leaves those rows alone. It rewrites only the
  LIVE rows; an accepted extraction whose row was deleted (an un-delete) is revived in place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, time

import db
import fields
from sqlalchemy import delete, false, select, update

DAY_EXISTS, SAME_DAY_IN_FILE, DELETED = "day_exists", "same_day_in_file", "deleted"
# Only these uploads take part in the day check. `needs_review` extractions are stored but held
# out until an admin approves (then the upload becomes `done` and is replayed in upload order).
COUNTED = ("done", "partial", "nothing_new")
ACCEPTED, REJECTED = "accepted", "rejected"

CANONICAL = [
    c.name
    for c in db.measurement.columns
    if c.name
    not in ("id", "user_id", "upload_id", "created_at", "deleted", "deleted_at", "deleted_by")
]
_LAST = time(23, 59, 59)


@dataclass
class Ext:
    id: str
    upload_id: str
    upload_order: tuple
    measured_date: date
    measured_time: time | None
    origin: str
    from_history: bool
    deleted: bool
    values: dict = field(default_factory=dict)  # every canonical column of the row
    checks: dict = field(default_factory=dict)

    def rank_in_day(self):
        """Within one upload and one day: full before history, then earliest printed time."""
        return (self.from_history, self.measured_time or _LAST, self.id)


@dataclass
class Decision:
    accepted: bool
    reason: str | None = None
    conflicting_id: str | None = None
    enriched: tuple[str, ...] = ()  # canonical fields this (rejected) extraction filled in


def load(conn, user_id: str) -> list[Ext]:
    x, u = db.extraction, db.upload
    rows = conn.execute(
        select(x, u.c.created_at.label("u_created"), u.c.deleted.label("u_deleted"))
        .select_from(x.join(u, x.c.upload_id == u.c.id))
        .where(x.c.user_id == user_id, u.c.status.in_(COUNTED))
    ).all()
    return [
        Ext(
            id=r.id,
            upload_id=r.upload_id,
            upload_order=(r.u_created, r.upload_id),
            measured_date=r.measured_date,
            measured_time=r.measured_time,
            origin=r.origin,
            from_history=bool(r.from_history),
            deleted=r.deleted_at is not None or bool(r.u_deleted),
            values={c: getattr(r, c) for c in CANONICAL},
            checks=json.loads(r.checks_json or "{}"),
        )
        for r in rows
    ]


def decide(exts: list[Ext]) -> dict[str, Decision]:
    """Replay in upload order; the result depends only on the set of extractions."""
    out: dict[str, Decision] = {}
    taken: dict[date, str] = {}  # day -> accepted extraction (= measurement) id
    for upload_order in sorted({e.upload_order for e in exts}):
        mine = [e for e in exts if e.upload_order == upload_order]
        for e in mine:
            if e.deleted:
                out[e.id] = Decision(False, DELETED)
        live = [e for e in mine if not e.deleted]
        for day in sorted({e.measured_date for e in live}):
            same_day = sorted((e for e in live if e.measured_date == day), key=Ext.rank_in_day)
            winner, losers = same_day[0], same_day[1:]
            if day in taken:
                out[winner.id] = Decision(False, DAY_EXISTS, taken[day])
            else:
                out[winner.id] = Decision(True)
                taken[day] = winner.id
            for e in losers:
                out[e.id] = Decision(False, SAME_DAY_IN_FILE, taken.get(day) or winner.id)
    return out


# What never travels between two files of the same day: identity, provenance and parser details.
ENRICH_EXCLUDED = frozenset(
    {
        "measured_date",
        "measured_time",
        "origin",
        "from_history",
        "parser_version",
        "source_name",
        "device_model",
        "device_serial",
        "software_version",
        "sheet_template",
        "sheet_language",
        "source_unit_system",
        "converted_from",
        "warnings",
    }
)
ENRICHABLE = [c for c in CANONICAL if c not in ENRICH_EXCLUDED]
# Two files are "the same measurement" only if what both print agrees (same day can also hold two
# real measurements). At least one of these must be present in both, all present ones within 2 %.
SAME_FIELDS = (
    "weight_kg",
    "skeletal_muscle_mass_kg",
    "body_fat_mass_kg",
    "total_body_water_l",
    "percent_body_fat",
)
SAME_TOLERANCE = 0.02


def _same_measurement(base: dict, donor: dict) -> bool:
    compared = 0
    for f in SAME_FIELDS:
        a, b = base.get(f), donor.get(f)
        if a is None or b is None:
            continue
        a, b = float(a), float(b)
        compared += 1
        if abs(a - b) > SAME_TOLERANCE * max(abs(a), abs(b)):
            return False
    return compared > 0


def enrich(exts: list[Ext], decisions: dict[str, Decision]) -> dict[str, dict]:
    """Fill the NULL values of each day's accepted measurement from the day's rejected full
    extractions (upload order, never overwriting a value). Returns {accepted id: merged values}
    for the measurements that got something and records the fields in `Decision.enriched`."""
    by_id = {e.id: e for e in exts}
    merged: dict[str, dict] = {}
    donors = sorted(
        (
            e
            for e in exts
            if decisions[e.id].reason == DAY_EXISTS and not e.deleted and not e.from_history
        ),
        key=lambda e: (e.upload_order, e.rank_in_day()),
    )
    for e in donors:
        d = decisions[e.id]
        winner = by_id.get(d.conflicting_id)
        if winner is None or not decisions[winner.id].accepted:
            continue
        base = merged.get(winner.id, dict(winner.values))
        if not _same_measurement(base, e.values):
            continue
        filled = [f for f in ENRICHABLE if base.get(f) is None and e.values.get(f) is not None]
        if not filled:
            continue
        for f in filled:
            base[f] = e.values[f]
        merged[winner.id] = base
        decisions[e.id] = Decision(False, d.reason, d.conflicting_id, tuple(filled))
    return merged


def rebuild_user(conn, user_id: str) -> tuple[list[Ext], dict[str, Decision]]:
    """Re-decide every extraction of the user and rewrite their LIVE `measurement` rows (deleted
    rows stay as they are, unless their extraction is accepted again: then they are revived)."""
    exts = load(conn, user_id)
    decisions = decide(exts)
    merged = enrich(exts, decisions)
    for e in exts:
        if e.id in merged:  # the day's measurement with the values later files added
            e.values = merged[e.id]
    for e in exts:
        d = decisions[e.id]
        conn.execute(
            update(db.extraction)
            .where(db.extraction.c.id == e.id)
            .values(
                accepted=d.accepted,
                reject_reason=d.reason,
                conflicting_measurement_id=d.conflicting_id,
            )
        )
    m = db.measurement
    # Live rows are derived data: re-derived wholesale. Deleted rows are the record of a delete.
    conn.execute(delete(m).where(m.c.user_id == user_id, m.c.deleted == false()))
    kept = set(conn.execute(select(m.c.id).where(m.c.user_id == user_id)).scalars())
    now = db.utcnow()
    for e in exts:
        if not decisions[e.id].accepted:
            continue
        values = dict(upload_id=e.upload_id, created_at=now, **e.values)
        if e.id in kept:  # un-deleted (scripts/pipeline.py restore): the same row comes back
            conn.execute(
                update(m)
                .where(m.c.id == e.id)
                .values(deleted=False, deleted_at=None, deleted_by=None, **values)
            )
        else:
            conn.execute(m.insert().values(id=e.id, user_id=user_id, **values))
    return exts, decisions


# ---------------------------------------------------------------------------------------- report


def _filled_groups(values: dict) -> list[str]:
    return [
        name
        for name, members in fields.REPORT_GROUPS
        if any(values.get(f) is not None for f in members)
    ]


def is_partial(accepted: list[Ext]) -> bool:
    """Some core fields missing / unreadable / inconsistent among the ACCEPTED measurements, or
    only history points were accepted."""
    full = [e for e in accepted if not e.from_history]
    if not full:
        return True
    return any(
        e.checks.get("invalid")
        or e.checks.get("errors")
        or any(e.values.get(f) is None for f in fields.REQUIRED_CORE)
        for e in full
    )


def outcome_status(exts_of_upload: list[Ext], decisions: dict[str, Decision]) -> str:
    accepted = [e for e in exts_of_upload if decisions[e.id].accepted]
    if not accepted:
        # a file that only completed an existing day is not "nothing new"
        return "done" if any(decisions[e.id].enriched for e in exts_of_upload) else "nothing_new"
    return "partial" if is_partial(accepted) else "done"


def upload_report(
    upload, exts: list[Ext], decisions: dict[str, Decision], uploads_by_id: dict
) -> dict:
    by_id = {e.id: e for e in exts}
    mine = sorted(
        (e for e in exts if e.upload_id == upload.id),
        key=lambda e: (e.measured_date, e.rank_in_day()),
    )
    items = []
    for e in mine:
        d = decisions[e.id]
        conflict = None
        other = by_id.get(d.conflicting_id) if d.conflicting_id else None
        if other is not None:
            up = uploads_by_id.get(other.upload_id)
            conflict = {
                "measurement_id": other.id,
                "upload_id": other.upload_id,
                "name": up.original_name if up else other.upload_id,
                "kind": up.kind if up else None,
                "origin": other.origin,
                "date": other.measured_date.isoformat(),
                "same_upload": other.upload_id == upload.id,
            }
        history = e.from_history
        items.append(
            {
                "extraction_id": e.id,
                "date": e.measured_date.isoformat(),
                "time": e.measured_time.strftime("%H:%M:%S" if e.measured_time.second else "%H:%M")
                if e.measured_time
                else None,
                "origin": e.origin,
                "from_history": history,
                "filled": _filled_groups(e.values),
                "missing_core": []
                if history
                else [f for f in fields.CORE if e.values.get(f) is None],
                "invalid": e.checks.get("invalid", []),
                "errors": e.checks.get("errors", []),
                "outcome": ACCEPTED if d.accepted else REJECTED,
                "reason": d.reason,
                "enriched": list(d.enriched),
                "conflict": conflict,
            }
        )
    accepted = [e for e in mine if decisions[e.id].accepted]
    first = mine[0].values if mine else {}
    return {
        "version": 3,
        "kind": upload.kind,
        "device": first.get("device_model"),
        "location": first.get("source_name"),
        "counts": {
            "total": len(items),
            "accepted": len(accepted),
            "rejected": len(items) - len(accepted),
        },
        "measurements": items,
        "partial": bool(accepted) and is_partial(accepted),
        "deleted": bool(getattr(upload, "deleted", False)),
    }
