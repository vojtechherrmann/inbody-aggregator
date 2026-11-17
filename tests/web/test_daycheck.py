"""docs/upload-pipeline.md sections 3 + 4: one measurement per user per DAY, first come wins."""

from __future__ import annotations

import json

import data
import db
import pipeline
import pytest
from helpers import FakeClient, csv_export, jpeg, wire
from security import User
from sqlalchemy import select


def _report(upload_id):
    with db.engine().connect() as conn:
        return json.loads(
            conn.execute(
                select(db.upload.c.report_json).where(db.upload.c.id == upload_id)
            ).scalar()
        )


def _status(upload_id):
    with db.engine().connect() as conn:
        return conn.execute(select(db.upload.c.status).where(db.upload.c.id == upload_id)).scalar()


def _measurements(user="vojta"):
    """Live measurements (a deleted one keeps its row with deleted = 1)."""
    with db.engine().connect() as conn:
        return conn.execute(
            select(db.measurement)
            .where(db.measurement.c.user_id == user, db.measurement.c.deleted.is_(False))
            .order_by(db.measurement.c.measured_date)
        ).all()


def _extractions(upload_id):
    with db.engine().connect() as conn:
        return conn.execute(
            select(db.extraction)
            .where(db.extraction.c.upload_id == upload_id)
            .order_by(db.extraction.c.measured_date)
        ).all()


def _csv(rows, name="x.csv", user="vojta"):
    upload_id = pipeline.admit(user, name, csv_export(rows))
    pipeline.process_upload(upload_id)
    return upload_id


def _sheet(answer, name):
    upload_id = pipeline.admit("vojta", name, jpeg(tag=name.encode()))
    pipeline.process_upload(upload_id, FakeClient(answer))
    return upload_id


def test_new_days_are_accepted():
    upload_id = _csv([("20260312061532", 190.0), ("20260701080000", 188.0)])
    report = _report(upload_id)
    assert report["counts"] == {"total": 2, "accepted": 2, "rejected": 0}
    assert all(m["outcome"] == "accepted" for m in report["measurements"])
    assert _status(upload_id) == "done"  # weight, SMM, BFM, PBF present (REQUIRED_CORE)
    assert len(_measurements()) == 2


def test_an_existing_day_rejects_the_newcomer_whatever_the_time_or_source():
    csv_id = _csv([("20260312061532", 190.0)])
    # same DAY, different time (the app may drop PM), stronger-looking sheet: still rejected
    sheet_id = _sheet(wire(when="2026-03-12T16:27", weight=86.0, bfm=17.2, pbf=20.0), "us.png")
    m = _report(sheet_id)["measurements"][0]
    assert m["outcome"] == "rejected" and m["reason"] == "day_exists"
    assert m["conflict"]["upload_id"] == csv_id and m["conflict"]["origin"] == "csv"
    assert _status(sheet_id) == "nothing_new"
    rows = _measurements()
    assert len(rows) == 1 and rows[0].weight_kg == 86.183  # nothing merged or overwritten
    ext = _extractions(sheet_id)[0]
    assert (
        not ext.accepted
        and ext.reject_reason == "day_exists"
        and ext.conflicting_measurement_id == rows[0].id
    )


def test_first_come_wins_even_if_the_better_source_comes_later():
    sheet_id = _sheet(wire(when="2026-03-12T06:15", weight=80.0), "a.jpg")
    csv_id = _csv([("20260312061532", 190.0)])
    assert _measurements()[0].weight_kg == 80.0
    assert _report(csv_id)["measurements"][0]["conflict"]["upload_id"] == sheet_id


def test_history_points_are_never_stored_so_a_later_csv_still_gets_those_days():
    # user decision 2026-11-16: the sheet's history strip is not stored at all
    upload_id = _sheet(
        wire(
            history=[
                ("2026-04-01T10:00", 82.0, False),
                ("2026-05-18T08:00", 79.0, False),
                ("2026-05-18T15:40", 80.0, True),
            ]
        ),
        "a.jpg",
    )
    report = _report(upload_id)
    assert [(m["date"], m["outcome"], m["from_history"]) for m in report["measurements"]] == [
        ("2026-05-18", "accepted", False)
    ]
    csv_id = _csv([("20260401100000", 182.0)])
    assert [m["outcome"] for m in _report(csv_id)["measurements"]] == ["accepted"]


def test_rebuild_replays_in_upload_order_deterministically():
    a = _sheet(wire(when="2026-03-12T06:15", weight=80.0), "a.jpg")
    _csv([("20260312061532", 190.0)])
    before = [(r.id, r.weight_kg) for r in _measurements()]
    pipeline.reprocess(pipeline.select_uploads(all_=True), FakeClient())
    assert [(r.id, r.weight_kg) for r in _measurements()] == before
    assert before[0][0] == _extractions(a)[0].id  # measurement id = the accepted extraction's id


def test_deleting_a_measurement_frees_the_day_for_the_next_upload_in_line():
    sheet_id = _sheet(wire(when="2026-03-12T06:15", weight=80.0), "a.jpg")
    csv_id = _csv([("20260312061532", 190.0)])
    first = _measurements()[0]
    with db.engine().begin() as conn:
        pipeline.delete_measurement(conn, User("vojta", False), first.id)
    rows = _measurements()
    assert len(rows) == 1 and rows[0].weight_kg == 86.183 and rows[0].upload_id == csv_id
    assert _extractions(sheet_id)[0].reject_reason == "deleted"
    assert _status(csv_id) in ("done", "partial")


