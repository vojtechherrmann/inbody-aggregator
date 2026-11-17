"""The ported UI (docs/ui-design.md): app bar, Report / History, settings, sharing, uploads.

Synthetic data only (helpers.csv_export)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pipeline
from conftest import post
from helpers import csv_export, png

TWO_DAYS = [("20260601080000", 190.0), ("20260615080000", 187.0)]


def _import(rows=TWO_DAYS, user="vojta") -> str:
    upload_id = pipeline.admit(user, "fake.csv", csv_export(rows), source_name="City Gym")
    assert pipeline.process_upload(upload_id) == "done"
    return upload_id


def _json_block(html: str, block_id: str):
    m = re.search(rf'<script type="application/json" id="{block_id}">(.*?)</script>', html, re.S)
    assert m, block_id
    return json.loads(m.group(1))


def test_no_inline_script_on_any_page(as_user, anon):
    upload_id = _import()
    vojta = as_user("vojta")
    pages = [
        vojta.get(p).text
        for p in ("/report", "/history", "/upload", "/sharing", f"/uploads/{upload_id}")
    ]
    pages += [anon.get("/").text, anon.get("/login").text]
    for html in pages:
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert 'src="/static/' in tag or 'type="application/json"' in tag, tag
        assert " style=" not in html and "onclick" not in html


def test_history_embeds_rows_and_strings_for_the_chart_module(as_user):
    _import()
    page = as_user("vojta").get("/history").text
    assert 'aria-current="page">Historie' in page and "/static/charts.js" in page
    data = _json_block(page, "history-data")
    assert [r["measured_date"] for r in data["measurements"]] == ["2026-06-01", "2026-06-15"]
    assert data["measurements"][0]["gym"] == "City Gym"  # CSV rows take the gym typed at upload
    assert data["units"] == {"mass": "metric", "energy": "kcal"}
    assert "shrinkPauses" not in data and "pauseMonths" not in data  # browser-side controls
    assert data["reportUrl"] == "/report?subject=vojta" and data["uploadUrl"] == "/upload"
    strings = _json_block(page, "i18n-data")
    assert strings["hc.pauseOver"] == "Zkrátit pauzy nad" and strings["fmt.locale"] == "cs-CZ"
    assert "hc.shrinkPauses" not in strings and 'id="ibc-shrink-pauses"' not in page


def test_report_shows_the_latest_with_directional_deltas(as_user):
    _import()
    page = as_user("vojta").get("/report").text
    assert "po 15. 6. 2026" in page  # latest by default, Czech long date
    assert '<span class="delta down neutral">▼' in page  # weight: no verdict -> neutral colour
    assert "p.p." in page or "p.p." in page  # % changes in percentage points
    first = as_user("vojta").get("/report?m=" + _first_id()).text
    assert (
        'class="delta none"' in first and "delta down" not in first and "delta up" not in first
    )  # nothing earlier: empty


def _first_id() -> str:
    import db
    from sqlalchemy import select

    with db.engine().connect() as conn:
        return conn.execute(
            select(db.measurement.c.id).order_by(db.measurement.c.measured_date)
        ).scalar()


def test_report_unknown_measurement_is_a_plain_404(as_user):
    _import()
    assert as_user("vojta").get("/report?m=nope").status_code == 404


def test_settings_set_language_and_units_and_the_report_follows(as_user):
    _import()
    vojta = as_user("vojta")
    resp = post(
        vojta,
        "/settings",
        data={
            "lang": "en",
            "mass": "imperial",
            "energy": "kJ",
            "shrink": ["0", "1"],
            "next": "/report",
        },
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/report"
    page = vojta.get("/report").text
    assert "Mon 15 Jun 2026" in page and " lb" in page
    data = _json_block(vojta.get("/history").text, "history-data")
    assert data["units"] == {"mass": "imperial", "energy": "kJ"}
    bad = post(vojta, "/settings", data={"mass": "stone", "next": "//evil"})
    assert bad.headers["location"] == "/"


def test_viewing_selector_lists_only_viewable_people_and_shared_view_is_read_only(as_user):
    _import()
    bara = as_user("bara")
    alone = bara.get("/report").text
    assert 'value="vojta"' not in alone
    # nobody shared with bara: a plain name (still labelled for screen readers), no dropdown
    assert 'class="who-static"' in alone and '<select id="who"' not in alone
    assert "<label" not in alone.split('class="tabrow"')[1].split("</nav>")[0]  # no visible label
    post(as_user("vojta"), "/sharing/grant", data={"username": "bara"})
    page = bara.get("/report?subject=vojta").text
    assert 'value="vojta" selected' in page and 'aria-label="Zobrazuji"' in page
    assert (
        "sdíleno s tebou" not in page and "(já)" in page
    )  # own entry: (já); others: just the name
    assert ">vojta</option>" in page and ">bara (já)</option>" in page
    assert "icon-danger" not in page and "/delete" not in page  # no bin for a shared viewer
    # Upload and Sharing are about MY account: enabled while looking at a friend's data too
    assert 'href="/upload"' in page and 'href="/sharing"' in page
    assert 'aria-disabled="true" title="Nahrává se vždy' not in page
    assert bara.get("/history?subject=vojta").status_code == 200
    admin = as_user("vojta-admin").get("/report").text
    assert 'value="vojta"' in admin and 'value="bara"' in admin


def test_data_handling_is_its_own_page_linked_from_the_footer(anon, as_user):
    for html in (
        anon.get("/").text,
        anon.get("/login").text,
        as_user("vojta").get("/history").text,
    ):
        assert "<details>" not in html or "datainfo" not in html  # no box in the page body
        assert 'href="/data">Jak se nakládá s daty</a>' in html  # footer link
        assert "VojtaH" in html and '<span class="build">' in html  # copyright + commit · revision
    page = anon.get("/data")  # public: visitors read it before asking for an account
    assert page.status_code == 200
    assert "<h1>Jak se nakládá s daty</h1>" in page.text
    assert "po 30 dnech je maže" in page.text and page.text.count("<li>") >= 12
    en = anon.get("/data", headers={"Accept-Language": "en"}).text
    assert "How your data is handled" in en and "deletes it after 30 days" in en


def test_failed_upload_shows_only_the_localized_reason(as_user):
    bad = pipeline.admit("vojta", "tiny.png", png(width=200, height=200))
    assert pipeline.process_upload(bad) == "failed"
    vojta = as_user("vojta")
    for page in (vojta.get(f"/uploads/{bad}").text, vojta.get("/upload").text):
        assert "Obrázek je moc malý na přečtení." in page
        assert "the picture is too small" not in page  # never the raw English exception text
    assert "návod doplníme" not in vojta.get("/upload").text


def test_upload_page_has_the_form_and_the_three_phase_bar(as_user):
    page = as_user("vojta").get("/upload").text
    assert 'id="upload-form"' in page and 'name="source_name"' in page
    assert page.count('class="seg"') == 3 and "/static/upload.js" in page


def test_sharing_revoke_needs_an_in_page_confirmation(as_user):
    vojta = as_user("vojta")
    post(vojta, "/sharing/grant", data={"username": "bara"})
    page = vojta.get("/sharing").text
    assert 'href="/sharing?revoke=bara"' in page and 'action="/sharing/revoke"' not in page
    confirm = vojta.get("/sharing?revoke=bara").text
    assert 'role="alertdialog"' in confirm and 'action="/sharing/revoke"' in confirm
    assert "vojta-admin" not in confirm  # never a list of all users


def test_every_report_section_has_its_info_box(as_user):
    """Report ⓘ: one per section, the shared component, term definitions + a short note."""
    import i18n

    _import()
    t = i18n.translator("cs")
    page = as_user("vojta").get("/report").text
    sections = re.findall(r'<details class="ibc-info" data-section="(\w+)">', page)
    assert sections == ["muscleFat", "obesity", "composition", "metabolism", "segmental"]
    assert "<details open" not in page
    for key in (
        "skeletal_muscle_mass_kg",
        "visceral_fat_level",
        "minerals_kg",
        "bmr_kcal",
        "seg_lean",
    ):
        assert t(f"def.{key}") in page, key
    assert t("rep.info.noteBar") in page and t("rep.info.noteChange") in page
    assert t("def.dry_lean_mass_kg") not in page  # optional rows: defined only when shown


def test_every_term_in_report_and_history_has_a_shared_definition_in_both_languages():
    """One def.* string per term, used by BOTH tabs (views.REPORT_INFO and charts.js defs())."""
    from pathlib import Path

    import i18n
    import views

    web = Path(__file__).resolve().parents[2] / "src" / "web"
    charts = (web / "static" / "charts.js").read_text(encoding="utf-8")
    block = re.search(r"const INFO_TERMS = \{(.*?)\};", charts, re.S).group(1)
    other = re.search(r"const OTHER = \[([^\]]*)\]", charts).group(1)
    history = set(re.findall(r'"([a-z_0-9]+)"', block + other))
    report = {k for keys, _note in views.REPORT_INFO.values() for k in keys}
    assert {"other_lean", "pct_of_weight", "seg_pct", "weight_kg", "bmi"} <= history
    for lang in ("cs", "en"):
        table = json.loads((web / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        missing = sorted(k for k in history | report if not table.get(f"def.{k}", "").strip())
        assert not missing, (lang, missing)
    # the same text on both tabs: the browser gets the def.* keys verbatim
    assert all(f"def.{k}" in i18n.client_table("cs") for k in history | report)


def _quote_pair(html):
    blocks = re.findall(
        r'<div class="quote (quote-[lr]) q(\d+)" aria-hidden="true"><p>(.*?)</p></div>', html
    )
    assert [b[0] for b in blocks] == ["quote-l", "quote-r"]
    return [int(b[1]) for b in blocks], [b[2].replace("&#39;", "'") for b in blocks]


def test_two_different_random_quotes_in_the_margins_as_pure_decoration(anon, as_user):
    """Server-side random, no JS: two DIFFERENT quotes per page, aria-hidden, no inline code."""
    import i18n

    table = i18n.TABLES["cs"]
    quotes = {table[f"quote.{n}"] for n in range(1, 17)}
    assert len(quotes) == 16 and set(i18n.TABLES["en"]) >= {f"quote.{n}" for n in range(1, 17)}
    seen = set()
    for _ in range(40):
        anon.cookies.clear()  # a new visitor: with no cookie any page draws once
        html = anon.get("/login").text
        ids, texts = _quote_pair(html)
        assert ids[0] != ids[1] and texts[0] != texts[1] and set(texts) <= quotes
        assert [table[f"quote.{n}"] for n in ids] == texts  # the class q<N> matches the text
        seen |= set(texts)
        assert " style=" not in html and "tabindex" not in html.split('class="quote')[1][:80]
    assert len(seen) > 2  # random across requests
    assert 'class="quote quote-l q' in as_user("vojta").get("/history").text  # every base.html page
    css = (Path(__file__).resolve().parents[2] / "src/web/static/app.css").read_text("utf-8")
    assert "@media (min-width: 1800px)" in css and "@media print" in css
    assert ".quote p::before" in css and "Fredoka" not in css  # ONE look for all 16 quotes


def test_a_new_quote_pair_only_on_report_and_history_never_repeating_a_quote(as_user):
    """The pair last shown rides in a cookie. Landing on Report / History (tab, person selector,
    stepper: all GET /report or /history) draws a NEW pair without either previous quote; every
    other page keeps the pair; so does the page right after a Settings change."""
    upload_id = _import()
    vojta = as_user("vojta")
    pair = _quote_pair(vojta.get("/history").text)[0]
    for path in ("/report", "/history", "/report?subject=vojta", "/history?subject=vojta") * 5:
        current = _quote_pair(vojta.get(path).text)[0]
        assert not set(current) & set(pair), (path, pair, current)
        pair = current
    for path in ("/sharing", "/upload", f"/uploads/{upload_id}", "/data", "/account/password"):
        resp = vojta.get(path)
        if resp.status_code == 200 and 'class="quote quote-l' in resp.text:
            assert _quote_pair(resp.text)[0] == pair, path
    # an htmx fragment (the upload list poll) is not a navigation: nothing changes
    cookie = vojta.cookies.get("inbody_quotes")
    assert cookie == ".".join(map(str, pair))
    vojta.get("/history/uploads", headers={"HX-Request": "true"})
    assert vojta.cookies.get("inbody_quotes") == cookie
    # a Settings round trip back to Report / History keeps the pair (one-shot flag) ...
    for target in ("/report", "/history"):
        assert post(vojta, "/settings", data={"theme": "light", "next": target}).status_code == 303
        assert _quote_pair(vojta.get(target).text)[0] == pair, target
        assert vojta.cookies.get("inbody_quotes_keep") is None  # ... and only once
        again = _quote_pair(vojta.get(target).text)[0]
        assert not set(again) & set(pair)
        pair = again
    # no cookie yet: any page draws once and remembers it
    vojta.cookies.delete("inbody_quotes")
    first = _quote_pair(vojta.get("/sharing").text)[0]
    assert vojta.cookies.get("inbody_quotes") == ".".join(map(str, first))
    assert _quote_pair(vojta.get("/upload").text)[0] == first
    # garbage in the cookie is ignored, never an error: it is drawn afresh
    vojta.cookies.set("inbody_quotes", "x.99.-3")
    assert len(set(_quote_pair(vojta.get("/report").text)[0])) == 2


def test_pauses_are_a_history_page_control_not_a_setting(as_user):
    """Settings has nothing about pauses; the control is client-side: the page ships the chart
    module and the strings for "[x] Shrink pauses over [N] mo", nothing server-side."""
    import i18n

    _import()
    page = as_user("vojta").get("/history").text
    settings = page.split('action="/settings"')[1].split("</form>")[0]
    assert "pause" not in settings.lower() and 'name="shrink"' not in settings
    strings = _json_block(page, "i18n-data")
    assert strings["hc.pauseOver"] == "Zkrátit pauzy nad" and strings["hc.pauseMo"] == "měs."
    assert "ibc-pause-months" not in page  # rendered by charts.js in the browser
    charts = (Path(__file__).resolve().parents[2] / "src/web/static/charts.js").read_text("utf-8")
    assert 'id: "ibc-shrink-pauses"' in charts and 'id: "ibc-pause-months"' in charts
    assert "inp.disabled = !cb.checked" in charts  # the number greys out while the box is off
    assert not any(
        k.startswith("settings.pause") or k == "settings.shrink_pauses" for k in i18n.TABLES["cs"]
    )


def test_empty_state_points_at_friends_results_only_when_someone_shared_theirs(as_user):
    hint = "výsledky kamarádů"
    vojta = as_user("vojta")  # nothing uploaded, nothing shared with him
    assert hint not in vojta.get("/report").text
    assert _json_block(vojta.get("/history").text, "history-data")["hasFriends"] is False
    post(as_user("bara"), "/sharing/grant", data={"username": "vojta"})
    assert hint in vojta.get("/report").text
    assert _json_block(vojta.get("/history").text, "history-data")["hasFriends"] is True
    strings = _json_block(vojta.get("/history").text, "i18n-data")
    assert hint in strings["hc.noDataOwnFriends"]
    # looking at the friend's (empty) page is not "your own" empty state
    assert hint not in vojta.get("/report?subject=bara").text


def test_top_block_is_frozen_only_on_roomy_screens():
    """App bar, tab row and the History filter row are pinned (sticky) only above 760 px AND from
    640 px height; the guest banner stays in the flow; shared charts.css is a byte copy."""
    root = Path(__file__).resolve().parents[2]
    css = (root / "src/web/static/app.css").read_text("utf-8")
    charts = (root / "src/web/static/charts.css").read_text("utf-8")
    query = "@media (min-width: 761px) and (min-height: 640px)"
    assert query in css and query in charts
    block = css.split(query, 1)[1].split("\n}\n", 1)[0]
    assert ".appbar { position: sticky; top: 0;" in block
    assert ".tabrow { position: sticky; top: calc(var(--bar-h) - 2px);" in block
    assert "background: var(--page)" in block and "scroll-padding-top" in block
    assert (
        "position: sticky; top: calc(var(--ibc-pin-top"
        in charts.split(query, 1)[1].split("}", 1)[0]
    )
    assert "sticky" not in css.replace(block, "").replace(
        "details.menu", ""
    )  # nothing pinned elsewhere
    assert "sticky" not in css.split("guest-banner", 1)[1].split("\n", 1)[0]
    assert (root / "docs/prototype/charts.css").read_bytes() == (
        root / "src/web/static/charts.css"
    ).read_bytes()


def test_the_custom_range_strings_reach_the_page_in_both_languages(as_user):
    """The History page ships the strings of the Custom month-range picker (client table hc.*);
    the picker itself is plain JS (charts.js) and is checked visually."""
    import i18n

    labels = {"cs": ("Vlastní", "Od", "Do"), "en": ("Custom", "From", "To")}
    for lang, (custom, frm, to) in labels.items():
        table = i18n.client_table(lang)
        assert (table["hc.rcustom"], table["hc.customFrom"], table["hc.customTo"]) == (
            custom,
            frm,
            to,
        )
        for key in ("customTitle", "customPrevYear", "customNextYear", "customDone"):
            assert table[f"hc.{key}"]
    _import()
    page = as_user("vojta").get("/history").text
    strings = _json_block(page, "i18n-data")
    assert strings["hc.rcustom"] == "Vlastní" and strings["hc.customTitle"]
    js = (Path(__file__).resolve().parents[2] / "src/web/static/charts.js").read_text("utf-8")
    assert 'type: "month"' not in js  # an own picker, no <input type="month"> (missing in Safari)
    assert 'role: "dialog"' in js and "aria-haspopup" in js


def test_the_vertical_view_notice_is_shown_by_css_only_at_narrow_width(anon):
    """A calm strip under the app bar on every page of base.html, both languages, dismissable by
    shell.js; the stylesheet shows it only inside @media (max-width: 760px); no inline code."""
    root = Path(__file__).resolve().parents[2]
    css = (root / "src/web/static/app.css").read_text("utf-8")
    assert ".mobile-notice { display: none;" in css
    assert "@media (max-width: 760px) { .mobile-notice { display: block; } }" in css
    assert "html.notice-off .mobile-notice { display: none; }" in css
    js = (root / "src/web/static/shell.js").read_text("utf-8")
    assert "inbody-mobile-notice-dismissed" in js and "notice-off" in js
    for lang, text, close in (
        ("cs", "Aplikace zatím není optimalizovaná pro vertikální pohled", "Zavřít"),
        ("en", "The app is not optimised for a vertical (portrait) view yet", "Close"),
    ):
        anon.cookies.set("inbody_lang", lang)
        for path in ("/", "/login"):
            html = anon.get(path).text
            assert 'class="band mobile-notice" role="note"' in html and text in html, (lang, path)
            assert f'aria-label="{close}"' in html and "data-dismiss-notice" in html
            assert " style=" not in html and " onclick=" not in html


def test_static_files_are_always_revalidated_so_a_deploy_never_mixes_new_html_with_old_css(anon):
    for path in ("/static/app.css", "/static/charts.js", "/static/shell.js"):
        resp = anon.get(path)
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-cache", path
        assert resp.headers.get("etag")
        again = anon.get(path, headers={"If-None-Match": resp.headers["etag"]})
        assert again.status_code == 304  # revalidation is cheap
    assert anon.get("/login").headers["cache-control"] == "no-store"


def test_asset_urls_carry_the_build_so_a_deploy_busts_the_browser_cache(anon, monkeypatch):
    import config

    monkeypatch.setattr(config, "APP_VERSION", "0123456789abcdef0123")
    html = anon.get("/login").text
    assert 'href="/static/app.css?v=0123456789ab"' in html
    assert 'src="/static/shell.js?v=0123456789ab"' in html


# ------------------------------------------------------------- the "starting the database" page


def _cold_database(monkeypatch):
    """Pretend to be on Azure SQL with a paused database: the warm-up probe blocks."""
    import threading

    import dbwarm

    release = threading.Event()
    dbwarm.reset_for_tests()
    monkeypatch.setattr(dbwarm, "enabled", lambda: True)
    monkeypatch.setattr(dbwarm, "FAST_WAIT", 0.05)
    monkeypatch.setattr(dbwarm, "_probe", lambda: release.wait(5))
    return dbwarm, release


def test_a_cold_database_shows_the_starting_page_then_the_real_page(as_user, anon, monkeypatch):
    vojta = as_user("vojta")  # logged in while the database is still "warm" (sqlite)
    dbwarm, release = _cold_database(monkeypatch)
    page = vojta.get("/history?subject=vojta", headers={"Accept-Language": "en"})
    assert page.status_code == 503 and page.headers["retry-after"] == "5"
    assert "Starting the database, this can take up to a minute" in page.text
    assert (
        'class="band db-banner"' in page.text
        and 'data-target="/history?subject=vojta"' in page.text
    )
    assert "/static/dbwait.js" in page.text and " style=" not in page.text
    assert vojta.get("/dbstatus").json() == {"warm": False, "session": True}
    # anonymous pages need no database: shown at once; /healthz never waits either
    assert anon.get("/").status_code == 200 and anon.get("/login").status_code == 200
    assert anon.get("/healthz").status_code == 200
    # a POST gets the page too, and sends the visitor back to the front page (not to re-POST)
    post_page = post(vojta, "/settings", data={"lang": "cs"})
    assert post_page.status_code == 503 and 'data-target="/"' in post_page.text
    release.set()  # the database is up
    for _ in range(100):
        if dbwarm.is_warm():
            break
        import time

        time.sleep(0.05)
    assert vojta.get("/dbstatus").json() == {"warm": True, "session": True}
    assert vojta.get("/history").status_code == 200


def test_a_database_that_answers_quickly_is_never_noticed(as_user, monkeypatch):
    import dbwarm

    vojta = as_user("vojta")
    dbwarm.reset_for_tests()
    monkeypatch.setattr(dbwarm, "enabled", lambda: True)
    monkeypatch.setattr(dbwarm, "_probe", lambda: None)  # answers at once
    assert vojta.get("/history").status_code == 200  # cold state, but the warm-up beats FAST_WAIT


def test_sqlite_is_always_warm(anon):
    import dbwarm

    assert dbwarm.is_warm() and anon.get("/dbstatus").json() == {"warm": True, "session": False}


def test_footer_has_a_mailto_name_and_a_homepage_link_and_the_landing_says_what_it_is_for(anon):
    import config

    html = anon.get("/").text
    footer = html.split("<footer")[1]
    assert 'class="contact-link" href="mailto:' in footer and ">VojtaH</a>" in footer
    assert '<a href="https://vojtah.cz" rel="noopener">Homepage</a>' in footer
    assert "sdílíme je s kamarády nebo trenéry" in html and "Sbíráme výsledky" in html
    en = anon.get("/", headers={"Accept-Language": "en"}).text
    assert "follow them over time and share them with friends or trainers" in en
    assert config.CONTACT_EMAIL  # the tests run with a contact address


def test_every_page_has_the_favicon_and_it_is_served_without_login(anon):
    page = anon.get("/").text
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg' in page
    resp = anon.get("/static/favicon.svg")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in resp.text and "<circle" in resp.text  # the little figure


def test_the_database_waiting_page_has_the_favicon_too():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src/web/templates/dbwait.html").read_text()
    assert 'rel="icon"' in src


def test_dbstatus_is_readable_by_the_start_page_only(anon):
    ok = anon.get("/dbstatus", headers={"Origin": "https://vojtah.cz"})
    assert ok.headers["access-control-allow-origin"] == "https://vojtah.cz"
    assert ok.headers["access-control-allow-credentials"] == "true"
    assert "Origin" in ok.headers["vary"]
    other = anon.get("/dbstatus", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers
    assert "access-control-allow-origin" not in anon.get("/dbstatus").headers
