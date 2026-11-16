"""The data pipeline -- ONE path for the web upload, the background worker and the CLI.
Binding spec with flowcharts: docs/upload-pipeline.md (sections 1-4).

    raw file --admit--> blob raw/<user>/<yyyy-mm>/<uuid>-<name> + `upload` row (pending)
             --process--> `extraction` rows (CSV: csv_parsers; PDF/JPG/PNG: Claude + sheet_schema)
             --refresh--> day check: `measurement` (daycheck.py) + per-upload reports + statuses

Blob is the source of truth; everything here is idempotent and re-derivable:
* `reprocess` re-runs `process` for chosen uploads. CSV is re-parsed from blob (free). A sheet
  reuses a cached Claude answer for the same file hash + `sheet_schema.SCHEMA_VERSION`, so
  reprocessing never pays Claude twice.
* `rebuild_index` recreates missing `upload` rows from the blob listing (user, id and name are
  in the path, the kind in the magic bytes); `reprocess` then restores the rest.
App state (users, sessions, shares, spend) is NOT derivable and is never touched here.

Deleting never deletes a row: uploads and measurements get deleted = 1 (+ when / by whom). ONE
blob per user + SHA-256 (`_sync_blob`): it sits in raw/ while a live upload references it and
MOVES to archive/ (copy + verify + delete source) when none does; uploading the same file again
moves it back and processes it like a first upload (from the cached Claude answer).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime

import config
import csv_parsers
import data
import daycheck
import db
import estimates
import extract
import fields
import notify
import prechecks
import sheet_schema
import storage
import uploads
from sqlalchemy import delete, false, or_, select, true, update

log = logging.getLogger("inbody.pipeline")

FINAL = (
    "done",
    "partial",
    "nothing_new",
    "needs_review",
    "failed",
    "skipped_budget",
    "duplicate_file",
)
RETRYABLE = ("failed", "skipped_budget")
_BLOB_RE = re.compile(
    r"^raw/(?P<user>[a-z0-9][a-z0-9_-]{1,31})/\d{4}-\d{2}/(?P<id>[0-9a-f-]{36})-(?P<name>.+)$"
)


# ---------------------------------------------------------------------------------- admission


def admit(
    user_id: str,
    filename: str,
    content: bytes,
    now: datetime | None = None,
    source_name: str | None = None,
) -> str:
    """Section 1. Returns the new upload id; raises uploads.Rejected(key) for a refusal.

    A file this user already uploaded (same SHA-256, upload not deleted) gets an upload row with
    status `duplicate_file` pointing at the earlier one -- nothing is stored or processed.
    A file whose earlier uploads are all DELETED is a normal new upload (last in the replay
    order); its archived blob moves back to raw/ instead of being stored a second time.

    The guest account never uploads (data.GuestReadOnly)."""
    data.refuse_guest(user_id)
    now = now or db.utcnow()
    with db.engine().connect() as conn:
        if data.uploads_today_count(conn, user_id, now) >= config.UPLOADS_PER_USER_PER_DAY:
            raise uploads.Rejected("upload.err.daily")
    kind = uploads.detect_kind(content)  # size limits per kind + type by content
    digest = hashlib.sha256(content).hexdigest()
    upload_id = str(uuid.uuid4())
    original = (filename or "upload")[:255]
    with db.engine().begin() as conn:
        earlier = data.find_upload_by_hash(conn, user_id, digest)
        if earlier is not None:
            _insert_upload(
                conn,
                upload_id,
                user_id,
                kind,
                original,
                earlier.blob_path,
                digest,
                len(content),
                now,
                status="duplicate_file",
                duplicate_of=earlier.id,
            )
            _set(conn, upload_id, phase="end", phase_started_at=now)
            refresh_user(conn, user_id)
            return upload_id
        archived = _blob_of(conn, user_id, digest)  # a deleted upload's file, or None
    started = time.monotonic()
    path = archived or storage.blob_path(user_id, upload_id, original, now)
    if archived is None:
        storage.store().put(path, content, uploads.CONTENT_TYPES[kind])
    with db.engine().begin() as conn:
        _insert_upload(
            conn,
            upload_id,
            user_id,
            kind,
            original,
            path,
            digest,
            len(content),
            now,
            source_name=source_name,
        )
        _set(
            conn,
            upload_id,
            pages=estimates.pdf_pages(content) if kind == "pdf" else None,
            store_ms=_ms(started),
            phase="parse",  # phase 1 is over once the row exists; waiting counts as parsing
            phase_started_at=db.utcnow(),
        )
    if archived is not None:
        try:  # best effort here; process_upload moves it (or fails the upload) if this did not
            with db.engine().begin() as conn:
                _sync_blob(conn, user_id, digest)
        except Exception:
            log.warning("upload %s: moving the stored file back failed", upload_id, exc_info=True)
    return upload_id


def _blob_of(conn, user_id: str, digest: str) -> str | None:
    """The blob of this user's (deleted) earlier uploads of the same content, if any."""
    return conn.execute(
        select(db.upload.c.blob_path)
        .where(db.upload.c.user_id == user_id, db.upload.c.sha256 == digest)
        .order_by(db.upload.c.created_at.desc())
        .limit(1)
    ).scalar()


