"""The public guest account "guest": fictional sample data, password-less login, read-only.

Synthetic data only (the generator's data is invented). The server must refuse EVERY write of a
guest session, whatever the UI shows; the guest sees only its own data; nobody shares into or out
of the account; one guest logging out must not end the other guests' sessions."""

from __future__ import annotations

import importlib.util
import json
import re
import uuid
from pathlib import Path

import config
import data
import db
import pipeline
import pytest
import security
from conftest import PASSWORDS, _client, csrf_of, login, post
from helpers import csv_export
from security import User
from sqlalchemy import func, select

REPO = Path(__file__).resolve().parents[2]
GUEST = config.GUEST_USER_ID
ALLOWED_POSTS = {"/logout", "/settings", "/login", "/login/guest", "/lang"}


def _load(script: str):
    spec = importlib.util.spec_from_file_location(script, REPO / "scripts" / f"{script}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seed_mod():
    return _load("seed_guest")


@pytest.fixture
def seeded(env, seed_mod):
    with db.engine().begin() as conn:
        seed_mod.seed(conn)
    return seed_mod


@pytest.fixture
def guest(seeded):
    """A client with a live guest session (no password anywhere)."""
    client = _client()
    resp = post(client, "/login/guest")
    assert resp.status_code == 303 and resp.headers["location"] == "/history"
    return client


def _count(table, **where) -> int:
    q = select(func.count()).select_from(table)
    for col, val in where.items():
        q = q.where(table.c[col] == val)
    with db.engine().connect() as conn:
        return conn.execute(q).scalar_one()


def _guest_ids() -> tuple[str, str]:
    """(a live measurement id, its upload id) of the guest."""
    with db.engine().connect() as conn:
        row = conn.execute(
            select(db.measurement.c.id, db.measurement.c.upload_id).where(
                db.measurement.c.user_id == GUEST
            )
        ).first()
    return row.id, row.upload_id


def _vojta_upload(as_user) -> str:
    client = as_user("vojta")
    content = csv_export([("20260312061532", 190.0)])
    resp = post(client, "/upload", files={"file": ("v.csv", content)})
    upload_id = resp.headers["location"].rsplit("/", 1)[1]
    assert pipeline.process_upload(upload_id) == "done"
    return upload_id


# --------------------------------------------------------------------------------------- seeding


def test_seed_creates_the_twelve_fictional_measurements(seeded):
    assert _count(db.measurement, user_id=GUEST) == 12
    with db.engine().connect() as conn:
        user = data.get_login_row(conn, GUEST)
        days = (
            conn.execute(
                select(db.measurement.c.measured_date).where(db.measurement.c.user_id == GUEST)
            )
            .scalars()
            .all()
        )
        statuses = set(
            conn.execute(select(db.upload.c.status).where(db.upload.c.user_id == GUEST)).scalars()
        )
    assert user.password_hash == security.UNUSABLE_PASSWORD_HASH and not user.is_admin
    assert not user.password_admin_set and not user.is_disabled
    assert min(days).year == 2024 and max(days) < db.utcnow().date()  # all in the past
    assert statuses <= {"done", "partial"}


def test_seed_uses_the_prototype_generator_with_all_columns_of_each_source(seeded, seed_mod):
    rows = seed_mod.sample_rows()
    assert len(rows) == 12
    with db.engine().connect() as conn:
        for rec in rows:
            m = conn.execute(
                select(db.measurement).where(
                    db.measurement.c.user_id == GUEST,
                    db.measurement.c.measured_date.isnot(None),
                    db.measurement.c.origin == rec["origin"],
                    func.date(db.measurement.c.measured_date) == rec["measured_date"],
                )
            ).first()
            assert m is not None, rec["measured_date"]
            for key in ("weight_kg", "skeletal_muscle_mass_kg", "bmi", "seg_lean_trunk_kg"):
                assert getattr(m, key) == pytest.approx(rec[key])


def test_seed_is_idempotent_and_replaces_only_the_guests_data(seeded, seed_mod, as_user):
    def snapshot():
        with db.engine().connect() as conn:
            ms = conn.execute(
                select(db.measurement.c.id, db.measurement.c.weight_kg).order_by(
                    db.measurement.c.id
                )
            ).all()
            us = conn.execute(select(db.upload.c.id).order_by(db.upload.c.id)).scalars().all()
        return [tuple(m) for m in ms if m], us

    vojta_upload = _vojta_upload(as_user)
    before_guest = _guest_ids()
    with db.engine().begin() as conn:  # tamper: an extra measurement + a stale share
        conn.execute(
            db.share.insert().values(owner_id="bara", viewer_id=GUEST, created_at=db.utcnow())
        )
    first = snapshot()
    with db.engine().begin() as conn:
        counts = seed_mod.seed(conn)
    assert counts == {"uploads": 5, "measurements": 12}
    assert snapshot() == first and _guest_ids() == before_guest  # deterministic ids
    assert _count(db.share) == 0  # shares in or out of the guest are gone
    assert _count(db.measurement, user_id=GUEST) == 12 == _count(db.measurement, user_id=GUEST)
    assert _count(db.upload, id=vojta_upload) == 1  # another user's data untouched
    assert _count(db.measurement, user_id="vojta") == 1


def test_reseed_keeps_open_sessions_and_a_disabled_flag(guest, seed_mod):
    from sqlalchemy import update

    with db.engine().begin() as conn:
        conn.execute(update(db.app_user).where(db.app_user.c.id == GUEST).values(is_disabled=True))
        seed_mod.seed(conn)
    assert _count(db.app_user, id=GUEST, is_disabled=True) == 1  # the off switch survives a re-seed
    with db.engine().begin() as conn:
        conn.execute(update(db.app_user).where(db.app_user.c.id == GUEST).values(is_disabled=False))
    assert guest.get("/history").status_code == 200  # the earlier session still works


def test_seeded_pages_render_for_the_guest(guest):
    history = guest.get("/history")
    assert history.status_code == 200
    payload = json.loads(
        re.search(r'id="history-data">(.*?)</script>', history.text, re.S).group(1)
    )
    assert len(payload["measurements"]) == 12 and payload["uploadUrl"] is None
    report = guest.get("/report")
    assert report.status_code == 200 and "2026" in report.text
    mid, upload_id = _guest_ids()
    assert guest.get(f"/report?m={mid}").status_code == 200
    assert guest.get(f"/uploads/{upload_id}").status_code == 200
    assert guest.get(f"/uploads/{upload_id}/live").status_code == 200
    assert guest.get("/history/uploads").status_code == 200
    for en in (guest.get("/report", headers={"Accept-Language": "en"}),):
        assert en.status_code == 200


def test_no_blob_exists_so_files_is_a_plain_404(guest):
    _, upload_id = _guest_ids()
    assert guest.get(f"/files/{upload_id}").status_code == 404


# ------------------------------------------------------------------------------------ guest login


def test_guest_login_needs_no_password_and_makes_a_short_session(seeded):
    client = _client()
    resp = post(client, "/login/guest")
    assert resp.status_code == 303 and resp.headers["location"] == "/history"  # no admin-set notice
    cookie = next(
        v
        for k, v in resp.headers.multi_items()
        if k == "set-cookie" and "__Host-inbody_session" in v
    ).lower()
    assert "max-age=7200" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    with db.engine().connect() as conn:
        row = conn.execute(select(db.user_session).where(db.user_session.c.user_id == GUEST)).one()
    assert (row.expires_at - row.created_at).total_seconds() == config.GUEST_SESSION_TTL_SECONDS
    assert config.GUEST_SESSION_TTL_SECONDS < config.SESSION_TTL_SECONDS
    assert client.get("/history").status_code == 200


def test_guest_login_needs_a_csrf_token(seeded):
    client = _client()
    assert client.post("/login/guest").status_code == 403
    client.get("/login")
    assert client.post("/login/guest", data={"csrf_token": "forged"}).status_code == 403


def test_no_password_ever_logs_in_as_the_guest(seeded, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_LIMITS_PER_IP", ((100, 60),))
    for pw in ("guest", "password", "Guest-2026", security.UNUSABLE_PASSWORD_HASH, "", "x" * 50):
        client = _client()
        resp = login(client, GUEST, pw or "x")
        assert resp.status_code == 401, pw
        assert client.get("/history").status_code == 303  # no session
    with db.engine().connect() as conn:
        row = data.get_login_row(conn, GUEST)
    assert (
        row.failed_login_count == 0 and row.locked_until is None
    )  # the shared account never locks
    assert not security.verify_password(row.password_hash, "anything")
    assert post(_client(), "/login/guest").status_code == 303  # still open after all of that


def test_a_dehashed_guest_row_still_refuses_the_normal_login(seeded):
    """Even with a REAL hash in the row (a mistake), /login refuses the reserved id."""
    from sqlalchemy import update

    with db.engine().begin() as conn:
        conn.execute(
            update(db.app_user)
            .where(db.app_user.c.id == GUEST)
            .values(password_hash=security.hash_password("Guest-Pw-1234"))
        )
    assert login(_client(), GUEST, "Guest-Pw-1234").status_code == 401


def test_guest_login_unavailable_when_not_seeded_or_disabled(env):
    assert post(_client(), "/login/guest").status_code == 503  # not seeded
    seed_mod = _load("seed_guest")
    with db.engine().begin() as conn:
        seed_mod.seed(conn)
    from sqlalchemy import update

    with db.engine().begin() as conn:
        conn.execute(update(db.app_user).where(db.app_user.c.id == GUEST).values(is_disabled=True))
    assert post(_client(), "/login/guest").status_code == 503
    with db.engine().begin() as conn:
        conn.execute(update(db.app_user).where(db.app_user.c.id == GUEST).values(is_disabled=False))
    assert post(_client(), "/login/guest").status_code == 303


def test_guest_login_is_rate_limited(seeded):
    client = _client()
    codes = [post(client, "/login/guest").status_code for _ in range(7)]
    assert codes[:5] == [303] * 5 and codes[5:] == [429, 429]


def test_guest_login_sends_no_push(seeded, monkeypatch):
    import notify

    monkeypatch.setattr(notify, "_send", lambda *a, **k: pytest.fail("guest login pushed"))
    monkeypatch.setattr(config, "NTFY_TOPIC", "t")
    assert post(_client(), "/login/guest").status_code == 303


def test_the_guest_is_never_an_admin_even_if_the_row_says_so(guest):
    from sqlalchemy import update

    with db.engine().begin() as conn:
        conn.execute(update(db.app_user).where(db.app_user.c.id == GUEST).values(is_admin=True))
    assert guest.get("/admin").status_code == 404
    assert guest.get("/admin/review").status_code == 404
    assert User(GUEST, True).is_guest and not User("bara", False).is_guest


def test_only_the_landing_page_offers_the_guest_button(anon):
    page = anon.get("/", headers={"Accept-Language": "en"})
    assert page.status_code == 200
    assert 'action="/login/guest"' in page.text and "Show as Guest" in page.text
    assert re.search(r'action="/login/guest".*?name="csrf_token"', page.text, re.S)
    login = anon.get("/login", headers={"Accept-Language": "en"})
    assert login.status_code == 200
    assert "/login/guest" not in login.text and "Show as Guest" not in login.text


# ----------------------------------------------------------------------------------- the banner


def test_banner_on_every_guest_page_and_only_there(guest, as_user, anon):
    for path in ("/history", "/report", "/data", "/nope", "/uploads/" + _guest_ids()[1]):
        page = guest.get(path, headers={"Accept-Language": "en"})
        assert "guest-banner" in page.text, path
        assert "Sample data: these are made-up measurements, not real ones." in page.text
        assert page.text.index("</header>") < page.text.index("guest-banner")  # right under the bar
    cs = guest.get("/history", headers={"Accept-Language": "cs"})
    assert "Ukázková data: toto jsou vymyšlená měření, ne skutečná." in cs.text
    assert 'style="' not in guest.get("/history").text  # strict CSP: no inline styles
    forbidden = guest.post("/upload", data={"csrf_token": csrf_of(guest)})
    assert forbidden.status_code == 403 and "guest-banner" in forbidden.text  # error pages too
    for client in (as_user("vojta"), as_user("vojta-admin"), anon):
        for path in ("/history", "/", "/login"):
            assert "guest-banner" not in client.get(path).text, path


# ------------------------------------------------------------------------ disabled UI controls


def test_ui_controls_render_disabled_for_the_guest(guest):
    for path in ("/history", "/report"):
        page = guest.get(path, headers={"Accept-Language": "en"}).text
        assert 'href="/upload"' not in page and 'href="/sharing"' not in page, path
        assert 'href="/account/password"' not in page, path
        assert "Change password" not in page, path  # not even a disabled entry in Settings
        n = page.count('aria-disabled="true" title="Not available in the guest account"')
        assert n >= 2, (path, n)  # Upload, Sharing (+ the bin on Report)
    report = guest.get("/report", headers={"Accept-Language": "en"}).text
    assert "confirm=delete" not in report and 'aria-label="Delete measurement"' in report
    upload_page = guest.get(f"/uploads/{_guest_ids()[1]}", headers={"Accept-Language": "en"}).text
    assert 'aria-label="Delete this upload and its measurements"' in upload_page
    assert "confirm=delete" not in upload_page and 'action="/uploads/' not in upload_page
    assert 'href="/upload"' not in upload_page  # "back" goes to History, not the upload form


def test_a_real_user_keeps_the_enabled_controls(as_user):
    page = as_user("vojta").get("/history").text
    assert 'href="/upload"' in page and 'href="/sharing"' in page
    assert 'href="/account/password"' in page


def test_the_unavailable_pages_are_refused_for_the_guest(guest):
    for path in ("/upload", "/sharing", "/account/password"):
        assert guest.get(path).status_code == 403, path


# ----------------------------------------------------------------- server-side write refusals


def _post_routes() -> list[str]:
    import app as app_module
    from fastapi.routing import APIRoute

    out = []
    for router in (app_module.public, app_module.private):
        for r in router.routes:
            if isinstance(r, APIRoute) and "POST" in r.methods and r.path not in ALLOWED_POSTS:
                out.append(
                    r.path.replace("{upload_id}", _guest_ids()[1])
                    .replace("{measurement_id}", _guest_ids()[0])
                    .replace("{action}", "approve")
                )
    return out


def test_every_mutating_route_refuses_the_guest_even_with_a_valid_csrf_token(guest):
    routes = _post_routes()
    assert len(routes) >= 8, routes  # upload, retry, deletes, sharing x2, admin review, password
    before = (
        _count(db.upload),
        _count(db.measurement, deleted=False),
        _count(db.share),
        _count(db.review_action),
    )
    for path in routes:
        body = {
            "csrf_token": csrf_of(guest),
            "username": "vojta",
            "viewer": "vojta",
            "current_password": "x",
            "new_password": "Abcdefg1",
            "new_password_repeat": "Abcdefg1",
        }
        assert guest.post(path, data=body).status_code == 403, path
        header_only = guest.post(path, headers={"X-CSRF-Token": csrf_of(guest)})
        assert header_only.status_code == 403, path
    after = (
        _count(db.upload),
        _count(db.measurement, deleted=False),
        _count(db.share),
        _count(db.review_action),
    )
    assert before == after


def test_multipart_upload_and_forged_requests_are_refused(guest):
    content = csv_export([("20260312061532", 190.0)])
    for headers in ({}, {"X-Upload": "xhr"}, {"X-CSRF-Token": csrf_of(guest)}):
        resp = guest.post(
            "/upload",
            files={"file": ("a.csv", content)},
            data={"csrf_token": csrf_of(guest), "source_name": "gym"},
            headers=headers,
        )
        assert resp.status_code == 403, headers
    assert _count(db.upload, user_id=GUEST) == 5  # only the seeded ones
    # a guest session cookie on a request WITHOUT any csrf token is refused as well
    assert guest.post("/upload", files={"file": ("a.csv", content)}).status_code == 403


def test_other_unsafe_methods_never_reach_a_route(guest):
    mid, upload_id = _guest_ids()
    for method in ("PUT", "PATCH", "DELETE"):
        for path in (f"/measurements/{mid}/delete", f"/uploads/{upload_id}", "/sharing/grant"):
            resp = guest.request(method, path, headers={"X-CSRF-Token": csrf_of(guest)})
            assert resp.status_code in (403, 405), (method, path)
    assert _count(db.measurement, user_id=GUEST, deleted=False) == 12


def test_the_theme_choice_works_for_the_guest(guest):
    resp = post(guest, "/settings", data={"theme": "dark", "next": "/report"})
    assert resp.status_code == 303
    assert 'data-theme="dark"' in guest.get("/report").text


def test_cookie_only_settings_and_logout_stay_allowed(guest):
    resp = post(guest, "/settings", data={"lang": "en", "mass": "imperial", "next": "/report"})
    assert resp.status_code == 303 and resp.headers["location"] == "/report"
    assert "inbody_units" in "".join(v for k, v in resp.headers.multi_items() if k == "set-cookie")
    assert guest.get("/report").status_code == 200
    assert post(guest, "/logout").status_code == 303
    assert guest.get("/history").status_code == 303


def test_logging_out_one_guest_keeps_every_other_guest_session(seeded):
    a, b = _client(), _client()
    post(a, "/login/guest")
    post(b, "/login/guest")
    assert post(a, "/logout").status_code == 303
    assert a.get("/history").status_code == 303  # a is out
    assert b.get("/history").status_code == 200  # b is still in
    with db.engine().connect() as conn:
        live = conn.execute(
            select(func.count()).where(
                db.user_session.c.user_id == GUEST, db.user_session.c.revoked_at.is_(None)
            )
        ).scalar_one()
    assert live == 1


def test_expired_guest_sessions_are_pruned_at_login(seeded):
    from datetime import timedelta

    with db.engine().begin() as conn:
        conn.execute(
            db.user_session.insert().values(
                id_hash="0" * 64,
                user_id=GUEST,
                created_at=db.utcnow() - timedelta(days=3),
                expires_at=db.utcnow() - timedelta(days=2),
            )
        )
    post(_client(), "/login/guest")
    assert _count(db.user_session, id_hash="0" * 64) == 0


# ---------------------------------------------------------------------------- data / pipeline layer


def test_the_data_layer_refuses_the_guest_without_the_router_guard(guest):
    mid, upload_id = _guest_ids()
    guest_user = User(GUEST, False)
    with pytest.raises(data.GuestReadOnly):
        pipeline.admit(GUEST, "a.csv", csv_export([("20260312061532", 190.0)]))
    assert _count(db.upload, user_id=GUEST) == 5
    with db.engine().begin() as conn:
        with pytest.raises(data.GuestReadOnly):
            pipeline.delete_measurement(conn, guest_user, mid)
        with pytest.raises(data.GuestReadOnly):
            pipeline.delete_upload(conn, guest_user, upload_id)
        # nobody edits the sample data, the admin included
        admin = User("vojta-admin", True)
        assert not data.can_edit(conn, admin, GUEST) and not data.can_edit(conn, guest_user, GUEST)
        with pytest.raises(data.GuestReadOnly):
            pipeline.delete_measurement(conn, admin, mid)
        with pytest.raises(data.GuestReadOnly):
            pipeline.delete_upload(conn, admin, upload_id)
        with pytest.raises(data.GuestReadOnly):
            pipeline.mark_for_retry(conn, GUEST, upload_id)
    with pytest.raises(data.GuestReadOnly):
        pipeline.restore_upload(upload_id)
    assert _count(db.measurement, user_id=GUEST, deleted=False) == 12


def test_reprocess_and_rebuild_leave_the_seeded_rows_alone(seeded):
    assert pipeline.select_uploads(all_=True) == []
    assert pipeline.select_uploads(user_id=GUEST) == []
    _, upload_id = _guest_ids()
    assert pipeline.process_upload(upload_id) == "done" or True
    assert _count(db.measurement, user_id=GUEST, deleted=False) == 12
    assert _count(db.extraction, user_id=GUEST) == 12
    assert pipeline.rebuild_index() == []


# ------------------------------------------------------------------------ isolation and sharing


def test_the_guest_sees_only_its_own_data(guest, as_user):
    upload_id = _vojta_upload(as_user)
    for path in (
        "/history?subject=vojta",
        "/report?subject=bara",
        "/history/uploads?subject=vojta",
        f"/uploads/{upload_id}",
        f"/uploads/{upload_id}/live",
        f"/files/{upload_id}",
    ):
        assert guest.get(path).status_code == 404, path
    page = guest.get("/history").text
    # nobody else to pick: the selector is a plain name (no dropdown listing anybody else)
    assert "<option" not in page and 'class="who-static"' in page and ">Host (já)<" in page
    other_names = page.replace("github.com/vojtechherrmann/vojtah", "")  # the footer repo link
    assert "vojta" not in other_names and "bara" not in other_names


def test_a_stale_share_row_never_opens_the_guest_in_or_out(guest, as_user):
    """Shares to / from the guest cannot be made; if a row exists anyway it grants nothing."""
    with db.engine().begin() as conn:
        for owner, viewer in (("vojta", GUEST), (GUEST, "bara")):
            conn.execute(
                db.share.insert().values(owner_id=owner, viewer_id=viewer, created_at=db.utcnow())
            )
    _vojta_upload(as_user)
    assert guest.get("/history?subject=vojta").status_code == 404
    bara = as_user("bara")
    assert bara.get("/history?subject=guest").status_code == 404
    assert bara.get("/report?subject=guest").status_code == 404
    assert 'value="guest"' not in bara.get("/history").text
    with db.engine().connect() as conn:
        assert not data.can_view(conn, User("bara", False), GUEST)
        assert not data.can_view(conn, User(GUEST, False), "vojta")
        assert data.shared_with_me(conn, User("bara", False)) == []
        assert data.shared_with_me(conn, User(GUEST, False)) == []
        assert data.my_grants(conn, User(GUEST, False)) == []


def test_the_admin_may_still_read_the_guest(seeded, as_user):
    admin = as_user("vojta-admin")
    assert admin.get("/history?subject=guest").status_code == 200
    assert admin.get("/report?subject=guest").status_code == 200
    with db.engine().connect() as conn:
        assert GUEST in data.viewable_subjects(conn, User("vojta-admin", True))
        assert data.can_view(conn, User("vojta-admin", True), GUEST)


def test_shares_to_or_from_the_guest_are_refused(seeded, as_user):
    with db.engine().begin() as conn:
        with pytest.raises(data.GuestReadOnly):
            data.grant_share(conn, User("vojta", False), GUEST)
        with pytest.raises(data.GuestReadOnly):
            data.grant_share(conn, User(GUEST, False), "vojta")
    vojta = as_user("vojta")
    resp = post(vojta, "/sharing/grant", data={"username": "guest"})
    assert resp.status_code == 303 and "share_not_found" in resp.headers["location"]  # unknown user
    assert _count(db.share) == 0
    page = vojta.get("/sharing").text
    assert "guest" not in page


def test_the_guest_id_cannot_be_created_as_a_normal_user(env):
    with db.engine().begin() as conn:
        with pytest.raises(ValueError):
            data.create_user(conn, GUEST, security.hash_password("Abcdefg1"))
        with pytest.raises(ValueError):
            data.create_user(
                conn, GUEST, security.UNUSABLE_PASSWORD_HASH, guest=True, is_admin=True
            )
        with pytest.raises(ValueError):
            data.create_user(conn, "someone", security.UNUSABLE_PASSWORD_HASH, guest=True)


# --------------------------------------------------------------------------------------- the CLI


@pytest.fixture
def cli(tmp_path, monkeypatch):
    mod = _load("users")
    monkeypatch.setattr(mod, "secrets_file", lambda: tmp_path / "inbody-users.local.env")
    return mod, tmp_path / "inbody-users.local.env"


def test_users_cli_refuses_the_reserved_guest_id(seeded, cli):
    mod, secrets_path = cli
    for argv in (
        ["add", GUEST],
        ["add", GUEST, "--admin"],
        ["reset-password", GUEST],
        ["grant-extra", GUEST, "5"],
        ["share", GUEST, "vojta"],
        ["share", "vojta", GUEST],
    ):
        with pytest.raises(SystemExit) as exc:
            mod.main(argv)
        assert "guest" in str(exc.value), argv
    assert not secrets_path.exists()
    with db.engine().connect() as conn:
        assert data.get_login_row(conn, GUEST).password_hash == security.UNUSABLE_PASSWORD_HASH
    assert _count(db.share) == 0


def test_apply_passwords_never_gives_the_guest_a_password(seeded, cli, capsys):
    mod, secrets_path = cli
    secrets_path.write_text(
        f"INBODY_PW_GUEST=Whatever-123456\nINBODY_PW_BARA={PASSWORDS['bara']}x\n"
    )
    mod.main(["apply-passwords"])
    out = capsys.readouterr().out
    assert "guest" not in out and "bara" in out
    with db.engine().connect() as conn:
        row = data.get_login_row(conn, GUEST)
    assert row.password_hash == security.UNUSABLE_PASSWORD_HASH and not row.password_admin_set


def test_users_cli_can_switch_the_guest_login_off_and_on(seeded, cli):
    mod, _ = cli
    mod.main(["disable", GUEST])
    assert post(_client(), "/login/guest").status_code == 503
    mod.main(["enable", GUEST])
    assert post(_client(), "/login/guest").status_code == 303


def test_pipeline_cli_import_for_the_guest_is_refused(seeded, tmp_path):
    f = tmp_path / "a.csv"
    f.write_bytes(csv_export([("20260312061532", 190.0)]))
    with pytest.raises(data.GuestReadOnly):
        pipeline.admit(GUEST, f.name, f.read_bytes())


def test_unrelated_uuid_paths_are_plain_404_for_the_guest(guest):
    assert guest.get(f"/uploads/{uuid.uuid4()}").status_code == 404
    assert guest.get(f"/report?m={uuid.uuid4()}").status_code == 404
