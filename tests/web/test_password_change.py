"""Self-service password change (/account/password), the password policy, generated passwords,
the show/hide toggle and the "forgot your password" line on the login page."""

from __future__ import annotations

import logging

import i18n
import pytest
from conftest import PASSWORDS, _client, csrf_of, login, post

URL = "/account/password"
NEW = "Novy-Heslo-2026"
T = i18n.translator("cs")


def _user_row(uid: str):
    import db
    from sqlalchemy import select

    with db.engine().connect() as conn:
        return conn.execute(select(db.app_user).where(db.app_user.c.id == uid)).first()


def _change(client, current=None, new=NEW, repeat=None, **extra):
    data = {
        "current_password": PASSWORDS["vojta"] if current is None else current,
        "new_password": new,
        "new_password_repeat": new if repeat is None else repeat,
        **extra,
    }
    return post(client, URL, data=data)


def _unchanged(uid: str = "vojta") -> bool:
    import security

    return security.verify_password(_user_row(uid).password_hash, PASSWORDS[uid])


# ----------------------------------------------------------------------------------- the policy


def test_policy_accepts_a_password_meeting_every_rule():
    import security

    assert security.password_policy_errors("Abcdefg1") == []
    assert security.password_policy_errors("Žluťoučký kůň 7") == []  # non-ASCII letters count


@pytest.mark.parametrize(
    ("password", "rule"),
    [
        ("Abcdef1", "length"),
        ("abcdefg1", "upper"),
        ("ABCDEFG1", "lower"),
        ("Abcdefgh", "digit"),
    ],
)
def test_policy_names_each_broken_rule(password, rule):
    import security

    assert security.password_policy_errors(password) == [rule]


def test_policy_lists_every_broken_rule_in_a_stable_order():
    import security

    assert security.password_policy_errors("") == ["length", "upper", "lower", "digit"]


def test_generated_passwords_always_meet_the_policy():
    import security

    for _ in range(300):
        pw = security.generate_password()
        assert len(pw) == 24 and pw.isalnum() and security.password_policy_errors(pw) == []


def test_generate_password_redraws_until_the_policy_holds(monkeypatch):
    """24 random alphanumerics can lack a case or a digit -- such a draw must be thrown away."""
    import security

    draws = iter("a" * 24 + "aB3" * 8)
    monkeypatch.setattr(security.secrets, "choice", lambda _seq: next(draws))
    assert security.generate_password() == "aB3" * 8


# ------------------------------------------------------------------------------------ the route


def test_form_has_the_three_fields_and_the_rules(as_user):
    resp = as_user("vojta").get(URL)
    assert resp.status_code == 200
    html = resp.text
    assert html.count('type="password"') == 3
    assert 'name="current_password"' in html and 'autocomplete="current-password"' in html
    assert 'name="new_password"' in html and 'name="new_password_repeat"' in html
    assert html.count('autocomplete="new-password"') == 2
    assert 'maxlength="1024"' in html
    for rule in ("length", "upper", "lower", "digit"):
        assert T(f"account.pw.rule.{rule}", n=8) in html


def test_unauthenticated_get_goes_to_login_and_post_is_refused(anon):
    resp = anon.get(URL)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    resp = anon.post(
        URL,
        data={
            "current_password": PASSWORDS["vojta"],
            "new_password": NEW,
            "new_password_repeat": NEW,
            "csrf_token": csrf_of(anon),
        },
    )
    assert resp.status_code == 401 and _unchanged()


def test_csrf_token_is_required(as_user):
    client = as_user("vojta")
    resp = client.post(
        URL,
        data={
            "current_password": PASSWORDS["vojta"],
            "new_password": NEW,
            "new_password_repeat": NEW,
        },
    )
    assert resp.status_code == 403 and _unchanged()
    resp = _change(client, csrf_token="forged")
    assert resp.status_code == 403 and _unchanged()


def test_wrong_current_password_is_refused_and_counts_as_a_failed_login(as_user):
    client = as_user("vojta")
    resp = _change(client, current="Wrong-current-1")
    assert resp.status_code == 400
    assert T("account.pw.err.wrong_current") in resp.text
    assert "Wrong-current-1" not in resp.text and NEW not in resp.text  # never echoed
    assert _unchanged() and _user_row("vojta").failed_login_count == 1


