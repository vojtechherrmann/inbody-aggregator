"""docs/upload-pipeline.md sections 1-3: admission, processing, merge -- no network, fake client."""

from __future__ import annotations

import json

import anthropic
import config
import db
import pipeline
import pytest
import uploads
from helpers import FakeClient, csv_export, jpeg, pdf, png, response, wire
from sqlalchemy import select


def _upload(upload_id):
    with db.engine().connect() as conn:
        return conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()


def _measurements(user="vojta"):
    with db.engine().connect() as conn:
        return conn.execute(
            select(db.measurement)
            .where(db.measurement.c.user_id == user)
            .order_by(db.measurement.c.measured_date)
        ).all()


def _report(upload_id):
    return json.loads(_upload(upload_id).report_json)


def _extractions_of(upload_id):
    with db.engine().connect() as conn:
        return conn.execute(
            select(db.extraction).where(db.extraction.c.upload_id == upload_id)
        ).all()


def _spent():
    with db.engine().connect() as conn:
        return conn.execute(select(db.api_usage)).all()


# ------------------------------------------------------------------------------------ admission


def test_type_is_detected_by_content_not_extension():
    assert uploads.detect_kind(pdf()) == "pdf"
    assert uploads.detect_kind(jpeg()) == "jpg"
    assert uploads.detect_kind(png()) == "png"
    assert uploads.detect_kind(csv_export([("20260101101010", 150.0)])) == "csv"
    for junk in (b"GIF89a....", b"\x00\x01binary", b"name,weight\nx,1\n"):
        with pytest.raises(uploads.Rejected) as exc:
            uploads.detect_kind(junk)
        assert exc.value.key == "upload.err.type"


def test_size_limits_per_kind():
    with pytest.raises(uploads.Rejected) as exc:
        uploads.detect_kind(csv_export([("20260101101010", 150.0)]) + b"," * config.MAX_CSV_BYTES)
    assert exc.value.key == "upload.err.csv_size"
    with pytest.raises(uploads.Rejected) as exc:
        uploads.detect_kind(jpeg() + b"\x00" * config.MAX_IMAGE_BYTES)
    assert exc.value.key == "upload.err.image_size"
    with pytest.raises(uploads.Rejected) as exc:
        uploads.detect_kind(pdf() + b"\x00" * config.MAX_UPLOAD_BYTES)
    assert exc.value.key == "upload.err.size"


def test_raw_file_lands_in_blob_under_the_users_folder():
    upload_id = pipeline.admit("vojta", "my export.csv", csv_export([("20260101101010", 150.0)]))
    row = _upload(upload_id)
    assert row.status == "pending" and row.kind == "csv"
    assert row.blob_path.startswith(f"raw/vojta/{row.created_at:%Y-%m}/{upload_id}-my_export.csv")
    import storage

    assert storage.store().get(row.blob_path).startswith(b"\xef\xbb\xbfdate,")


def test_same_file_twice_is_duplicate_file_without_processing():
    data = csv_export([("20260101101010", 150.0)])
    first = pipeline.admit("vojta", "a.csv", data)
    pipeline.process_upload(first)
    second = pipeline.admit("vojta", "b.csv", data)
    row = _upload(second)
    assert (
        row.status == "duplicate_file"
        and row.duplicate_of == first
        and row.blob_path == _upload(first).blob_path
    )
    assert pipeline.process_upload(second) == "duplicate_file"
    assert _report(second)["duplicate_of"]["upload_id"] == first
    # another user uploading the same bytes is NOT a duplicate of vojta's
    assert _upload(pipeline.admit("bara", "a.csv", data)).status == "pending"


def test_daily_upload_limit(monkeypatch):
    monkeypatch.setattr(config, "UPLOADS_PER_USER_PER_DAY", 2)
    pipeline.admit("vojta", "1.csv", csv_export([("20260101101010", 150.0)]))
    pipeline.admit("vojta", "2.csv", csv_export([("20260102101010", 150.0)]))
    with pytest.raises(uploads.Rejected) as exc:
        pipeline.admit("vojta", "3.csv", csv_export([("20260103101010", 150.0)]))
    assert exc.value.key == "upload.err.daily"


def test_web_rejections_have_the_spec_status_codes(as_user, monkeypatch):
    from conftest import post

    client = as_user("vojta")
    resp = post(
        client, "/upload", files={"file": ("x.gif", b"GIF89a")}, headers={"X-Upload": "xhr"}
    )
    assert resp.status_code == 415 and "CSV" in resp.text  # + CSV tip
    monkeypatch.setattr(config, "UPLOADS_PER_USER_PER_DAY", 0)
    resp = post(client, "/upload", files={"file": ("x.csv", csv_export([("20260101101010", 1.0)]))})
    assert resp.status_code == 429


