"""Anti-IDOR: the viewer always comes from the session; every read goes through can_view.

bara must not see vojta's history, uploads or files through ANY route or guessed id; a share
grants read-only access; revoking removes it; the admin sees everything."""

from __future__ import annotations

import uuid

import pipeline
from conftest import post
from helpers import csv_export


def _vojta_upload(as_user) -> str:
    client = as_user("vojta")
    data = csv_export([("20260312061532", 190.0)])
    resp = post(
        client, "/upload", files={"file": ("vojta.csv", data)}, data={"source_name": "Gym C"}
    )
    assert resp.status_code == 303
    upload_id = resp.headers["location"].rsplit("/", 1)[1]
    assert pipeline.process_upload(upload_id) == "done"
    return upload_id


def _bara_sees_nothing_of_vojta(bara, upload_id):
    assert bara.get("/history?subject=vojta").status_code == 404
    assert bara.get("/history/uploads?subject=vojta").status_code == 404
    assert bara.get(f"/uploads/{upload_id}").status_code == 404
    assert bara.get(f"/uploads/{upload_id}/live").status_code == 404
    assert bara.get(f"/files/{upload_id}").status_code == 404
    own = bara.get("/history")
    assert own.status_code == 200 and "vojta.csv" not in own.text and upload_id not in own.text
    assert 'value="vojta"' not in own.text  # not offered in the "see as" selector


def test_bara_cannot_reach_vojtas_data_by_any_route(as_user):
    upload_id = _vojta_upload(as_user)
    _bara_sees_nothing_of_vojta(as_user("bara"), upload_id)


def test_guessed_and_malformed_ids_are_plain_404(as_user):
    bara = as_user("bara")
    for path in (
        f"/files/{uuid.uuid4()}",
        "/files/1",
        "/files/..%2f..%2fetc",
        f"/uploads/{uuid.uuid4()}",
    ):
        assert bara.get(path).status_code == 404, path


def test_a_share_grants_read_only_access_and_revoking_removes_it(as_user):
    upload_id = _vojta_upload(as_user)
    vojta, bara = as_user("vojta"), as_user("bara")
    assert post(vojta, "/sharing/grant", data={"username": "bara"}).status_code == 303

    page = bara.get("/history?subject=vojta")
    assert (
        page.status_code == 200 and "86.183" in page.text
    )  # 190 lb = 86.183 kg, the merged weight
    assert 'action="/upload"' not in page.text  # read-only: no upload form
    assert (
        bara.get(f"/files/{upload_id}").status_code == 404
    )  # no download for anybody but the admin
    assert vojta.get(f"/files/{upload_id}").status_code == 404  # not even the owner
    assert bara.get(f"/uploads/{upload_id}").status_code == 200
    assert 'value="vojta"' in bara.get("/history").text  # now offered in the selector
    # read-only: bara cannot retry vojta's upload
    assert post(bara, f"/uploads/{upload_id}/retry").status_code == 404

    assert post(vojta, "/sharing/revoke", data={"viewer": "bara"}).status_code == 303
    _bara_sees_nothing_of_vojta(bara, upload_id)


def test_revoke_only_ever_touches_the_session_users_own_grants(as_user):
    _vojta_upload(as_user)
    vojta, bara = as_user("vojta"), as_user("bara")
    post(vojta, "/sharing/grant", data={"username": "bara"})
    # bara tries to revoke "her own access" to vojta: the owner is always the session user,
    # so this can only revoke a (non-existent) bara -> bara grant
    post(bara, "/sharing/revoke", data={"viewer": "bara", "owner": "vojta"})
    assert bara.get("/history?subject=vojta").status_code == 200


def test_uploads_always_belong_to_the_session_user(as_user):
    bara = as_user("bara")
    resp = post(
        bara,
        "/upload",
        files={"file": ("x.csv", csv_export([("20260101101010", 150.0)]))},
        data={"user_id": "vojta", "subject": "vojta"},
    )
    upload_id = resp.headers["location"].rsplit("/", 1)[1]
    import db
    from sqlalchemy import select

    with db.engine().connect() as conn:
        owner = conn.execute(
            select(db.upload.c.user_id).where(db.upload.c.id == upload_id)
        ).scalar()
    assert owner == "bara"


def test_admin_sees_everything_and_only_the_admin_page_lists_users(as_user):
    upload_id = _vojta_upload(as_user)
    admin = as_user("vojta-admin")
    assert admin.get("/history?subject=vojta").status_code == 200
    assert admin.get(f"/files/{upload_id}").status_code == 200
    page = admin.get("/admin")
    assert page.status_code == 200 and "bara" in page.text and "vojta" in page.text
    sel = admin.get("/history").text
    assert 'value="bara"' in sel and 'value="vojta"' in sel  # admin's selector lists all users
    assert as_user("vojta").get("/admin").status_code == 404


def test_can_view_is_the_single_rule(env):
    import data
    from security import User

    with env.begin() as conn:
        vojta, bara, admin = User("vojta", False), User("bara", False), User("vojta-admin", True)
        assert data.can_view(conn, vojta, "vojta")
        assert not data.can_view(conn, bara, "vojta")
        assert data.can_view(conn, admin, "vojta") and not data.can_view(conn, admin, "nobody")
        data.grant_share(conn, vojta, "bara")
        assert data.can_view(conn, bara, "vojta") and not data.can_view(conn, vojta, "bara")
        data.revoke_share(conn, vojta, "bara")
        assert not data.can_view(conn, bara, "vojta")


def test_only_owner_or_admin_can_delete_measurements_and_uploads(as_user):
    upload_id = _vojta_upload(as_user)
    import db
    from sqlalchemy import select

    with db.engine().connect() as conn:
        measurement_id = conn.execute(select(db.measurement.c.id)).scalar()
    vojta, bara = as_user("vojta"), as_user("bara")
    post(vojta, "/sharing/grant", data={"username": "bara"})  # bara may VIEW, never edit
    assert post(bara, f"/measurements/{measurement_id}/delete").status_code == 404
    assert post(bara, f"/uploads/{upload_id}/delete").status_code == 404
    assert post(bara, f"/measurements/{uuid.uuid4()}/delete").status_code == 404
    with db.engine().connect() as conn:
        assert conn.execute(select(db.measurement.c.id)).scalar() == measurement_id
    assert "/delete" not in bara.get("/history?subject=vojta").text
    # the admin can; so can the owner
    assert post(as_user("vojta-admin"), f"/measurements/{measurement_id}/delete").status_code == 303
    with db.engine().connect() as conn:  # flagged, not removed (deleting never deletes rows)
        assert conn.execute(select(db.measurement.c.deleted)).scalar() is True
    assert post(vojta, f"/uploads/{upload_id}/delete").status_code == 303