def test_wrong_current_password_shares_the_login_lockout(as_user, anon, monkeypatch):
    import config

    monkeypatch.setattr(config, "PASSWORD_CHANGE_LIMITS_PER_USER", ((100, 60),))
    client = as_user("vojta")
    for _ in range(config.LOCKOUT_THRESHOLD):
        assert _change(client, current="Wrong-current-1").status_code == 400
    row = _user_row("vojta")
    assert row.failed_login_count == config.LOCKOUT_THRESHOLD and row.locked_until is not None
    # locked: even the right current password does not change anything, and login is locked too
    resp = _change(client)
    assert resp.status_code == 429 and _unchanged()
    assert login(anon, "vojta").status_code == 401


def test_new_passwords_must_match(as_user):
    client = as_user("vojta")
    resp = _change(client, repeat=NEW + "x")
    assert resp.status_code == 400 and T("account.pw.err.mismatch") in resp.text
    assert NEW not in resp.text and _unchanged()


@pytest.mark.parametrize(
    ("weak", "rule"),
    [
        ("Abcde1", "length"),
        ("abcdefgh1", "upper"),
        ("ABCDEFGH1", "lower"),
        ("Abcdefghij", "digit"),
    ],
)
def test_each_policy_rule_is_enforced_server_side(as_user, weak, rule):
    client = as_user("vojta")
    resp = _change(client, new=weak)
    assert resp.status_code == 400
    assert T(f"account.pw.err.{rule}", n=8) in resp.text
    assert weak not in resp.text and _unchanged()


def test_new_password_must_differ_from_the_current_one(as_user, env):
    import db
    import security
    from sqlalchemy import update

    same = "Same-Password-1"  # meets the policy, so only the "same" rule can refuse it
    with env.begin() as conn:
        conn.execute(
            update(db.app_user)
            .where(db.app_user.c.id == "bara")
            .values(password_hash=security.hash_password(same))
        )
    client = _client()
    assert login(client, "bara", same).status_code == 303
    resp = post(
        client,
        URL,
        data={"current_password": same, "new_password": same, "new_password_repeat": same},
    )
    assert resp.status_code == 400 and T("account.pw.err.same") in resp.text


def test_overlong_password_is_refused(as_user):
    client = as_user("vojta")
    long = "Aa1" * 400  # 1200 chars > 1024
    assert _change(client, new=long).status_code == 400
    assert _unchanged()


def test_attempts_are_rate_limited_per_user(as_user, monkeypatch):
    import config

    monkeypatch.setattr(config, "PASSWORD_CHANGE_LIMITS_PER_USER", ((2, 60),))
    client = as_user("vojta")
    for _ in range(2):
        assert _change(client, repeat="nope").status_code == 400
    resp = _change(client)
    assert resp.status_code == 429 and T("account.pw.err.rate_limited") in resp.text
    assert _unchanged()
    # another user has an own bucket
    other = as_user("bara")
    assert (
        post(
            other,
            URL,
            data={
                "current_password": PASSWORDS["bara"],
                "new_password": NEW,
                "new_password_repeat": NEW,
            },
        ).status_code
        == 303
    )