def test_xhr_upload_gets_201_and_the_upload_page(as_user):
    from conftest import post

    client = as_user("vojta")
    resp = post(
        client,
        "/upload",
        files={"file": ("x.csv", csv_export([("20260101101010", 150.0)]))},
        headers={"X-Upload": "xhr"},
    )
    assert resp.status_code == 201 and resp.headers["location"].startswith("/uploads/")
    page = client.get(resp.headers["location"])
    assert page.status_code == 200 and 'role="progressbar"' in page.text and "every 1s" in page.text


# ------------------------------------------------------------------------------------------- CSV


def test_csv_is_parsed_deterministically_without_any_api_call():
    client = FakeClient()
    upload_id = pipeline.admit(
        "vojta", "x.csv", csv_export([("20260312061532", 190.0), ("20260701080000", 188.0)])
    )
    assert pipeline.process_upload(upload_id, client) == "done"
    assert client.calls == [] and client.counted == 0 and _spent() == []
    rows = _measurements()
    assert [(str(r.measured_date), str(r.measured_time)) for r in rows] == [
        ("2026-03-12", "06:15:32"),  # stored as exported -- no timezone, never "fixed"
        ("2026-07-01", "08:00:00"),
    ]
    assert rows[0].weight_kg == 86.183
    assert rows[0].origin == "csv" and rows[0].device_model == "InBody270"


def test_csv_with_an_unknown_column_goes_to_review_and_saves_nothing():
    upload_id = pipeline.admit(
        "vojta", "x.csv", csv_export([("20260101101010", 150.0)], extra_header="Mystery(lb)")
    )
    assert pipeline.process_upload(upload_id) == "needs_review"
    row = _upload(upload_id)
    assert row.error == "csv_unknown_header" and "Mystery(lb)" in row.review_json
    assert _measurements() == []


# ----------------------------------------------------------------------------------------- sheets


def _sheet(name="s.jpg", content=None, **kw):
    return pipeline.admit("vojta", name, content or jpeg(tag=name.encode()), **kw)


def test_sheet_goes_through_claude_with_the_package_prompt_and_schema():
    client = FakeClient(
        wire(history=[("2026-04-01T10:00", 81.0, False), ("2026-05-18T15:40", 80.0, True)])
    )
    upload_id = _sheet()
    assert pipeline.process_upload(upload_id, client) == "done"
    call = client.calls[0]
    import sheet_schema

    assert call["model"] == "claude-opus-5-5" and call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["schema"] == sheet_schema.OUTPUT_SCHEMA
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["max_tokens"] == config.MAX_TOKENS
    rows = _measurements()
    # Only the main measurement: the sheet's history strip is checked but never stored
    # (user decision 2026-11-16 -- partial points would block a later full CSV row that day).
    assert len(rows) == 1 and not rows[0].from_history
    assert str(rows[0].measured_date) == "2026-05-18" and str(rows[0].measured_time) == "15:40:00"
    assert len(_extractions_of(upload_id)) == 1
    usage = _spent()
    assert len(usage) == 1 and usage[0].cost_microusd == 6000 * 4 + 3000 * 20  # $0.084


def test_form_source_name_applies_only_when_the_sheet_prints_none():  # two different days
    upload_id = _sheet("a.jpg", source_name="Gym B")
    pipeline.process_upload(upload_id, FakeClient(wire()))
    upload_id2 = _sheet("b.jpg", source_name="Gym B")
    pipeline.process_upload(
        upload_id2, FakeClient(wire(when="2026-11-01T09:00", source_name="Gym A"))
    )
    names = [r.source_name for r in _measurements()]
    assert names == ["Gym B", "Gym A"]


def test_not_an_inbody_sheet_fails_without_retry_and_shows_the_csv_tip(as_user):
    client = FakeClient(wire(is_sheet=False))
    upload_id = _sheet()
    assert pipeline.process_upload(upload_id, client) == "failed"
    assert len(client.calls) == 1 and _upload(upload_id).error_code == "not_inbody"
    page = as_user("vojta").get(f"/uploads/{upload_id}")
    assert "CSV" in page.text and "mailto:owner@example.test" in page.text


def test_invalid_answer_is_retried_once_and_the_retry_is_budget_checked(monkeypatch):
    checks = []
    import extract

    real = extract.check_budget
    monkeypatch.setattr(extract, "check_budget", lambda *a: (checks.append(1), real(*a))[1])
    client = FakeClient("not json", wire())
    assert pipeline.process_upload(_sheet(), client) == "done"
    assert len(client.calls) == 2 and len(checks) == 2 and len(_spent()) == 2  # both attempts paid


def test_api_error_retry_is_capped_at_one():
    import httpx2

    err = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    client = FakeClient(err, err, wire())
    upload_id = _sheet()
    assert pipeline.process_upload(upload_id, client) == "failed"
    assert len(client.calls) == 2 and "API error" in _upload(upload_id).error