def test_deleting_an_upload_keeps_blob_and_rows_and_allows_reupload():
    data_bytes = csv_export([("20260312061532", 190.0)])
    upload_id = pipeline.admit("vojta", "x.csv", data_bytes)
    pipeline.process_upload(upload_id)
    with db.engine().begin() as conn:
        pipeline.delete_upload(conn, User("vojta", False), upload_id)
    assert _measurements() == [] and len(_extractions(upload_id)) == 1
    import storage

    with db.engine().connect() as conn:
        path = conn.execute(
            select(db.upload.c.blob_path).where(db.upload.c.id == upload_id)
        ).scalar()
    assert storage.store().get(path) == data_bytes
    again = pipeline.admit("vojta", "x.csv", data_bytes)  # not a duplicate_file any more
    assert pipeline.process_upload(again) in ("done", "partial") and len(_measurements()) == 1


def test_can_edit_excludes_shared_viewers(env):
    with env.begin() as conn:
        data.grant_share(conn, User("vojta", False), "bara")
        assert data.can_view(conn, User("bara", False), "vojta")
        assert not data.can_edit(conn, User("bara", False), "vojta")
        assert data.can_edit(conn, User("vojta-admin", True), "vojta")


def test_upload_page_shows_the_matching_section(as_user):
    csv_id = _csv([("20260312061532", 190.0)])
    sheet_id = _sheet(wire(when="2026-03-12T16:27", weight=86.0, bfm=17.2, pbf=20.0), "a.jpg")
    page = as_user("vojta").get(f"/uploads/{sheet_id}").text
    for text in (
        "Co jsme našli",
        "ODMÍTNUTO",
        "Tento den už má měření",
        f"/uploads/{csv_id}",
        "Tip:",
        "Nic nového",
    ):
        assert text in page, text
    assert (
        f"mailto:owner@example.test?subject=InBody%3A%20nahr%C3%A1n%C3%AD%20{sheet_id}%20%28vojta%29"
        in page
    )


@pytest.mark.parametrize("stamps", [[("20260312061532", 190.0), ("20260312160000", 189.0)]])
def test_two_rows_on_one_day_in_a_csv_go_to_review(stamps):
    upload_id = pipeline.admit("vojta", "x.csv", csv_export(stamps))
    assert pipeline.process_upload(upload_id) == "needs_review"
    assert _measurements() == []


# ------------------------------------------------------------------------------ enrichment
# User decision 2026-11-17 (changes docs/upload-pipeline.md section 3): a later upload for a day
# that already has a measurement still fills the VALUES THE DAY IS MISSING (NULL) and never
# overwrites a value.
# Typical case: a sheet photo first (no InBody score), the CSV export later (has it).


def _csv_with_score(stamp="20260624071000", weight_lb=176.4, score="77"):
    upload_id = pipeline.admit(
        "vojta", "s.csv", csv_export([(stamp, weight_lb)], extra={"InBody Score": score})
    )
    pipeline.process_upload(upload_id)
    return upload_id


def _sheet_same_day():
    # matches the CSV above: 176.4 lb = 80.0 kg, SMM 79.4 lb = 36.0 kg, BFM 35.3 lb = 16.0 kg
    return _sheet(wire(when="2026-06-24T07:10", weight=80.0, smm=36.0, bfm=16.0, pbf=20.0), "a.png")


def test_a_later_upload_fills_the_missing_values_of_the_day_and_never_overwrites():
    sheet_id = _sheet_same_day()
    assert _measurements()[0].inbody_score is None
    csv_id = _csv_with_score()
    row = _measurements()[0]
    assert len(_measurements()) == 1
    assert row.inbody_score == 77  # filled from the CSV
    assert row.weight_kg == 80.0 and row.origin == "sheet"  # the first one's values stay
    assert row.id == _extractions(sheet_id)[0].id and row.upload_id == sheet_id
    m = _report(csv_id)["measurements"][0]
    assert m["outcome"] == "rejected" and m["reason"] == "day_exists"
    assert "inbody_score" in m["enriched"] and "weight_kg" not in m["enriched"]
    assert _status(csv_id) == "done"  # it did add something
    assert "score" in _report(sheet_id)["measurements"][0]["filled"]  # the sheet's report is fuller


def test_enrichment_survives_a_rebuild_and_is_undone_when_the_donor_is_deleted():
    _sheet_same_day()
    csv_id = _csv_with_score()
    pipeline.reprocess(pipeline.select_uploads(all_=True), FakeClient())
    assert _measurements()[0].inbody_score == 77  # deterministic replay
    with db.engine().begin() as conn:
        pipeline.delete_upload(conn, User("vojta", False), csv_id)
    assert _measurements()[0].inbody_score is None  # nothing of the deleted file remains


def test_a_file_that_does_not_look_like_the_same_measurement_is_not_merged():
    _sheet_same_day()
    # same day, but 190 lb = 86 kg: another measurement, not the same one
    csv_id = _csv_with_score(weight_lb=190.0)
    assert _measurements()[0].inbody_score is None
    m = _report(csv_id)["measurements"][0]
    assert m["outcome"] == "rejected" and not m.get("enriched")
    assert _status(csv_id) == "nothing_new"


def test_values_that_are_already_there_are_never_replaced_by_a_later_file():
    _csv_with_score(score="77")
    _csv_with_score(stamp="20260624091500", score="55")  # same day, another score
    assert _measurements()[0].inbody_score == 77


def test_the_upload_page_says_which_values_were_added_to_the_day(as_user):
    _sheet_same_day()
    csv_id = _csv_with_score()
    page = as_user("vojta").get(f"/uploads/{csv_id}").text
    assert "chybějící hodnoty se do něj doplnily" in page and "InBody skóre" in page
    en = as_user("vojta").get(f"/uploads/{csv_id}", headers={"Accept-Language": "en"}).text
    assert "the values it was missing were added to it" in en and "InBody score" in en
