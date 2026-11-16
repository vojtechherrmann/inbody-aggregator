"""Deleting (docs/upload-pipeline.md, "Deleting"): SQL rows are only FLAGGED (`deleted = 1`), never
deleted; the raw file MOVES raw/ -> archive/ once no live upload references it, and moves back
when the same file is uploaded again (one blob per SHA-256 per user). A re-upload of a deleted
file is processed like a first upload, from the cached Claude answer (no API call, no spend)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import db
import pipeline
import pytest
import storage
from conftest import post
from helpers import FakeClient, csv_export, jpeg, wire
from security import User
from sqlalchemy import func, select

TEN_DAYS = [(f"202606{d:02d}101010", 190.0 - d) for d in range(1, 11)]


def _upload(upload_id):
    with db.engine().connect() as conn:
        return conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()


def _live(user="vojta"):
    """The measurements users see: live rows only."""
    with db.engine().connect() as conn:
        return conn.execute(
            select(db.measurement)
            .where(db.measurement.c.user_id == user, db.measurement.c.deleted == False)  # noqa: E712
            .order_by(db.measurement.c.measured_date)
        ).all()


def _all_measurements(user="vojta"):
    with db.engine().connect() as conn:
        return conn.execute(
            select(db.measurement)
            .where(db.measurement.c.user_id == user)
            .order_by(db.measurement.c.measured_date, db.measurement.c.created_at)
        ).all()


def _count(table):
    with db.engine().connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _spent():
    with db.engine().connect() as conn:
        return conn.execute(select(db.api_usage)).all()


def _import(rows=TEN_DAYS, name="ten.csv"):
    content = csv_export(rows)
    upload_id = pipeline.admit("vojta", name, content)
    pipeline.process_upload(upload_id)
    return upload_id, content


def _sheet(name="a.jpg", when="2026-03-12T06:15", client=None):
    content = jpeg(tag=b"sheet-a")
    upload_id = pipeline.admit("vojta", name, content)
    status = pipeline.process_upload(upload_id, client or FakeClient(wire(when=when)))
    return upload_id, content, status


def _delete_measurement(measurement_id, who="vojta"):
    with db.engine().begin() as conn:
        pipeline.delete_measurement(conn, User(who, who == "vojta-admin"), measurement_id)


def _delete_upload(upload_id, who="vojta"):
    with db.engine().begin() as conn:
        pipeline.delete_upload(conn, User(who, who == "vojta-admin"), upload_id)


def _exists(path):
    return storage.store().exists(path)


def _copies(content) -> list[str]:
    """Every stored blob with exactly these bytes (raw/ and archive/)."""
    root = storage.store().root
    digest = hashlib.sha256(content).digest()
    return sorted(
        p.relative_to(root).as_posix()
        for p in Path(root).rglob("*")
        if p.is_file() and hashlib.sha256(p.read_bytes()).digest() == digest
    )


# ------------------------------------------------------------------------------ flags, not deletes


def test_deleting_one_of_ten_csv_days_flags_the_measurement_and_keeps_the_file_in_raw():
    upload_id, _ = _import()
    raw = _upload(upload_id).blob_path
    victim = _live()[3]
    _delete_measurement(victim.id)
    assert len(_live()) == 9
    kept = [m for m in _all_measurements() if m.id == victim.id]
    assert len(kept) == 1 and kept[0].deleted and kept[0].deleted_by == "vojta"
    assert kept[0].deleted_at is not None and kept[0].weight_kg == victim.weight_kg
    row = _upload(upload_id)
    assert not row.deleted and row.archived_at is None and row.blob_path == raw and _exists(raw)


def test_deleting_the_last_remaining_day_deletes_the_upload_and_archives_the_file():
    upload_id, content = _import(TEN_DAYS[:2])
    raw = _upload(upload_id).blob_path
    _delete_measurement(_live()[0].id)
    assert not _upload(upload_id).deleted and _upload(upload_id).archived_at is None
    _delete_measurement(_live()[0].id)
    row = _upload(upload_id)
    assert _live() == [] and len(_all_measurements()) == 2  # both rows kept, flagged
    assert row.deleted and row.deleted_by == "vojta" and row.deleted_at is not None
    assert row.archived_at is not None and row.archived_by == "vojta"
    assert row.blob_path == storage.archive_path(raw) and row.blob_path.startswith("archive/vojta/")
    assert not _exists(raw) and _copies(content) == [row.blob_path]  # moved, not lost


def test_deleting_an_upload_deletes_no_sql_row():
    upload_id, content = _import()
    counts = {t.name: _count(t) for t in (db.upload, db.extraction, db.measurement)}
    _delete_upload(upload_id, who="vojta-admin")
    assert {t.name: _count(t) for t in (db.upload, db.extraction, db.measurement)} == counts
    row = _upload(upload_id)
    assert _live() == [] and row.deleted and row.deleted_by == "vojta-admin"
    assert all(m.deleted and m.deleted_by == "vojta-admin" for m in _all_measurements())
    assert row.blob_path.startswith("archive/") and _copies(content) == [row.blob_path]


def test_a_deleted_day_is_free_again_for_the_next_upload():
    upload_id, _ = _import(TEN_DAYS[:1])
    _delete_upload(upload_id)
    other, _ = _import([("20260601080000", 150.0)], name="other.csv")
    assert [m.upload_id for m in _live()] == [other] and _upload(other).status == "done"
    assert len(_all_measurements()) == 2  # the deleted one is kept next to the live one


def test_reprocess_keeps_a_deleted_day_deleted():
    _import()
    victim = _live()[3]
    _delete_measurement(victim.id)
    pipeline.reprocess(pipeline.select_uploads(all_=True))
    assert len(_live()) == 9 and victim.id not in {m.id for m in _live()}


# --------------------------------------------------------------------------------- re-upload


def test_reupload_of_a_deleted_sheet_is_a_normal_first_upload_from_the_cache():
    first, content, status = _sheet()
    assert status == "done" and len(_spent()) == 1
    path = _upload(first).blob_path
    _delete_upload(first)
    assert _upload(first).blob_path == storage.archive_path(path)

    no_api = FakeClient()  # any call would pop from an empty list and fail
    again = pipeline.admit("vojta", "a.jpg", content)
    assert pipeline.process_upload(again, no_api) == "done"
    assert no_api.calls == [] and no_api.counted == 0 and len(_spent()) == 1  # no call, no spend

    new, old = _upload(again), _upload(first)
    assert new.created_at >= old.created_at  # last in the replay order
    assert new.blob_path == path and _copies(content) == [path]  # moved back, not stored again
    assert old.deleted and old.blob_path == path and old.archived_at is None
    assert [m.upload_id for m in _live()] == [again]
    report = json.loads(new.report_json)
    assert report["counts"] == {"total": 1, "accepted": 1, "rejected": 0}
    assert report["measurements"][0]["conflict"] is None and "duplicate_of" not in report
    assert new.duplicate_of is None and not report["deleted"]


def test_reupload_page_never_mentions_cache_earlier_upload_or_archive(as_user):
    first, content, _ = _sheet()
    _delete_upload(first)
    vojta = as_user("vojta")
    for lang in ("cs", "en"):
        post(vojta, "/lang", data={"lang": lang, "next": "/"})
        resp = post(vojta, "/upload", files={"file": ("a.jpg", content)})
        again = resp.headers["location"].rsplit("/", 1)[1]
        pipeline.process_upload(again, FakeClient())
        assert first not in vojta.get(f"/uploads/{again}").text
        live = vojta.get(f"/uploads/{again}/live").text.lower()  # the visible status + report
        for word in (
            "cache",
            "archiv",
            "earlier",
            "already",
            "dříve",
            "už nahr",
            "smazáno",
            "smazán.",
            "was deleted",
        ):
            assert word not in live, (lang, word)
        _delete_upload(again)


def test_delete_reupload_delete_reupload_keeps_one_blob_where_it_belongs():
    content = csv_export(TEN_DAYS[:3])
    ids = []
    for _ in range(2):
        upload_id = pipeline.admit("vojta", "x.csv", content)
        assert pipeline.process_upload(upload_id) == "done"
        ids.append(upload_id)
        path = _upload(upload_id).blob_path
        assert path.startswith("raw/") and _copies(content) == [path] and len(_live()) == 3
        _delete_upload(upload_id)
        path = _upload(upload_id).blob_path
        assert path.startswith("archive/") and _copies(content) == [path] and _live() == []
    upload_id = pipeline.admit("vojta", "x.csv", content)
    assert pipeline.process_upload(upload_id) == "done" and len(_live()) == 3
    assert _copies(content) == [_upload(upload_id).blob_path]
    assert all(_upload(i).deleted for i in ids) and not _upload(upload_id).deleted
    assert {_upload(i).blob_path for i in [*ids, upload_id]} == {_upload(upload_id).blob_path}
    assert len(_all_measurements()) == 9  # 3 live + 2 x 3 deleted, nothing hard-deleted


def test_identical_file_with_a_live_upload_is_still_a_duplicate():
    upload_id, content = _import()
    again = pipeline.admit("vojta", "ten.csv", content)
    row = _upload(again)
    assert row.status == "duplicate_file" and row.duplicate_of == upload_id


def test_deleting_an_upload_also_deletes_its_duplicate_rows():
    upload_id, content = _import()
    dup = pipeline.admit("vojta", "ten.csv", content)
    _delete_upload(upload_id)
    assert _upload(dup).deleted and _upload(dup).blob_path.startswith("archive/")


def test_move_back_failing_half_way_keeps_the_source_and_fails_sanely(monkeypatch):
    first, content, _ = _sheet()
    _delete_upload(first)
    archived = _upload(first).blob_path
    store = storage.store()
    real_get = store.get
    # the copy lands, but verifying it fails: the archived source must stay
    monkeypatch.setattr(store, "get", lambda p: b"corrupt" if p.startswith("raw/") else real_get(p))
    again = pipeline.admit("vojta", "a.jpg", content)
    assert pipeline.process_upload(again, FakeClient()) == "failed"
    assert real_get(archived) == content  # source intact
    row = _upload(again)
    assert row.error_code == "storage" and not row.deleted and _upload(first).deleted
    assert _live() == [] and len(_spent()) == 1

    monkeypatch.setattr(store, "get", real_get)  # storage healthy again: "try again" works
    with db.engine().begin() as conn:
        assert pipeline.mark_for_retry(conn, "vojta", again)
    assert pipeline.process_upload(again, FakeClient()) == "done"
    assert _copies(content) == [_upload(again).blob_path] and len(_spent()) == 1


# ------------------------------------------------------------------- reprocess / index / restore


def test_reprocess_and_rebuild_index_ignore_deleted_uploads():
    upload_id, _ = _import()
    _delete_upload(upload_id)
    assert upload_id not in pipeline.select_uploads(all_=True)
    pipeline.reprocess(pipeline.select_uploads(all_=True))
    with db.engine().begin() as conn:  # lose the index entirely
        for t in (db.measurement, db.extraction, db.upload):
            conn.execute(t.delete())
    assert pipeline.rebuild_index() == []  # only raw/ is listed
    assert _live() == []


def test_restore_undeletes_moves_back_and_replays():
    upload_id, content = _import()
    ids = sorted(m.id for m in _live())
    _delete_upload(upload_id)
    assert pipeline.restore_upload(upload_id) == "done"
    row = _upload(upload_id)
    assert not row.deleted and row.deleted_at is None and row.deleted_by is None
    assert row.archived_at is None and row.blob_path.startswith("raw/")
    assert _copies(content) == [row.blob_path]
    assert sorted(m.id for m in _live()) == ids and len(_all_measurements()) == 10  # revived


def test_restore_refuses_while_a_reupload_of_the_same_file_is_live():
    upload_id, content = _import()
    _delete_upload(upload_id)
    again = pipeline.admit("vojta", "ten.csv", content)
    pipeline.process_upload(again)
    with pytest.raises(ValueError):
        pipeline.restore_upload(upload_id)
    assert _upload(upload_id).deleted and not _upload(again).deleted


def test_move_is_idempotent_and_keeps_the_source_if_verification_fails(monkeypatch):
    store = storage.store()
    store.put("raw/vojta/2026-11/x-a.csv", b"abc", "text/csv")
    storage.move(store, "raw/vojta/2026-11/x-a.csv", "archive/vojta/2026-11/x-a.csv")
    storage.move(store, "raw/vojta/2026-11/x-a.csv", "archive/vojta/2026-11/x-a.csv")  # no-op
    assert not store.exists("raw/vojta/2026-11/x-a.csv")
    assert store.get("archive/vojta/2026-11/x-a.csv") == b"abc"
    store.put("raw/vojta/2026-11/y-b.csv", b"xyz", "text/csv")
    real_get = store.get
    monkeypatch.setattr(
        store, "get", lambda p: b"corrupt" if p.startswith("archive/") else real_get(p)
    )
    with pytest.raises(OSError):
        storage.move(store, "raw/vojta/2026-11/y-b.csv", "archive/vojta/2026-11/y-b.csv")
    assert store.exists("raw/vojta/2026-11/y-b.csv")


def test_archive_path_only_maps_raw():
    assert storage.archive_path("raw/vojta/2026-11/u-n.csv") == "archive/vojta/2026-11/u-n.csv"
    assert storage.raw_path("archive/vojta/2026-11/u-n.csv") == "raw/vojta/2026-11/u-n.csv"
    with pytest.raises(ValueError):
        storage.archive_path("other/x")


# ------------------------------------------------------------------------------------- web / IDOR


def test_shared_viewer_can_never_delete_and_files_disappear_after_delete(as_user):
    upload_id, _ = _import(TEN_DAYS[:1])
    vojta, bara, admin = as_user("vojta"), as_user("bara"), as_user("vojta-admin")
    post(vojta, "/sharing/grant", data={"username": "bara"})
    measurement_id = _live()[0].id
    assert post(bara, f"/measurements/{measurement_id}/delete").status_code == 404
    assert post(bara, f"/uploads/{upload_id}/delete").status_code == 404
    assert not _upload(upload_id).deleted and len(_live()) == 1

    page = vojta.get("/report?confirm=delete").text  # the in-page confirmation (Report tab)
    assert "Smazat měření z 1. 6. 2026? Ten den pak můžeš nahrát znovu." in page
    assert f'action="/measurements/{measurement_id}/delete"' in page
    assert "archiv" not in page.lower()  # the user-facing text never mentions the archive
    shared = bara.get("/report?subject=vojta&confirm=delete").text
    assert "/delete" not in shared and "icon-danger" not in shared  # read-only: no bin, no form
    assert post(vojta, f"/measurements/{measurement_id}/delete").status_code == 303
    assert _upload(upload_id).deleted  # it was the only day
    assert post(vojta, f"/measurements/{measurement_id}/delete").status_code == 404  # gone
    assert vojta.get(f"/files/{upload_id}").status_code == 404
    assert bara.get(f"/files/{upload_id}").status_code == 404
    assert admin.get(f"/files/{upload_id}").status_code == 200  # audit


def test_a_deleted_measurement_is_invisible_on_every_page(as_user):
    upload_id, _ = _import(TEN_DAYS[:3])
    victim = _live()[1]
    _delete_measurement(victim.id)
    vojta, admin = as_user("vojta"), as_user("vojta-admin")
    post(vojta, "/sharing/grant", data={"username": "bara"})
    bara = as_user("bara")
    day = victim.measured_date.isoformat()
    for client, sub in ((vojta, ""), (bara, "?subject=vojta"), (admin, "?subject=vojta")):
        for url in ("/history", "/report"):
            page = client.get(url + sub)
            assert page.status_code == 200
            assert victim.id not in page.text and day not in page.text, (url, sub)
        sep = "&" if sub else "?"
        assert client.get(f"/report{sub}{sep}m={victim.id}").status_code == 404


def test_deleted_uploads_show_the_status_deleted_everywhere(as_user):
    upload_id, _ = _import(TEN_DAYS[:1])
    _delete_upload(upload_id)
    vojta, admin = as_user("vojta"), as_user("vojta-admin")
    for lang, label, wrong in (("cs", "smazáno", "nic nového"), ("en", "deleted", "nothing new")):
        for client in (vojta, admin):
            post(client, "/lang", data={"lang": lang, "next": "/"})
        pages = {
            "list": vojta.get("/history/uploads").text,
            "upload": vojta.get("/upload").text,
            "page": vojta.get(f"/uploads/{upload_id}").text,
            "admin": admin.get("/admin").text,
        }
        for where, page in pages.items():
            assert f">{label}<" in page, (lang, where)
            assert wrong not in page, (lang, where)


def test_upload_page_delete_button_confirms_without_mentioning_the_archive(as_user):
    upload_id, _ = _import()
    vojta = as_user("vojta")
    page = vojta.get(f"/uploads/{upload_id}").text
    assert f'href="/uploads/{upload_id}?confirm=delete"' in page  # the red bin
    page = vojta.get(f"/uploads/{upload_id}?confirm=delete").text  # in-page confirmation
    assert "Smazat tento upload a jeho měření? Pak ho můžeš nahrát znovu." in page
    assert "archiv" not in page.lower()
    assert post(vojta, f"/uploads/{upload_id}/delete").status_code == 303
    page = vojta.get(f"/uploads/{upload_id}").text
    assert "icon-danger" not in page and "archiv" not in page.lower()
