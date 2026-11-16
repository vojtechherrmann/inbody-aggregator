"""Fixtures: a fresh SQLite DB + blob dir per test, three users, logged-in clients."""

from __future__ import annotations

import re

import pytest

PASSWORDS = {
    "vojta": "pw-vojta-0123456789",
    "vojta-admin": "pw-admin-0123456789",
    "bara": "pw-bara-0123456789",
}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    import data
    import db
    import ratelimit
    import security
    import storage

    engine = db.sqlite_engine(str(tmp_path / "t.db"))
    db.set_engine(engine)
    storage.set_store(storage.LocalStore(str(tmp_path / "blobs")))
    ratelimit.limiter.reset()
    with engine.begin() as conn:
        for uid, pw in PASSWORDS.items():
            data.create_user(conn, uid, security.hash_password(pw), is_admin=uid == "vojta-admin")
    yield engine
    db.set_engine(None)
    storage.set_store(None)


def _client():
    from app import app
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def csrf_of(client) -> str:
    """The CSRF token = the signed double-submit cookie (what the page embeds in forms)."""
    token = client.cookies.get("__Host-inbody_csrf")
    if token is None:
        client.get("/login")
        token = client.cookies.get("__Host-inbody_csrf")
    return token


def login(client, uid: str, password: str | None = None):
    return client.post(
        "/login",
        data={
            "username": uid,
            "password": password or PASSWORDS[uid],
            "csrf_token": csrf_of(client),
        },
    )


@pytest.fixture
def anon():
    return _client()


@pytest.fixture
def as_user():
    """as_user("bara") -> a TestClient with a live session for that user."""

    def make(uid: str):
        client = _client()
        resp = login(client, uid)
        assert resp.status_code == 303, resp.text
        return client

    return make


def post(client, url: str, **kwargs):
    data = dict(kwargs.pop("data", {}))
    data.setdefault("csrf_token", csrf_of(client))
    return client.post(url, data=data, **kwargs)


def first_upload_id(text: str) -> str:
    return re.search(r"/(?:files|uploads)/([0-9a-f-]{36})", text).group(1)
