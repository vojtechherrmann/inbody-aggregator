"""needs_review + the admin review queue + the ntfy push (docs/upload-pipeline.md section 6)."""

from __future__ import annotations

import db
import notify
import pipeline
import pytest
from conftest import post
from helpers import FakeClient, csv_export, jpeg, wire
from sqlalchemy import select


@pytest.fixture
def pushes(monkeypatch):
    import config

    sent = []
    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(notify, "SYNC", True)
    monkeypatch.setattr(
        notify, "_send", lambda topic, title, body, priority: sent.append((topic, title, body))
    )
    notify.reset()
    yield sent
    notify.reset()


def _upload(upload_id):
    with db.engine().connect() as conn:
        return conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()


def _measurements():
    with db.engine().connect() as conn:
        return conn.execute(select(db.measurement)).all()


def _odd_csv():
    upload_id = pipeline.admit(
        "vojta", "odd.csv", csv_export([("20260312061532", 190.0)], extra_header="Weight(st)")
    )
    return upload_id, pipeline.process_upload(upload_id)


def test_odd_csv_header_goes_to_review_pushes_and_saves_nothing(pushes):
    upload_id, status = _odd_csv()
    assert status == "needs_review" and _measurements() == []
    assert len(pushes) == 1
    topic, title, body = pushes[0]
    assert topic == "test-topic" and "needs_review" in title and "vojta" in title
    assert f"upload: {upload_id}" in body and f"/admin/review/{upload_id}" in body
    assert "csv_unknown_header" in body


def test_no_measured_values_in_any_push(pushes):
    # a sheet whose numbers are inconsistent -> review; its values must not leak into the push
    upload_id = pipeline.admit("vojta", "a.jpg", jpeg(tag=b"a"))
    assert (
        pipeline.process_upload(upload_id, FakeClient(wire(weight=123.4, bfm=16.0, pbf=30.0)))
        == "needs_review"
    )
    body = pushes[0][2]
    without_ids = body.replace(upload_id, "<id>")  # a random uuid may contain any digits
    for value in ("123.4", "16.0", "30.0", "16", "123"):
        assert value not in without_ids, value
    assert "sheet_inconsistent" in body
    assert "weight" not in body.split("reason:")[1].split("\n")[0].replace("sheet_inconsistent", "")


def test_a_failing_push_never_breaks_the_upload(monkeypatch):
    import config

    monkeypatch.setattr(config, "NTFY_TOPIC", "test-topic")
    monkeypatch.setattr(notify, "SYNC", True)

    def boom(*_a):
        raise OSError("ntfy down")

    monkeypatch.setattr(notify, "_send", boom)
    notify.reset()
    _, status = _odd_csv()
    assert status == "needs_review"


def test_one_push_per_upload_and_twenty_per_hour(pushes, monkeypatch):
    import config

    monkeypatch.setattr(config, "UPLOADS_PER_USER_PER_DAY", 100)
    upload_id, _ = _odd_csv()
    assert not notify.upload_event(upload_id, "vojta", "needs_review", "x")  # already notified
    with db.engine().begin() as conn:
        conn.execute(db.upload.update().values(notified_at=None))
    for i in range(notify.MAX_PER_HOUR + 5):
        uid = pipeline.admit(
            "bara",
            f"{i}.csv",
            csv_export([(f"2026010{1 + i % 9}10101{i % 10}", 150.0 + i)], extra_header="X"),
        )
        pipeline.process_upload(uid)
    assert len(pushes) == notify.MAX_PER_HOUR
    notify._sent.clear()  # an hour later
    last = pipeline.admit(
        "bara", "last.csv", csv_export([("20260301101010", 150.0)], extra_header="Y")
    )
    pipeline.process_upload(last)
    assert "more not pushed" in pushes[-1][2]


def test_only_the_first_skipped_budget_of_the_day_pushes(pushes, monkeypatch):
    import config

    monkeypatch.setattr(config, "USER_MONTHLY_QUOTA_USD", 0.01)
    for name in ("a.jpg", "b.jpg"):
        uid = pipeline.admit("vojta", name, jpeg(tag=name.encode()))
        assert pipeline.process_upload(uid, FakeClient(wire())) == "skipped_budget"
    assert len(pushes) == 1