def _sync_blob(conn, user_id: str, digest: str, actor: str | None = None) -> None:
    """ONE blob per user + SHA-256, shared by all their upload rows of that content (deleted,
    duplicate or live). It belongs in raw/ while a live (not deleted, not duplicate_file) upload
    references it, in archive/ otherwise. Moves it if needed (copy + verify + delete source, so
    a failure half-way keeps the source) and points every row at the new place. Idempotent."""
    u = db.upload
    rows = conn.execute(
        select(u.c.id, u.c.blob_path, u.c.deleted, u.c.status).where(
            u.c.user_id == user_id, u.c.sha256 == digest
        )
    ).all()
    groups: dict[str, list] = {}  # legacy data may hold several blobs of one content
    for r in rows:
        groups.setdefault(storage.raw_path(r.blob_path), []).append(r)
    now = db.utcnow()
    for raw, members in groups.items():
        live = any(not r.deleted and r.status != "duplicate_file" for r in members)
        target = raw if live else storage.archive_path(raw)
        if all(r.blob_path == target for r in members):
            continue
        source = storage.archive_path(raw) if live else raw
        storage.move(storage.store(), source, target)
        conn.execute(
            update(u)
            .where(u.c.id.in_([r.id for r in members]))
            .values(
                blob_path=target,
                archived_at=None if live else now,
                archived_by=None if live else (actor or "")[:64] or None,
                updated_at=now,
            )
        )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _insert_upload(
    conn,
    upload_id,
    user_id,
    kind,
    original,
    path,
    digest,
    size,
    created_at,
    status="pending",
    duplicate_of=None,
    source_name=None,
) -> None:
    conn.execute(
        db.upload.insert().values(
            id=upload_id,
            user_id=user_id,
            kind=kind,
            original_name=original,
            blob_path=path,
            size_bytes=size,
            sha256=digest,
            status=status,
            duplicate_of=duplicate_of,
            source_name=(source_name or "").strip()[:128] or None,
            created_at=created_at,
            updated_at=created_at,
        )
    )


# ------------------------------------------------------------------------------------ process


class _Failed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _set(conn, upload_id: str, **values) -> None:
    conn.execute(
        update(db.upload)
        .where(db.upload.c.id == upload_id)
        .values(updated_at=db.utcnow(), **values)
    )


def _cached_answer(conn, digest: str) -> str | None:
    """A Claude answer for the same file content and the current schema version, if any --
    deleted uploads included, so re-uploading a deleted sheet never pays twice."""
    return conn.execute(
        select(db.upload.c.llm_json)
        .where(
            db.upload.c.sha256 == digest,
            db.upload.c.llm_schema_version == sheet_schema.SCHEMA_VERSION,
            db.upload.c.llm_json.is_not(None),
        )
        .order_by(db.upload.c.created_at)
        .limit(1)
    ).scalar()


def _blob_in_place(row):
    """Before reading the file: make sure it is in raw/ (a re-upload of a deleted file whose
    move back failed in `admit`). A failure is a retryable `failed` upload; the source stays."""
    try:
        with db.engine().begin() as conn:
            _sync_blob(conn, row.user_id, row.sha256)
            return conn.execute(select(db.upload).where(db.upload.c.id == row.id)).first()
    except Exception as exc:
        log.warning("upload %s: the stored file could not be moved into place", row.id)
        raise _Failed("storage", "the stored file could not be moved into place") from exc


