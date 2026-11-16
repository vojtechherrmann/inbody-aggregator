"""Czech + English UI strings, one flat JSON file per language (`i18n/<lang>.json`).

Language = the `inbody_lang` cookie if set (language switcher), else the first supported entry
of Accept-Language, else Czech. `tests/web/test_i18n.py` keeps both files on the same key set.
"""

from __future__ import annotations

import json
from pathlib import Path

LANGS = ("cs", "en")
DEFAULT = "cs"
_DIR = Path(__file__).resolve().parent / "i18n"
TABLES: dict[str, dict[str, str]] = {
    lang: json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8")) for lang in LANGS
}


def pick_lang(cookie: str | None, accept_language: str | None) -> str:
    if cookie in LANGS:
        return cookie
    for part in (accept_language or "").split(","):
        tag = part.split(";")[0].strip().lower()
        primary = tag.split("-")[0]
        if primary in LANGS:
            return primary
    return DEFAULT


def translator(lang: str):
    table = TABLES.get(lang, TABLES[DEFAULT])

    def t(key: str, **kwargs) -> str:
        text = table.get(key) or TABLES["en"].get(key) or key
        return text.format(**kwargs) if kwargs else text

    return t


def has_key(key: str) -> bool:
    return key in TABLES[DEFAULT]


# Keys the browser-side chart module needs (static/strings.js builds its dictionary from them).
CLIENT_PREFIXES = ("hc.", "metric.", "seg.", "segshort.", "unit.", "src.", "fmt.", "def.")
CLIENT_KEYS = ("nav.upload", "rep.ofIdeal")


def client_table(lang: str) -> dict[str, str]:
    table = TABLES.get(lang, TABLES[DEFAULT])
    return {k: v for k, v in table.items() if k.startswith(CLIENT_PREFIXES) or k in CLIENT_KEYS}