def test_push_is_off_without_a_topic(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_send", lambda *a: sent.append(a))
    _odd_csv()
    assert sent == []


def test_user_sees_vojta_was_notified(as_user, pushes):
    upload_id, _ = _odd_csv()
    page = as_user("vojta").get(f"/uploads/{upload_id}").text
    assert "Vojta dostal upozornění a ozve se ti." in page
    assert "Weight(st)" not in page  # review details are admin-only


# ---------------------------------------------------------------------------------- admin review


def test_review_queue_is_admin_only(as_user, pushes):
    upload_id, _ = _odd_csv()
    for who in ("vojta", "bara"):
        client = as_user(who)
        assert client.get("/admin/review").status_code == 404
        assert client.get(f"/admin/review/{upload_id}").status_code == 404
        for action in ("approve", "reject", "reprocess"):
            assert post(client, f"/admin/review/{upload_id}/{action}").status_code == 404
    assert _upload(upload_id).status == "needs_review"
    admin = as_user("vojta-admin")
    queue = admin.get("/admin/review").text
    assert upload_id in queue
    detail = admin.get(f"/admin/review/{upload_id}").text
    assert "Weight(st)" in detail and f"/files/{upload_id}" in detail
    assert admin.get(f"/files/{upload_id}").status_code == 200


def test_approve_runs_the_day_check_and_is_logged(as_user):
    upload_id = pipeline.admit("vojta", "a.jpg", jpeg(tag=b"a"))
    pipeline.process_upload(upload_id, FakeClient(wire(weight=80.0, bfm=16.0, pbf=30.0)))
    assert _upload(upload_id).status == "needs_review" and _measurements() == []
    admin = as_user("vojta-admin")
    assert post(admin, f"/admin/review/{upload_id}/approve").status_code == 303
    assert _upload(upload_id).status in ("done", "partial") and len(_measurements()) == 1
    with db.engine().connect() as conn:
        log = conn.execute(select(db.review_action)).all()
    assert [(a.admin_id, a.action) for a in log] == [("vojta-admin", "approve")]


def test_reject_fails_the_upload_with_a_note(as_user, pushes):
    upload_id, _ = _odd_csv()
    admin = as_user("vojta-admin")
    post(admin, f"/admin/review/{upload_id}/reject", data={"note": "please export again in kg"})
    row = _upload(upload_id)
    assert (
        row.status == "failed"
        and row.error == "please export again in kg"
        and _measurements() == []
    )


def test_reprocess_requeues(as_user, pushes):
    upload_id, _ = _odd_csv()
    post(as_user("vojta-admin"), f"/admin/review/{upload_id}/reprocess")
    assert _upload(upload_id).status == "pending"


@pytest.mark.parametrize(
    "env, title, priority, tag",
    [
        ("production", "[PROD] t", "urgent", "rotating_light"),
        ("dev", "[DEV] t", "default", "inbody"),
        ("staging", "[DEV] t", "default", "inbody"),
    ],
)
def test_push_headers_by_explicit_app_env(monkeypatch, env, title, priority, tag):
    import config

    monkeypatch.setattr(config, "APP_ENV", env)
    h = notify.headers("t")
    assert (h["Title"], h["Priority"], h["Tags"]) == (title, priority, tag)


def test_send_uses_those_headers(monkeypatch):
    import urllib.request

    import config

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout):
        captured.update(url=req.full_url, headers=dict(req.header_items()), timeout=timeout)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(config, "APP_ENV", "production")
    notify._send("topic-x", "inbody: failed (vojta)", "body")
    assert captured["url"] == "https://ntfy.sh/topic-x" and captured["timeout"] == 10.0
    assert captured["headers"]["Priority"] == "urgent" and captured["headers"]["Title"].startswith(
        "[PROD] "
    )


def test_non_blocking_warnings_and_exercise_warnings_do_not_send_to_review():
    answer = wire()
    answer["warnings"] = [
        {"code": "truncated_value", "field": "z_20khz_right_arm_ohm", "note": "decimals cut off"},
        {"code": "not_printed", "field": "", "note": "no device model"},
        {"code": "ambiguous", "field": "exercise", "note": "label hard to read"},
    ]
    upload_id = pipeline.admit("vojta", "w.jpg", jpeg(tag=b"w"))
    assert pipeline.process_upload(upload_id, FakeClient(answer)) == "done"
    answer2 = wire(when="2026-11-01T10:00")
    answer2["warnings"] = [{"code": "ambiguous", "field": "weight_kg", "note": "smudged"}]
    upload_id2 = pipeline.admit("vojta", "w2.jpg", jpeg(tag=b"w2"))
    assert pipeline.process_upload(upload_id2, FakeClient(answer2)) == "needs_review"