def _derive(row, client) -> tuple[list[fields.Extracted], str]:
    """Section 2: `is_csv -> csv_path (no LLM)`, else `llm_path`."""
    raw = storage.store().get(row.blob_path)
    if row.kind == "csv":
        return _csv_path(raw)
    if row.kind in uploads.SHEET_KINDS:
        return _llm_path(row, raw, client)
    raise _Failed("kind", "unknown kind")


def _csv_path(raw: bytes) -> tuple[list[fields.Extracted], str]:
    """Deterministic parser: no LLM, no spend."""
    text = uploads.csv_text(raw)
    if text is None:
        raise _Failed("csv_invalid", "not a UTF-8 CSV with a 'date' header")
    try:
        rows = csv_parsers.parse_csv(text)
    except csv_parsers.CsvNeedsReview as exc:
        raise _Review(exc.codes, exc.details) from exc
    except csv_parsers.CsvNotSupported as exc:
        raise _Failed("csv_format", str(exc)) from exc
    try:
        return [fields.checked(r) for r in rows], csv_parsers.PARSER_VERSION
    except ValueError as exc:
        raise _Failed("csv_format", str(exc)) from exc


def _llm_path(row, raw: bytes, client) -> tuple[list[fields.Extracted], str]:
    """cache lookup -> pre_checks -> budget -> call -> post_checks."""
    with db.engine().connect() as conn:
        cached = _cached_answer(conn, row.sha256)  # same file hash + schema version
    if cached is not None:
        payload = json.loads(cached)  # no API call, no spend
    else:
        try:
            prechecks.check(row.kind, raw)  # pages / pixels / opens -- before any spend
        except prechecks.PrecheckFailed as exc:
            raise _Failed(f"precheck_{exc.code}", str(exc)) from exc
        try:  # budget -> call (max 1 retry, each budget-checked) -> valid JSON + InBody sheet
            payload = extract.call_claude(
                raw,
                uploads.CONTENT_TYPES[row.kind],
                user_id=row.user_id,
                upload_id=row.id,
                client=client,
            )
        except extract.NotInBodySheet as exc:
            raise _Failed("not_inbody", str(exc)) from exc
        except extract.ExtractionFailed as exc:
            raise _Failed("extraction", str(exc)) from exc
    with db.engine().begin() as conn:  # keep what we paid for, keyed by hash + version
        _set(
            conn,
            row.id,
            llm_json=json.dumps(payload),
            llm_schema_version=sheet_schema.SCHEMA_VERSION,
        )
    return rows_from_wire(payload, row.source_name), sheet_schema.SCHEMA_VERSION


class _Review(Exception):
    """The format does not look as expected (docs/upload-pipeline.md CV / SV): `needs_review`.
    `.codes` are value-free (safe for the push); `.details` may quote values (admin page only);
    `.exts` is the parser output, stored but held out of the day check until approved."""

    def __init__(self, codes: list[str], details: list[str], exts: list | None = None) -> None:
        super().__init__(", ".join(codes))
        self.codes, self.details, self.exts = codes, details, exts or []


