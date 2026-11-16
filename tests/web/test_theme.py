"""Light / dark / system theme: a settings-cookie value rendered server-side as <html data-theme>
(dark by default; none for "system"), and the two dark-token blocks of app.css kept identical."""

from __future__ import annotations

import re
from pathlib import Path

import views
from conftest import post

STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
PROTOTYPE = Path(__file__).resolve().parents[2] / "docs" / "prototype"


def _units_cookie(resp) -> str | None:
    for k, v in resp.headers.multi_items():
        if k == "set-cookie" and v.startswith("inbody_units="):
            return v.split(";", 1)[0].split("=", 1)[1]
    return None


def test_theme_in_the_settings_cookie():
    """DEFAULT = dark. The cookie always carries the theme explicitly (dark | light | system)."""
    assert views.Settings().theme == "dark" and views.Settings().cookie() == "metric.kcal.3.dark"
    for theme in ("dark", "light", "system"):
        s = views.Settings(theme=theme)
        assert s.cookie() == f"metric.kcal.3.{theme}" and views.parse_settings(s.cookie()) == s
    assert views.parse_settings("imperial.kJ.6.light").theme == "light"  # legacy 4-part cookie
    # an old cookie WITHOUT a theme part is dark (the new default), not system
    assert views.parse_settings("imperial.kJ.6") == views.Settings("imperial", "kJ", "dark")
    assert (
        views.parse_settings(None) == views.Settings()
        and views.parse_settings("") == views.Settings()
    )
    # unknown theme values mean dark; a broken cookie is the default (dark too)
    assert views.parse_settings("imperial.kJ.3.purple") == views.Settings("imperial", "kJ", "dark")
    for bad in ("metric.kcal.3.dark.x", "a.b.c.d"):
        assert views.parse_settings(bad) == views.Settings(), bad
    assert views.settings_from_form("metric", "kcal", "neon").theme == "dark"
    assert views.settings_from_form("metric", "kcal", None).theme == "dark"
    assert views.settings_from_form("metric", "kcal", "system").theme == "system"
    # <html data-theme>: dark and light are explicit, system renders NO attribute
    assert views.Settings(theme="dark").data_theme == "dark"
    assert views.Settings(theme="light").data_theme == "light"
    assert views.Settings(theme="system").data_theme is None


def test_default_is_dark_whatever_the_os_prefers_and_system_renders_no_attribute(as_user):
    client = as_user("vojta")
    page = client.get("/history").text  # no cookie: dark, the OS preference is not consulted
    assert '<html lang="cs" data-theme="dark">' in page
    assert 'value="dark" checked' in page and 'value="light" checked' not in page
    assert page.index('value="dark"') < page.index('value="light"') < page.index('value="system"')
    for choice in ("light", "dark"):
        resp = post(
            client, "/settings", data={"theme": choice, "mass": "imperial", "next": "/report"}
        )
        assert resp.status_code == 303 and _units_cookie(resp) == f"imperial.kcal.3.{choice}"
        page = client.get("/report").text
        assert f'<html lang="cs" data-theme="{choice}">' in page
        assert f'value="{choice}" checked' in page  # the radio shows the stored choice
    resp = post(client, "/settings", data={"theme": "system"})
    assert _units_cookie(resp) == "metric.kcal.3.system"
    page = client.get("/history").text
    assert "data-theme" not in page and 'value="system" checked' in page


def test_an_invalid_theme_means_dark(as_user):
    client = as_user("vojta")
    resp = post(client, "/settings", data={"theme": "<script>", "mass": "metric"})
    assert _units_cookie(resp) == "metric.kcal.3.dark"
    client.cookies.set("inbody_units", "metric.kcal.3.neon")
    assert 'data-theme="dark"' in client.get("/history").text
    client.cookies.set("inbody_units", "metric.kcal.3")  # an old cookie without a theme part
    assert 'data-theme="dark"' in client.get("/history").text


def test_the_public_pages_follow_the_cookie_too(anon):
    assert 'data-theme="dark"' in anon.get("/login").text  # default, no cookie
    anon.cookies.set("inbody_units", "metric.kcal.3.light")
    assert 'data-theme="light"' in anon.get("/login").text
    anon.cookies.set("inbody_units", "metric.kcal.3.system")
    assert "data-theme" not in anon.get("/login").text


def test_the_prototype_has_the_same_appearance_group_and_default():
    html = (PROTOTYPE / "index.html").read_text(encoding="utf-8")
    assert [html.index(f'id="set-theme-{v}"') for v in ("dark", "light", "system")] == sorted(
        html.index(f'id="set-theme-{v}"') for v in ("dark", "light", "system")
    )
    js = (PROTOTYPE / "app.js").read_text(encoding="utf-8")
    assert 'saved.theme === "light" || saved.theme === "system" ? saved.theme : "dark"' in js
    assert 'removeAttribute("data-theme")' in js and 'removeAttribute("data-theme")' in (
        PROTOTYPE / "theme.js"
    ).read_text(encoding="utf-8")
    i18n = (PROTOTYPE / "i18n.js").read_text(encoding="utf-8")
    for label in (
        "Appearance",
        "Dark",
        "Light",
        "System",
        "Vzhled",
        "Tmavý",
        "Světlý",
        "Podle systému",
    ):
        assert f'"{label}"' in i18n, label


def _dark_blocks(css: str) -> list[str]:
    bodies = []
    for head in (
        '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {',
        ':root[data-theme="dark"] {',
    ):
        start = css.index(head) + len(head)
        end = css.index("}", start)
        bodies.append(" ".join(css[start:end].split()))
    return bodies


def test_the_two_dark_token_blocks_are_identical():
    for css in (STATIC / "app.css", PROTOTYPE / "ui.css"):
        text = css.read_text(encoding="utf-8")
        auto, explicit = _dark_blocks(text)
        assert auto == explicit and "--page:" in auto and "color-scheme: dark" in auto, css.name
        assert "color-scheme: dark" in auto


def test_every_dark_rule_yields_to_an_explicit_light_choice():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for m in re.finditer(r"@media \(prefers-color-scheme: dark\) \{\s*([^{}]+)\{", css):
        assert ':not([data-theme="light"])' in m.group(1), m.group(1)
    # the shared chart module reads only the tokens: it must not look at the OS preference itself
    for js in ("charts.js", "history.js", "shell.js"):
        assert "prefers-color-scheme" not in (STATIC / js).read_text(encoding="utf-8")
    assert "prefers-color-scheme" not in (STATIC / "charts.css").read_text(encoding="utf-8")


def test_the_chart_module_is_identical_in_the_prototype():
    for name in ("charts.js", "charts.css"):
        assert (STATIC / name).read_bytes() == (PROTOTYPE / name).read_bytes(), name
