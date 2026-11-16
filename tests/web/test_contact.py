"""ONE "Contact Vojta" mailto link everywhere: the same label, an address only from config, a
pre-filled subject per context (never a measured value), no link for the guest on private pages."""

from __future__ import annotations

import re

import config
import i18n
import pytest
from conftest import post
from test_guest import guest, seed_mod, seeded  # noqa: F401  (fixtures)
from test_ui import _import

LABELS = {"cs": "Kontaktovat Vojtu", "en": "Contact Vojta"}
CONTEXTS = ("general", "forgot", "account", "delete", "extra", "upload", "error")
MAIL = re.compile(r'<a class="contact-link" href="(mailto:[^"]+)">([^<]+)</a>')


def _subjects(html: str) -> list[str]:
    from urllib.parse import parse_qs, unquote, urlparse

    out = []
    for href, _label in MAIL.findall(html):
        out.append(parse_qs(urlparse(unquote(href.replace("&amp;", "&"))).query)["subject"][0])
    return out


def test_the_strings_one_label_and_a_subject_for_every_context():
    for lang in ("cs", "en"):
        t = i18n.translator(lang)
        assert t("contact.link") == LABELS[lang]
        subjects = [t(f"contact.subject.{c}", id="X1", code=404) for c in CONTEXTS]
        assert len(set(subjects)) == len(CONTEXTS), subjects  # each context says what it is about
        assert all(len(s) <= 40 for s in subjects)
    # every sentence with the link uses the {contact} placeholder, in both languages
    for key in (
        "contact.text",
        "login.forgot",
        "login.no_account",
        "landing.no_registration",
        "error.contact",
        "privacy.delete",
    ):
        assert all("{contact}" in i18n.TABLES[lang][key] for lang in ("cs", "en")), key


@pytest.mark.parametrize("lang", ["cs", "en"])
def test_every_page_with_a_contact_offer_uses_the_same_label(anon, as_user, lang):
    headers = {"Accept-Language": lang}
    pages = {
        "forgot": ("/login", "forgot"),
        "account": ("/login", "account"),
        "landing": ("/", "account"),
        "data": ("/data", "delete"),
        "404": ("/no-such-page", "error"),
    }
    for name, (path, context) in pages.items():
        html = anon.get(path, headers=headers).text
        links = MAIL.findall(html)
        links = [(h, label) for h, label in links if label != "VojtaH"]  # the footer name
        assert links and all(label == LABELS[lang] for _h, label in links), name
        t = i18n.translator(lang)
        assert t(f"contact.subject.{context}", id="", code=404).strip() in _subjects(html), name
        assert "owner@example.test" in html
    # the logged-in pages: the upload report, error pages (the user id goes into the subject)
    upload_id = _import()
    vojta = as_user("vojta")
    report = vojta.get(f"/uploads/{upload_id}", headers=headers).text
    assert f"{i18n.translator(lang)('contact.subject.upload', id=upload_id)} (vojta)" in _subjects(
        report
    )
    err = vojta.get("/no-such-page", headers=headers).text  # an unmatched route: no session context
    assert i18n.translator(lang)("contact.subject.error", code=404) in _subjects(err)
    assert all(
        label in (LABELS[lang], "VojtaH") for _h, label in MAIL.findall(report + err)
    )  # VojtaH = the footer name, also a mailto link


def test_the_address_only_comes_from_config_and_nothing_leaks_elsewhere(anon, monkeypatch):
    monkeypatch.setattr(config, "CONTACT_EMAIL", "someone@else.example")
    html = anon.get("/login").text
    assert "mailto:someone@else.example?subject=" in html and "owner@example.test" not in html
    assert not [c for c in anon.cookies.jar if "someone@else" in (c.value or "")]
    # nothing but the one link: the address is not in the scripts or in the data blocks
    assert html.count("someone@else.example") == len(MAIL.findall(html))


def test_without_an_address_the_label_is_plain_text_and_nothing_crashes(anon, as_user, monkeypatch):
    monkeypatch.setattr(config, "CONTACT_EMAIL", "")
    upload_id = _import()
    vojta = as_user("vojta")
    for client, path in (
        (anon, "/"),
        (anon, "/login"),
        (anon, "/data"),
        (anon, "/no-such-page"),
        (vojta, f"/uploads/{upload_id}"),
        (vojta, "/no-such-page"),
    ):
        resp = client.get(path)
        assert resp.status_code in (200, 404), path
        assert "mailto:" not in resp.text and "contact-link" not in resp.text, path
        assert LABELS["cs"] in resp.text, path  # the label stays, as plain text


def test_a_refused_upload_offers_the_right_contact(as_user):
    client = as_user("vojta")
    resp = post(client, "/upload", files={"file": ("x.txt", b"hello", "text/plain")})
    assert resp.status_code == 415, resp.status_code
    assert "Kontaktovat Vojtu</a>" in resp.text
    assert "InBody%3A%20nahr%C3%A1n%C3%AD%20%28vojta%29" in resp.text  # "InBody: nahrání (vojta)"


def test_the_guest_gets_a_contact_link_only_on_the_public_pages(guest, anon):  # noqa: F811
    for path in ("/no-such-page", "/history", "/report", "/sharing"):
        resp = guest.get(path)
        assert "contact-link" not in resp.text and "mailto:" not in resp.text, path
    assert "contact-link" in guest.get("/data").text  # public page