def rows_from_wire(payload: dict, form_source_name: str | None = None) -> list[fields.Extracted]:
    """post_checks: wire schema -> canonical rows (validate.normalize) -> the form's gym name
    where the sheet prints none -> per-row sanity -> the SV checks. Anything unexpected raises
    _Review: nothing is guessed, nothing reaches `measurement` until Vojta approves."""
    try:
        rows, problems, wire_errors = sheet_schema.normalize(payload)
    except sheet_schema.NotInBodySheet as exc:
        raise _Failed("not_inbody", str(exc)) from exc
    except sheet_schema.SheetInvalid as exc:
        raise _Failed("extraction", str(exc)) from exc
    sheet = payload["sheet"]
    codes: list[str] = []
    details: list[str] = []

    def flag(code: str, detail: str) -> None:
        if code not in codes:
            codes.append(code)
        details.append(f"{code}: {detail}")

    if not sheet["measured_at_local"]:
        flag("sheet_no_date", "no test date found")
    if (
        sheet["language"] not in sheet_schema.KNOWN_LANGUAGES
        or sheet["date_order"] == "unknown"
        or sheet["capture"] == "unknown"
    ):
        flag(
            "sheet_unknown_layout",
            f"language={sheet['language']} date_order={sheet['date_order']} "
            f"capture={sheet['capture']}",
        )
    for p in problems:
        flag("sheet_unexpected_values", p)
    for e in wire_errors:
        flag("sheet_history_dates", e)
    for w in payload["warnings"]:
        if w["code"] in sheet_schema.REVIEW_WARNING_CODES and not sheet_schema.non_critical(
            w["field"]
        ):
            flag("sheet_model_warning", f"{w['code']} {w['field']}: {w['note']}")
    out = []
    for r in rows:
        if not r.get("measured_date"):
            continue
        # The sheet's history strip is CHECKED (sheet_history_dates above) but never stored: its
        # partial points would take a day by first-come-wins and block a later full CSV row
        # (user decision 2026-11-16).
        if r.get("from_history"):
            continue
        r = dict(r)
        if not r.get("source_name") and form_source_name:
            r["source_name"] = form_source_name
        try:
            e = fields.checked(r)
        except ValueError as exc:
            flag("sheet_no_date", str(exc))
            continue
        for err in e.errors:
            flag("sheet_inconsistent", f"{r['measured_date']}: {err}")
        for f in e.invalid:
            if f in fields.CORE:
                flag("sheet_core_unreadable", f"{r['measured_date']}: {f}")
        out.append(e)
    if codes:
        raise _Review(codes, details, out)
    return out


def parse_sheet(
    content: bytes,
    mime: str,
    *,
    user_id: str,
    upload_id: str | None = None,
    client=None,
    source_name: str | None = None,
) -> list[fields.Extracted]:
    """The spec's `parse_sheet(bytes, mime) -> list[Measurement]` for one sheet, without an
    upload row: pre-checks -> budget -> Claude -> post-checks. Raises _Failed /
    extract.BudgetExceeded. Used by scripts/live_extract_check.py."""
    kind = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[mime]
    try:
        prechecks.check(kind, content)
    except prechecks.PrecheckFailed as exc:
        raise _Failed(f"precheck_{exc.code}", str(exc)) from exc
    try:
        payload = extract.call_claude(
            content, mime, user_id=user_id, upload_id=upload_id, client=client
        )
    except extract.NotInBodySheet as exc:
        raise _Failed("not_inbody", str(exc)) from exc
    except extract.ExtractionFailed as exc:
        raise _Failed("extraction", str(exc)) from exc
    return rows_from_wire(payload, source_name)