def test_success_revokes_other_sessions_and_keeps_this_one(as_user, anon):
    import security

    client = as_user("vojta")
    elsewhere = as_user("vojta")
    old_session = client.cookies.get("__Host-inbody_session")
    old_csrf = client.cookies.get("__Host-inbody_csrf")
    before = _user_row("vojta").password_changed_at

    resp = _change(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"{URL}?notice=notice.password_changed"
    new_session = client.cookies.get("__Host-inbody_session")
    assert new_session and new_session != old_session
    assert client.cookies.get("__Host-inbody_csrf") != old_csrf  # rotated

    # this device: still logged in, sees the success message
    page = client.get(resp.headers["location"])
    assert page.status_code == 200 and T("notice.password_changed") in page.text
    assert client.get("/history").status_code == 200
    # every other session (and the old cookie of this device) is gone
    assert elsewhere.get("/history").status_code == 303
    thief = _client()
    thief.cookies.set("__Host-inbody_session", old_session)
    assert thief.get("/history").status_code == 303

    row = _user_row("vojta")
    assert row.password_hash.startswith("$argon2id$") and NEW not in row.password_hash
    assert security.verify_password(row.password_hash, NEW)
    assert row.password_changed_at > before and row.failed_login_count == 0
    assert login(anon, "vojta").status_code == 401  # old password
    assert login(_client(), "vojta", NEW).status_code == 303


def test_no_password_is_ever_logged(as_user, caplog):
    caplog.set_level(logging.DEBUG)
    client = as_user("vojta")
    _change(client, current="Wrong-current-1")
    _change(client, repeat="Mismatch-123")
    _change(client)
    for secret in (PASSWORDS["vojta"], "Wrong-current-1", "Mismatch-123", NEW):
        assert secret not in caplog.text


# ------------------------------------------------------------------- toggle + forgotten password


def test_password_toggle_script_is_loaded_with_both_languages(anon):
    cs = anon.get("/login").text
    assert 'src="/static/pwtoggle.js?v=' in cs and T("pw.show") in cs
    en = anon.get("/login", headers={"Accept-Language": "en"}).text
    assert i18n.translator("en")("pw.show") in en
    js = anon.get("/static/pwtoggle.js")
    assert js.status_code == 200 and "aria-pressed" in js.text and "eval" not in js.text


def test_login_page_offers_the_contact_link_for_a_forgotten_password(anon, monkeypatch):
    cs = anon.get("/login").text
    assert (
        "Zapomenuté heslo?" in cs and 'class="contact-link"' in cs and "Kontaktovat Vojtu</a>" in cs
    )
    assert "mailto:owner@example.test?subject=InBody%3A%20zapomenut%C3%A9%20heslo" in cs
    en = anon.get("/login", headers={"Accept-Language": "en"}).text
    assert "Forgot your password?" in en and "Contact Vojta</a>" in en

    import config

    monkeypatch.setattr(config, "CONTACT_EMAIL", "")  # no address: the label as plain text, no link
    plain = anon.get("/login").text
    assert (
        "Kontaktovat Vojtu" in plain
        and "mailto:" not in plain
        and 'class="contact-link"' not in plain
    )


# ------------------------------------------------------------- "your password was set by Vojta"

ADMIN_SET = "notice.password_admin_set"


def _mark_admin_set(uid: str, value: bool = True) -> None:
    import db
    from sqlalchemy import update

    with db.engine().begin() as conn:
        conn.execute(
            update(db.app_user).where(db.app_user.c.id == uid).values(password_admin_set=value)
        )


def test_login_recommends_a_change_only_while_the_password_is_admin_set(anon):
    resp = login(anon, "vojta")
    assert resp.status_code == 303 and resp.headers["location"] == "/history"  # flag 0: nothing
    _mark_admin_set("vojta")
    fresh = _client()
    resp = login(fresh, "vojta")
    assert resp.status_code == 303 and resp.headers["location"] == f"/history?notice={ADMIN_SET}"
    page = fresh.get(resp.headers["location"]).text
    assert T(ADMIN_SET) == "Heslo ti nastavil Vojta. Doporučujeme si ho změnit."
    assert T(ADMIN_SET) in page and f'href="{URL}"' in page
    en = i18n.translator("en")(ADMIN_SET)
    assert en == "Your password was set by Vojta. We recommend changing it."
    # once per login, never blocking: every page works without changing the password
    plain = fresh.get("/history")
    assert plain.status_code == 200 and T(ADMIN_SET) not in plain.text
    for url in ("/report", "/upload", "/sharing", URL):
        assert fresh.get(url).status_code == 200, url


def test_a_self_service_change_clears_the_admin_set_flag(as_user):
    _mark_admin_set("vojta")
    client = as_user("vojta")
    assert _user_row("vojta").password_admin_set
    assert _change(client).status_code == 303
    assert not _user_row("vojta").password_admin_set
    resp = login(_client(), "vojta", NEW)
    assert resp.headers["location"] == "/history"  # no recommendation any more


def test_a_refused_change_keeps_the_admin_set_flag(as_user):
    _mark_admin_set("vojta")
    client = as_user("vojta")
    assert _change(client, repeat="Something-Else-2026").status_code == 400
    assert _user_row("vojta").password_admin_set
