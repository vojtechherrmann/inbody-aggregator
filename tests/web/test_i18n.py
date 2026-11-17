"""Czech + English, same keys; language from the cookie, else Accept-Language, else Czech."""

import json
from pathlib import Path

import i18n
from conftest import csrf_of

I18N = Path(__file__).resolve().parents[2] / "src" / "web" / "i18n"


def test_both_languages_have_the_same_keys():
    cs = json.loads((I18N / "cs.json").read_text())
    en = json.loads((I18N / "en.json").read_text())
    assert set(cs) == set(en)
    assert all(v.strip() for v in [*cs.values(), *en.values()])


def test_pick_lang():
    assert i18n.pick_lang(None, None) == "cs"
    assert i18n.pick_lang(None, "en-US,en;q=0.9") == "en"
    assert i18n.pick_lang(None, "de-DE,cs;q=0.5") == "cs"
    assert i18n.pick_lang("en", "cs") == "en"
    assert i18n.pick_lang("xx", "fr") == "cs"


def test_landing_is_bilingual_and_shows_the_contact(anon):
    cs = anon.get("/").text
    assert "Sbíráme výsledky" in cs and "mailto:owner@example.test" in cs
    assert "Kontaktovat Vojtu</a>" in cs  # the one shared contact label
    assert "VojtaH" in cs  # centred copyright footer
    en = anon.get("/", headers={"Accept-Language": "en"}).text
    assert "Contact Vojta</a>" in en


def test_language_switch_sets_a_cookie_and_stays_on_site(anon):
    resp = anon.post("/lang", data={"lang": "en", "next": "/login", "csrf_token": csrf_of(anon)})
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert "Login" in anon.get("/login").text and "Log in" not in anon.get("/login").text
    evil = anon.post(
        "/lang", data={"lang": "en", "next": "//evil.example", "csrf_token": csrf_of(anon)}
    )
    assert evil.headers["location"] == "/"


def test_no_hardcoded_contact_address_in_code_or_templates():
    web = Path(__file__).resolve().parents[2] / "src" / "web"
    for path in [*web.rglob("*.py"), *web.rglob("*.html"), *web.rglob("*.json")]:
        assert "@gmail.com" not in path.read_text(encoding="utf-8"), path