def process_upload(upload_id: str, client=None) -> str:
    """Section 2 + 3. Derive this upload's extractions, then re-run the user's day check and
    reports. Returns the final status (done / partial / nothing_new / failed / skipped_budget);
    never raises for a bad file, an API error or a budget."""
    with db.engine().connect() as conn:
        row = conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()
    if row is None:
        return "missing"
    if row.status == "duplicate_file" or row.deleted or data.is_guest_id(row.user_id):
        # never processed (section 1) / the owner deleted it / the guest's seeded sample rows
        # have no blob and no Claude answer to re-derive from (scripts/seed_guest.py owns them)
        return row.status
    cache_hit = False
    if row.kind in uploads.SHEET_KINDS:
        with db.engine().connect() as conn:
            cache_hit = _cached_answer(conn, row.sha256) is not None
    with db.engine().begin() as conn:
        _set(
            conn,
            upload_id,
            status="processing",
            error=None,
            error_code=None,
            phase="parse",
            phase_started_at=db.utcnow(),
            cache_hit=cache_hit,
        )
    status, code, error, exts, version, review = "done", None, None, [], "", None
    started = time.monotonic()
    try:
        row = _blob_in_place(row)
        exts, version = _derive(row, client)
        status = "parsed"  # done / partial / nothing_new is decided by the day check below
    except extract.BudgetExceeded as exc:
        log.warning("upload %s skipped: %s", upload_id, exc)
        status, code = "skipped_budget", exc.kind  # quota_exhausted | global_budget
        error = (
            "your monthly limit is used up"
            if exc.kind == "quota_exhausted"
            else "the site's monthly AI budget is used up"
        )
    except _Review as exc:  # "Be defensive": hold everything, ask Vojta
        status, code, error = "needs_review", "needs_review", ", ".join(exc.codes)
        exts, review = exc.exts, {"codes": exc.codes, "details": exc.details[:50]}
        version = exts[0].row.get("parser_version", "") if exts else ""
    except _Failed as exc:
        status, code, error = "failed", exc.code, str(exc)
    except Exception as exc:  # outage, storage error, bug: a status, not a crash
        log.exception("upload %s failed", upload_id)
        status, code, error = "failed", "error", f"processing error ({type(exc).__name__})"

    parse_ms = _ms(started)
    if status in ("failed", "skipped_budget", "needs_review"):  # the bar stops in phase 2
        with db.engine().begin() as conn:
            _set(conn, upload_id, parse_ms=parse_ms if status == "failed" else None)
    else:
        with db.engine().begin() as conn:
            _set(conn, upload_id, parse_ms=parse_ms, phase="save", phase_started_at=db.utcnow())
    started = time.monotonic()
    with db.engine().begin() as conn:
        gone = _deleted_marks(conn, upload_id)  # the owner's deletes survive a re-derivation
        if status != "skipped_budget":  # a skipped upload keeps whatever it had before
            conn.execute(delete(db.extraction).where(db.extraction.c.upload_id == upload_id))
        now = db.utcnow()
        for n, e in enumerate(exts if status in ("parsed", "needs_review") else []):  # review: held
            values = extraction_values(e, upload_id, row.user_id, version, now, n)
            values["deleted_at"] = gone.get(values["id"])
            conn.execute(db.extraction.insert().values(**values))
        done = status == "parsed"
        _set(
            conn,
            upload_id,
            status="done" if done else status,  # refined to done/partial/nothing_new below
            error=error and error[:500],
            error_code=code,
            review_json=json.dumps(review, sort_keys=True) if review else None,
            phase="end" if done else "parse",
        )
        refresh_user(conn, row.user_id)  # day check, statuses, reports
        if done:
            _set(conn, upload_id, save_ms=_ms(started))
        final = conn.execute(select(db.upload.c.status).where(db.upload.c.id == upload_id)).scalar()
    if final in notify.NOTIFY_STATUSES:
        # value-free reason: the review codes, or the error CODE (an error text may quote values)
        reason = error if final == "needs_review" else code
        notify.upload_event(upload_id, row.user_id, final, reason)  # fire-and-forget
    return final


def _deleted_marks(conn, upload_id: str) -> dict:
    """{extraction id: deleted_at} of the measurements the owner deleted from this upload: the
    extraction marks, plus the kept deleted `measurement` rows (ids are deterministic, so they
    still match after a failed re-derivation dropped the extractions)."""
    x, m = db.extraction, db.measurement
    marks = dict(
        conn.execute(
            select(m.c.id, m.c.deleted_at).where(m.c.upload_id == upload_id, m.c.deleted == true())
        ).all()
    )
    marks.update(
        conn.execute(
            select(x.c.id, x.c.deleted_at).where(
                x.c.upload_id == upload_id, x.c.deleted_at.is_not(None)
            )
        ).all()
    )
    return marks


_EXTRACTION_NS = uuid.UUID("5b0c7d1e-9a43-4c7e-8f0e-1d6a2f3b4c5d")


def extraction_id(upload_id: str, e: fields.Extracted, n: int) -> str:
    """Deterministic: the same upload re-derived gives the same ids, so measurement ids (= the
    accepted extraction's id) and conflicting_measurement_id survive a reprocess."""
    key = f"{upload_id}|{n}|{e.row['origin']}|{e.measured_date}|{e.measured_time}"
    return str(uuid.uuid5(_EXTRACTION_NS, key))


def extraction_values(
    e: fields.Extracted, upload_id: str, user_id: str, version: str, now, n: int = 0
) -> dict:
    r = e.row
    return {
        "id": extraction_id(upload_id, e, n),
        "upload_id": upload_id,
        "user_id": user_id,
        "measured_date": e.measured_date,
        "measured_time": e.measured_time,
        "origin": r["origin"],
        "from_history": e.from_history,
        "parser_version": r.get("parser_version") or version,
        **{k: r.get(k) for k in fields.SOURCE_META},
        **{k: r.get(k) for k in fields.FIELDS},
        **{
            k: json.dumps(r.get(k), sort_keys=True) if r.get(k) is not None else None
            for k in fields.JSON_COLUMNS
        },
        "checks_json": json.dumps(
            {"invalid": e.invalid, "errors": e.errors, "warnings": e.check_warnings}, sort_keys=True
        ),
        "accepted": False,  # decided by daycheck.rebuild_user
        "created_at": now,
    }


