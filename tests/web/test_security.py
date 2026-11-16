"""Auth, CSRF, headers, login abuse controls."""

from __future__ import annotations

import uuid

import pytest
from conftest import PASSWORDS, csrf_of, login, post


def _routes():
    """Every API route of both routers (FastAPI wraps included routers; walk into them)."""
    import app as app_module
    from fastapi.routing import APIRoute

    out = []
    for router in (app_module.public, app_module.private):
        for r in router.routes:
            if isinstance(r, APIRoute):
                path = (
                    r.path.replace("{upload_id}", str(uuid.uuid4()))
                    .replace("{measurement_id}", str(uuid.uuid4()))
                    .replace("{action}", "approve")
                )
                for m in r.methods:
                    out.append((m, path))
    return out


PUBLIC = {
    ("GET", "/"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/login/guest"),  # "Log in as Guest": a session without a password (see test_guest)
    ("POST", "/lang"),
    ("GET", "/healthz"),
    ("GET", "/dbstatus"),  # polled by the "starting the database" page; never touches the DB
    ("GET", "/data"),  # how data is handled: readable before asking for an account
}


def test_every_non_public_route_requires_a_session(anon):
    routes = [r for r in _routes() if r not in PUBLIC]
    assert len(routes) >= 16
    for method, path in routes:
        resp = anon.request(method, path, data={"csrf_token": csrf_of(anon)})
        if method == "GET":
            assert resp.status_code == 303 and resp.headers["location"] == "/login", (method, path)
        else:
            assert resp.status_code == 401, (method, path, resp.status_code)


def test_htmx_request_without_session_gets_401_with_redirect_header(anon):
    resp = anon.get("/history/uploads", headers={"HX-Request": "true"})
    assert resp.status_code == 401 and resp.headers["HX-Redirect"] == "/login"


def test_every_post_without_csrf_is_rejected(as_user):
    client = as_user("vojta")
    for method, path in _routes():
        if method != "POST" or path == "/upload":
            continue
        resp = client.post(path, data={"username": "bara", "viewer": "bara"})
        assert resp.status_code == 403, (path, resp.status_code)
    resp = client.post("/upload", files={"file": ("a.csv", b"date,x\n1,2\n")})
    assert resp.status_code == 403


def test_post_with_a_wrong_or_forged_csrf_token_is_rejected(as_user):
    client = as_user("vojta")
    assert (
        post(client, "/sharing/grant", data={"username": "bara", "csrf_token": "x"}).status_code
        == 403
    )
    client.cookies.set("__Host-inbody_csrf", "forged")
    assert (
        client.post("/sharing/grant", data={"username": "bara", "csrf_token": "forged"}).status_code
        == 403
    )


def test_login_without_csrf_is_rejected(anon):
    assert (
        anon.post("/login", data={"username": "vojta", "password": PASSWORDS["vojta"]}).status_code
        == 403
    )


def test_no_json_api_docs(anon):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert anon.get(path).status_code == 404


def test_security_headers(anon):
    resp = anon.get("/")
    csp = resp.headers["content-security-policy"]
    assert (
        "default-src 'self'" in csp
        and "frame-ancestors 'none'" in csp
        and "unsafe-inline" not in csp
    )
    assert resp.headers["strict-transport-security"].startswith("max-age=")
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_session_cookie_flags(anon):
    resp = login(anon, "vojta")
    cookie = next(
        v
        for k, v in resp.headers.multi_items()
        if k == "set-cookie" and "__Host-inbody_session" in v
    )
    low = cookie.lower()
    assert (
        "httponly" in low
        and "secure" in low
        and "samesite=strict" in low
        and "max-age=43200" in low
    )


def test_login_failure_message_is_uniform(anon):
    wrong = login(anon, "vojta", "nope")
    unknown = login(anon, "nobody", "nope")
    assert wrong.status_code == unknown.status_code == 401
    assert "Špatné jméno nebo heslo" in wrong.text and "Špatné jméno nebo heslo" in unknown.text


def test_honeypot_fails_like_a_wrong_password(anon):
    resp = anon.post(
        "/login",
        data={
            "username": "vojta",
            "password": PASSWORDS["vojta"],
            "website": "spam",
            "csrf_token": csrf_of(anon),
        },
    )
    assert resp.status_code == 401


def test_lockout_after_repeated_failures_blocks_even_the_right_password(anon, monkeypatch):
    import config

    monkeypatch.setattr(config, "LOGIN_LIMITS_PER_IP", ((100, 60),))
    for _ in range(config.LOCKOUT_THRESHOLD):
        assert login(anon, "bara", "wrong").status_code == 401
    assert login(anon, "bara").status_code == 401  # locked, uniform message
    import db
    from sqlalchemy import select

    with db.engine().connect() as conn:
        row = conn.execute(select(db.app_user).where(db.app_user.c.id == "bara")).first()
    assert row.locked_until is not None and row.failed_login_count == config.LOCKOUT_THRESHOLD


def test_lockout_backoff_doubles_and_caps():
    import config
    import security

    t = config.LOCKOUT_THRESHOLD
    assert security.lockout_seconds(t - 1) == 0
    assert security.lockout_seconds(t) == 60 and security.lockout_seconds(t + 1) == 120
    assert security.lockout_seconds(t + 20) == config.LOCKOUT_MAX_SECONDS


def test_login_rate_limit_per_ip(anon):
    for _ in range(5):
        login(anon, "vojta", "wrong")
    assert login(anon, "vojta").status_code == 429


def test_logout_revokes_the_session_server_side(as_user):
    client = as_user("vojta")
    stolen = client.cookies.get("__Host-inbody_session")
    assert post(client, "/logout").status_code == 303
    from app import app
    from fastapi.testclient import TestClient

    thief = TestClient(app, base_url="https://testserver", follow_redirects=False)
    thief.cookies.set("__Host-inbody_session", stolen)
    assert thief.get("/history").status_code == 303


def test_disabled_user_cannot_log_in_and_loses_sessions(as_user, anon):
    client = as_user("bara")
    import db
    import security
    from sqlalchemy import update

    with db.engine().begin() as conn:
        conn.execute(update(db.app_user).where(db.app_user.c.id == "bara").values(is_disabled=True))
        security.revoke_all_sessions(conn, "bara")
    assert client.get("/history").status_code == 303
    assert login(anon, "bara").status_code == 401


def test_passwords_are_argon2id_phc_strings_never_plaintext(env):
    import db
    from sqlalchemy import select

    with env.connect() as conn:
        hashes = conn.execute(select(db.app_user.c.password_hash)).scalars().all()
    assert all(h.startswith("$argon2id$v=19$m=19456,t=2,p=1$") for h in hashes)
    assert not any(pw in h for h in hashes for pw in PASSWORDS.values())


@pytest.mark.parametrize("xff, expected", [("1.2.3.4", "1.2.3.4"), ("6.6.6.6, 1.2.3.4", "1.2.3.4")])
def test_client_ip_is_the_last_forwarded_hop(monkeypatch, xff, expected):
    import app as app_module
    import config

    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    request = type("R", (), {"headers": {"x-forwarded-for": xff}, "client": None})()
    assert app_module.client_ip(request) == expected


def test_generated_passwords_are_24_alphanumeric_characters():
    import security

    seen = {security.generate_password() for _ in range(200)}
    assert len(seen) == 200
    assert all(len(p) == 24 and p.isascii() and p.isalnum() for p in seen)


# ------------------------------------------------------------------ canonical host (migration)


def test_other_hosts_redirect_to_the_canonical_base_url(anon, monkeypatch):
    """After a move to a custom domain the old *.azurecontainerapps.io host must not serve a second
    origin (separate cookies): GET / HEAD are redirected to PUBLIC_BASE_URL, path + query kept."""
    import config

    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://inbody.example.test/")
    r = anon.get("/login?x=1", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "https://inbody.example.test/login?x=1"
    assert anon.head("/", follow_redirects=False).status_code == 308
    ok = anon.get("/login", headers={"host": "inbody.example.test"}, follow_redirects=False)
    assert ok.status_code == 200
    ok = anon.get("/login", headers={"host": "INBODY.example.test:443"}, follow_redirects=False)
    assert ok.status_code == 200  # case and port do not matter


def test_canonical_redirect_spares_health_probes_unsafe_methods_and_an_unset_url(anon, monkeypatch):
    import config

    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://inbody.example.test")
    assert anon.get("/healthz", follow_redirects=False).status_code == 200
    assert anon.get("/dbstatus", follow_redirects=False).status_code != 308
    assert anon.post("/login", data={}, follow_redirects=False).status_code != 308
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    assert anon.get("/login", follow_redirects=False).status_code == 200