def test_budget_exhausted_means_skipped_budget_and_no_call(monkeypatch):
    monkeypatch.setattr(config, "USER_MONTHLY_QUOTA_USD", 0.10)  # worst case is ~$0.41
    client = FakeClient(wire())
    upload_id = _sheet()
    assert pipeline.process_upload(upload_id, client) == "skipped_budget"
    assert client.calls == [] and _spent() == []


def test_monthly_budget_counts_everyone(monkeypatch):
    monkeypatch.setattr(config, "MONTHLY_BUDGET_USD", 0.45)  # worst case 0.414 + spent 0.084
    pipeline.process_upload(_sheet("a.jpg"), FakeClient(wire()))  # spends $0.084
    bara_upload = pipeline.admit("bara", "b.jpg", jpeg(tag=b"b"))
    assert pipeline.process_upload(bara_upload, FakeClient(wire())) == "skipped_budget"


def test_worst_case_estimate_includes_the_fallback_model():
    import extract

    assert extract.worst_case_microusd(10_000) == (10_000 * 4 + 8000 * 20) + (
        10_000 * 5 + 8000 * 25
    )


def test_fallback_iterations_are_billed_at_each_models_price():
    from types import SimpleNamespace as NS

    import extract

    resp = response(wire())
    resp.usage.iterations = [
        NS(
            type="message",
            model="claude-opus-5-5",
            input_tokens=1000,
            output_tokens=100,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        NS(
            type="fallback_message",
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    ]
    rows = extract.usage_rows(resp)
    assert [r["cost_microusd"] for r in rows] == [1000 * 4 + 100 * 20, 1000 * 5 + 500 * 25]


def test_retry_button_requeues_failed_uploads_for_the_owner_only(as_user):
    from conftest import post

    upload_id = _sheet()
    pipeline.process_upload(upload_id, FakeClient(wire(is_sheet=False)))
    assert post(as_user("bara"), f"/uploads/{upload_id}/retry").status_code == 404
    vojta = as_user("vojta")
    assert "/retry" in vojta.get(f"/uploads/{upload_id}").text
    assert post(vojta, f"/uploads/{upload_id}/retry").status_code == 303
    assert _upload(upload_id).status == "pending"
    # "not an InBody sheet" is not cached (only valid sheet answers are): a retry asks again
    client = FakeClient(wire())
    assert pipeline.process_upload(upload_id, client) == "done" and len(client.calls) == 1


# ------------------------------------------------------------------------------ pre-LLM checks


@pytest.mark.parametrize(
    "name, content, code",
    [
        ("long.pdf", pdf(pages=6), "precheck_too_many_pages"),
        ("small.png", png(500, 900), "precheck_too_small"),
        ("small.jpg", jpeg(599, 2000), "precheck_too_small"),
        ("cut.jpg", jpeg()[:-2], "precheck_unreadable"),
        ("cut.pdf", b"%PDF-1.4\n1 0 obj << /Type /Page >>", "precheck_unreadable"),
    ],
)
def test_prechecks_fail_before_any_spend(name, content, code):
    client = FakeClient(wire())
    upload_id = pipeline.admit("vojta", name, content)
    assert pipeline.process_upload(upload_id, client) == "failed"
    assert _upload(upload_id).error_code == code
    assert client.calls == [] and _spent() == []


def test_prechecks_pass_for_a_normal_sheet():
    import prechecks

    prechecks.check("pdf", pdf(pages=5))
    prechecks.check("png", png(600, 600))
    prechecks.check("jpg", jpeg())


# -------------------------------------------------------------------- reprocess / rebuild from blob


def test_reprocess_is_idempotent_and_reuses_the_cached_answer():
    client = FakeClient(wire())
    upload_id = _sheet()
    pipeline.process_upload(upload_id, client)
    before = [(r.id, r.measured_date, r.weight_kg) for r in _measurements()]
    again = FakeClient()
    assert pipeline.reprocess(pipeline.select_uploads(all_=True), again) == {upload_id: "done"}
    assert again.calls == [] and len(_spent()) == 1
    after = [(r.id, r.measured_date, r.weight_kg) for r in _measurements()]
    assert [x[1:] for x in after] == [x[1:] for x in before]


def test_sql_is_rebuildable_from_blob():
    upload_id = pipeline.admit("vojta", "x.csv", csv_export([("20260101101010", 150.0)]))
    pipeline.process_upload(upload_id)
    with db.engine().begin() as conn:  # lose every derived row
        for t in (db.measurement, db.extraction, db.upload):
            conn.execute(t.delete())
    assert pipeline.rebuild_index() == [upload_id]
    pipeline.reprocess(pipeline.select_uploads(all_=True))
    assert [r.weight_kg for r in _measurements()] == [68.039]