PROCESSED = ("done", "partial", "nothing_new")


def refresh_user(conn, user_id: str) -> None:
    """Replay the user's day check, then every processed upload's status + report."""
    exts, decisions = daycheck.rebuild_user(conn, user_id)
    rows = conn.execute(select(db.upload).where(db.upload.c.user_id == user_id)).all()
    by_id = {r.id: r for r in rows}
    for r in rows:
        values = {}
        if r.status in PROCESSED:
            mine = [e for e in exts if e.upload_id == r.id]
            values["status"] = daycheck.outcome_status(mine, decisions)
            report = daycheck.upload_report(r, exts, decisions, by_id)
        else:
            report = {
                "version": 3,
                "kind": r.kind,
                "counts": {"total": 0},
                "measurements": [],
                "partial": False,
            }
        report["status"] = values.get("status", r.status)
        report["error_code"] = r.error_code
        if r.status == "duplicate_file" and r.duplicate_of in by_id:
            first = by_id[r.duplicate_of]
            report["duplicate_of"] = {
                "upload_id": first.id,
                "name": first.original_name,
                "created_at": first.created_at.isoformat(timespec="minutes"),
            }
        _set(conn, r.id, report_json=json.dumps(report, sort_keys=True), **values)


# ------------------------------------------------------------------------------------- delete


def _actor(editor) -> str:
    return editor if isinstance(editor, str) else editor.id


def _mark_upload_deleted(conn, upload_id: str, actor: str, now) -> None:
    """Flag the upload, its duplicate_file rows and its live measurements deleted. No row goes."""
    u, m = db.upload, db.measurement
    conn.execute(
        update(u)
        .where(or_(u.c.id == upload_id, u.c.duplicate_of == upload_id), u.c.deleted == false())
        .values(deleted=True, deleted_at=now, deleted_by=actor[:64], updated_at=now)
    )
    conn.execute(
        update(m)
        .where(m.c.upload_id == upload_id, m.c.deleted == false())
        .values(deleted=True, deleted_at=now, deleted_by=actor[:64])
    )


def delete_measurement(conn, editor, measurement_id: str) -> str:
    """The owner (or admin) deletes one day's measurement: the measurement row and its extraction
    are flagged deleted and the user's days are re-decided (the next upload in line may take the
    day). If the upload then has NO live measurement left, the upload is deleted too and its file
    archived; otherwise (e.g. one day of a 10-day CSV) the file stays in raw/. Returns the owner
    id. Raises data.NotFound unless can_edit (never a shared viewer)."""
    m = db.measurement
    data.refuse_guest(editor)
    row = conn.execute(select(m).where(m.c.id == measurement_id, m.c.deleted == false())).first()
    if row is not None:
        data.refuse_guest(row.user_id)  # the sample data is read-only for everyone
    if row is None or not data.can_edit(conn, editor, row.user_id):
        raise data.NotFound(measurement_id)
    actor, now = _actor(editor)[:64], db.utcnow()
    conn.execute(
        update(db.extraction).where(db.extraction.c.id == measurement_id).values(deleted_at=now)
    )
    conn.execute(
        update(m)
        .where(m.c.id == measurement_id)
        .values(deleted=True, deleted_at=now, deleted_by=actor)
    )
    refresh_user(conn, row.user_id)
    left = conn.execute(
        select(m.c.id).where(m.c.upload_id == row.upload_id, m.c.deleted == false())
    ).first()
    if left is None:
        _delete_upload_row(conn, row.upload_id, actor, now)
    return row.user_id


def delete_upload(conn, editor, upload_id: str) -> str:
    """The owner (or admin) deletes an upload: it and its measurements are flagged deleted (rows
    stay for the record), the day check is replayed, the file moves to archive/."""
    data.refuse_guest(editor)
    row = conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()
    if row is not None:
        data.refuse_guest(row.user_id)
    if row is None or not data.can_edit(conn, editor, row.user_id):
        raise data.NotFound(upload_id)
    if not row.deleted:  # deleting twice (a double click) changes nothing
        _delete_upload_row(conn, upload_id, _actor(editor)[:64], db.utcnow())
    return row.user_id


