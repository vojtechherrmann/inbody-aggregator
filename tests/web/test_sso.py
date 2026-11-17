"""Single sign-on from vojtah.cz: the accounts are the site's (schema vojtah); the site's API
mints a one-time ticket and GET /sso redeems it once, only on a same-site navigation."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

import pytest

SAME_SITE = {"Sec-Fetch-Site": "same-site"}


def mint(engine, uid="vojta", *, audience="inbody", ttl=60, used=False) -> str:
    """What the site's API does: a random ticket, only its sha256 in vojtah.sso_ticket."""
    import db

    ticket = secrets.token_urlsafe(32)
    now = db.utcnow()
    with engine.begin() as conn:
        conn.execute(
            db.sso_ticket.insert().values(
                id_hash=hashlib.sha256(ticket.encode()).hexdigest(),
                user_id=uid,
                audience=audience,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl),
                used_at=now if used else None,
            )
        )
    return ticket


def test_ticket_signs_in_once(env, anon):
    ticket = mint(env)
    resp = anon.get(f"/sso?t={ticket}", headers=SAME_SITE)
    assert resp.status_code == 303 and resp.headers["location"] == "/history"
    assert anon.cookies.get("__Host-inbody_session")
    assert anon.get("/history").status_code == 200

    other = type(anon)(anon.app, base_url="https://testserver", follow_redirects=False)
    again = other.get(f"/sso?t={ticket}", headers=SAME_SITE)  # replay
    assert again.status_code == 303 and again.headers["location"] == "/login"
    assert not other.cookies.get("__Host-inbody_session")


@pytest.mark.parametrize(
    "kwargs",
    [{"ttl": -1}, {"used": True}, {"audience": "other"}],
    ids=["expired", "used", "audience"],
)
def test_bad_ticket_goes_to_login(env, anon, kwargs):
    ticket = mint(env, **kwargs)
    resp = anon.get(f"/sso?t={ticket}", headers=SAME_SITE)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert not anon.cookies.get("__Host-inbody_session")


def test_unknown_or_missing_ticket(env, anon):
    for url in ("/sso", "/sso?t=nope", "/sso?t=" + "x" * 129):
        resp = anon.get(url, headers=SAME_SITE)
        assert resp.status_code in (303, 400), url
        assert not anon.cookies.get("__Host-inbody_session")


@pytest.mark.parametrize("site", [None, "cross-site", "none"])
def test_only_a_same_site_navigation_redeems(env, anon, site):
    """A ticket planted on another site must not sign the victim in (login CSRF) -- and it is not
    burned, so nothing is learnt either."""
    ticket = mint(env)
    headers = {"Sec-Fetch-Site": site} if site else {}
    resp = anon.get(f"/sso?t={ticket}", headers=headers)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert not anon.cookies.get("__Host-inbody_session")
    assert anon.get(f"/sso?t={ticket}", headers=SAME_SITE).headers["location"] == "/history"


def test_disabled_account_cannot_sign_in(env, anon):
    import db
    from sqlalchemy import update

    ticket = mint(env, "bara")
    with env.begin() as conn:
        conn.execute(update(db.app_user).where(db.app_user.c.id == "bara").values(is_disabled=True))
    assert anon.get(f"/sso?t={ticket}", headers=SAME_SITE).headers["location"] == "/login"


def test_replaces_the_previous_session(env, as_user):
    """Signed in here as someone else: the ticket's user wins, the old session is revoked."""
    client = as_user("bara")
    old = client.cookies.get("__Host-inbody_session")
    resp = client.get(f"/sso?t={mint(env, 'vojta')}", headers=SAME_SITE)
    assert resp.status_code == 303
    new = client.cookies.get("__Host-inbody_session")
    assert new and new != old
    import security

    with env.connect() as conn:
        assert security.session_user(conn, old) is None
        assert security.session_user(conn, new).id == "vojta"


def test_accounts_are_the_sites():
    """The user tables are in schema vojtah, and every user reference of this app points there."""
    import db

    for table in (db.app_user, db.user_session, db.sso_ticket):
        assert table.schema == "vojtah"
    for table in db.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name == "app_user":
                assert fk.target_fullname == "vojtah.app_user.id", table.name