def _delete_upload_row(conn, upload_id: str, actor: str, now) -> None:
    row = conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()
    _mark_upload_deleted(conn, upload_id, actor, now)
    refresh_user(conn, row.user_id)
    _sync_blob(conn, row.user_id, row.sha256, actor)  # no live upload left -> archive/


def restore_upload(upload_id: str) -> str:
    """Backend only (scripts/pipeline.py restore): un-delete an upload for a mistake -- clear the
    delete flags of the upload and its extractions, move the file back to raw/, replay the day
    check (its measurement rows come back where the days are still free). Refused while another
    live upload of the same file exists (one live upload per file). Returns the new status."""
    u = db.upload
    with db.engine().begin() as conn:
        row = conn.execute(select(u).where(u.c.id == upload_id)).first()
        if row is None:
            raise data.NotFound(upload_id)
        data.refuse_guest(row.user_id)  # the sample rows have no blob to restore
        other = conn.execute(
            select(u.c.id).where(
                u.c.user_id == row.user_id,
                u.c.sha256 == row.sha256,
                u.c.id != upload_id,
                u.c.deleted == false(),
                u.c.status != "duplicate_file",
            )
        ).scalar()
        if other is not None:
            raise ValueError(f"upload {other} of the same file is live; delete it first")
        _set(conn, upload_id, deleted=False, deleted_at=None, deleted_by=None)
        conn.execute(
            update(db.extraction)
            .where(db.extraction.c.upload_id == upload_id)
            .values(deleted_at=None)
        )
        _sync_blob(conn, row.user_id, row.sha256)
        refresh_user(conn, row.user_id)
        return conn.execute(select(u.c.status).where(u.c.id == upload_id)).scalar()


# ----------------------------------------------------------------------- retry / reprocess / index


def mark_for_retry(conn, owner_id: str, upload_id: str) -> bool:
    """The owner's "try again" (failed / skipped_budget only). Returns True if re-queued."""
    data.refuse_guest(owner_id)
    return bool(
        conn.execute(
            update(db.upload)
            .where(
                db.upload.c.id == upload_id,
                db.upload.c.user_id == owner_id,
                db.upload.c.status.in_(RETRYABLE),
            )
            .values(status="pending", updated_at=db.utcnow())
        ).rowcount
    )


def select_uploads(
    *, upload_id: str | None = None, user_id: str | None = None, all_: bool = False
) -> list[str]:
    q = (
        select(db.upload.c.id)
        .where(
            db.upload.c.status != "duplicate_file",
            db.upload.c.deleted == false(),  # deleted data never comes back by itself
            db.upload.c.user_id != config.GUEST_USER_ID,  # seeded sample rows have no blob
        )
        .order_by(db.upload.c.created_at, db.upload.c.id)
    )
    if upload_id:
        q = q.where(db.upload.c.id == upload_id)
    elif user_id:
        q = q.where(db.upload.c.user_id == user_id)
    elif not all_:
        raise ValueError("choose an upload, a user, or all")
    with db.engine().connect() as conn:
        return list(conn.execute(q).scalars())


def reprocess(ids: list[str], client=None) -> dict[str, str]:
    return {upload_id: process_upload(upload_id, client) for upload_id in ids}


def rebuild_index() -> list[str]:
    """Insert `upload` rows for blobs that have none. Returns the new upload ids (pending)."""
    created = []
    for path, created_at in storage.store().list_raw():
        m = _BLOB_RE.match(path)
        if not m:
            log.warning("skipping unexpected blob %s", path)
            continue
        with db.engine().begin() as conn:
            if conn.execute(select(db.upload.c.id).where(db.upload.c.id == m["id"])).first():
                continue
            if data.is_guest_id(m["user"]):
                continue  # the guest's data is seeded, never rebuilt from blobs
            if not data.user_exists(conn, m["user"]):
                log.warning("blob %s belongs to unknown user %s", path, m["user"])
                continue
            content = storage.store().get(path)
            try:
                kind = uploads.detect_kind(content)
            except uploads.Rejected:
                log.warning("blob %s is not a supported file", path)
                continue
            _insert_upload(
                conn,
                m["id"],
                m["user"],
                kind,
                m["name"],
                path,
                hashlib.sha256(content).hexdigest(),
                len(content),
                created_at,
            )
            created.append(m["id"])
    return created
