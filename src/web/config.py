"""All configuration in one place: environment-driven settings + the hard limits as constants.

Environment (Container App settings in Azure, `.env` / `.apikeys/` locally -- see `.env.example`):
    SQL_BACKEND           sqlite (default; local + tests) | azure
    SQLITE_PATH           .storage/inbody.db
    SQL_SERVER            vhgwcsql1.database.windows.net     (azure)
    SQL_DATABASE          vojtah                             (azure; schema `inbody` inside it)
    STORAGE_BACKEND       local (default) | azure
    LOCAL_STORAGE_DIR     .storage/blobs
    AZURE_STORAGE_ACCOUNT / AZURE_CONTAINER_NAME              (azure)
    SESSION_SECRET        signing key for session + CSRF cookies (REQUIRED outside sqlite/local)
    ANTHROPIC_API_KEY     read by the SDK itself; never logged
    CONTACT_EMAIL         shown on the public landing page ("ask for an account"); empty = no link
    TRUST_PROXY_HEADERS   1 behind the Container Apps ingress (client IP from X-Forwarded-For)
    WORKER_ENABLED        1 (default) runs the background processing thread; tests set 0
    INBODY_NTFY_TOPIC     ntfy topic for pushes to Vojta (secret); empty = off
    PUBLIC_BASE_URL       the site's https base URL (links in pushes)
    APP_VERSION           commit sha, set by the deploy workflow
    APP_ENV               production (set by web.ps1) | dev (default) -- push priority / prefix
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

try:  # optional local convenience; the container gets real env vars
    from dotenv import load_dotenv

    # Repo checkout: src/web/config.py -> repo root is parents[2]. In the container the file sits at
    # /app/config.py, where parents[2] does not exist (crashed the first deploy with IndexError).
    _parents = Path(__file__).resolve().parents
    if len(_parents) > 2:
        load_dotenv(_parents[2] / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or default


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else raw.strip() not in ("0", "false", "no")


# Repo checkout: src/web/config.py -> parents[2] is the repo root. Container: /app/config.py has
# no parents[2]; fall back to the app dir (only used for local SQLite / blob defaults).
_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[2] if len(_HERE.parents) > 2 else _HERE.parent

APP_VERSION = _env("APP_VERSION", "dev")  # baked into the image (Dockerfile ARG GIT_SHA)


def build_label(commit: str, revision: str) -> str:
    """Footer text, as in the monorepo's weber: `0123456 · rev 12`. CONTAINER_APP_REVISION is set
    by Container Apps (`<app>--0000012`); empty elsewhere, then the commit alone."""
    suffix = revision.strip().rsplit("--", 1)[-1].lstrip("0")
    short = commit.strip()[:7] or "dev"
    return f"{short} · rev {suffix}" if suffix else short


BUILD_LABEL = build_label(APP_VERSION, _env("CONTAINER_APP_REVISION"))
# Explicit only, never inferred: `production` is set by .azure/services/web.ps1; else dev.
APP_ENV = _env("APP_ENV", "dev")
SQL_BACKEND = _env("SQL_BACKEND", "sqlite")
SQLITE_PATH = _env("SQLITE_PATH", str(REPO_ROOT / ".storage" / "inbody.db"))
SQL_SERVER = _env("SQL_SERVER", "vhgwcsql1.database.windows.net")
SQL_DATABASE = _env("SQL_DATABASE", "vojtah")
STORAGE_BACKEND = _env("STORAGE_BACKEND", "local")
LOCAL_STORAGE_DIR = _env("LOCAL_STORAGE_DIR", str(REPO_ROOT / ".storage" / "blobs"))
AZURE_STORAGE_ACCOUNT = _env("AZURE_STORAGE_ACCOUNT", "vhgwcsa1")
AZURE_CONTAINER_NAME = _env("AZURE_CONTAINER_NAME", "inbody")
CONTACT_EMAIL = _env("CONTACT_EMAIL")
TRUST_PROXY_HEADERS = _flag("TRUST_PROXY_HEADERS", False)
WORKER_ENABLED = _flag("WORKER_ENABLED", True)
NTFY_TOPIC = _env("INBODY_NTFY_TOPIC")  # a credential (Container App secret); empty = push off
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL")  # e.g. https://<app fqdn>, for links in pushes

# Required for the web app outside local/tests (enforced at app startup, see app.lifespan); a
# CLI such as scripts/users.py signs nothing and runs fine without it.
SESSION_SECRET_EPHEMERAL = not _env("SESSION_SECRET")
SESSION_SECRET = _env("SESSION_SECRET") or secrets.token_urlsafe(48)

# ------------------------------------------------------------------------------------- sessions
SESSION_COOKIE = "__Host-inbody_session"  # __Host-: Secure, Path=/, no Domain -- browser-enforced
# The start page (vojtah.cz) reads /dbstatus before it opens this app, so a click on its InBody
# card waits there instead of on our "Starting the database" page. CORS for exactly these origins.
HOMEPAGE_ORIGINS = ("https://vojtah.cz",)
CSRF_COOKIE = "__Host-inbody_csrf"
LANG_COOKIE = "inbody_lang"
QUOTE_COOKIE = (
    "inbody_quotes"  # the two margin quotes last shown, "a.b": the next page draws others
)
QUOTE_KEEP_COOKIE = (
    "inbody_quotes_keep"  # one-shot, set by POST /settings: the next page keeps the pair
)
UNITS_COOKIE = "inbody_units"  # display units + theme, e.g. "metric.kcal.3.dark" (views.Settings)
SESSION_TTL_SECONDS = 12 * 3600

# ------------------------------------------------------------------------------- guest account
# ONE shared, password-less, read-only account filled with fictional sample data
# (scripts/seed_guest.py). Many visitors use it at once, each with their own session.
GITHUB_URL = "https://github.com/vojtechherrmann/inbody-aggregator"  # footer link (public repo)
HOMEPAGE_URL = "https://vojtah.cz"  # footer link on the name (the start page of the site)
GUEST_USER_ID = "guest"  # reserved: scripts/users.py refuses it, data.create_user needs guest=True
GUEST_SESSION_TTL_SECONDS = 2 * 3600  # a guest session is short; logging out ends only that one
GUEST_LOGIN_LIMITS_PER_IP = ((5, 60), (30, 3600))  # POST /login/guest, own bucket (not /login's)

# ------------------------------------------------------------------------------------ passwords
# OWASP Password Storage Cheat Sheet, Argon2id option 2: m=19 MiB, t=2, p=1.
ARGON2_MEMORY_KIB = 19 * 1024
ARGON2_TIME_COST = 2
ARGON2_PARALLELISM = 1
# Policy for a password the user picks (/account/password); generated ones meet it too.
PASSWORD_MIN_LENGTH = 8  # + at least one uppercase, one lowercase letter and one digit
PASSWORD_MAX_LENGTH = 1024  # same cap as the login form

# ----------------------------------------------------------------------------------- anti-abuse
# In-memory limits are correct only because the app runs as ONE replica (max replicas = 1, one
# process). Lockout and the daily upload count are persisted in SQL and survive restarts.
LOGIN_LIMITS_PER_IP = ((5, 60), (30, 3600))  # (max attempts, window seconds)
LOCKOUT_THRESHOLD = 5  # consecutive failures before the account locks
LOCKOUT_BASE_SECONDS = 60  # 1st lock 1 min, then doubling ...
LOCKOUT_MAX_SECONDS = 3600  # ... capped at 1 h
GLOBAL_LIMITS_PER_IP = ((120, 60), (2000, 3600))  # every request, static included
SHARE_LOOKUP_LIMITS_PER_USER = ((10, 3600),)
PASSWORD_CHANGE_LIMITS_PER_USER = ((5, 300), (20, 3600))  # every POST /account/password
UPLOADS_PER_USER_PER_DAY = 20  # persisted (counted from the upload table, UTC day)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # Claude API per-image limit; larger photos are rejected
MAX_CSV_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 256 * 1024  # multipart overhead

# ------------------------------------------------------------------------------ Claude / budget
MODEL = "claude-opus-5-5"
# Server-side refusal fallback (beta). Pinned to a model with a known price so the worst-case
# budget estimate stays exact; None disables it.
FALLBACK_MODEL: str | None = "claude-opus-4-8"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORT = (
    "low"  # extraction/README.md: chart/screenshot reading holds at low; raise on golden misses
)
MAX_TOKENS = 8000  # output cap per call (thinking included); wire JSON <= ~3.5k tokens
MAX_INPUT_TOKENS = 100_000  # a bigger PDF is rejected, never truncated
API_ATTEMPTS = 2  # 1 call + max 1 retry (API error / invalid answer), each budget-checked
API_TIMEOUT_SECONDS = 300.0

MONTHLY_BUDGET_USD = 50.0  # GLOBAL cap, all users, calendar month (UTC); = the Console limit
USER_MONTHLY_QUOTA_USD = 5.0  # base quota per user per calendar month (UTC), same for everyone
EXTRA_USAGE_DEFAULT_DAYS = 7  # `users.py grant-extra` default validity: from today for 7 days

# $ per 1M tokens: input, output, cache write (5 min), cache read -- from the claude-api skill
# price table (cached 2026-11-03). An unknown model is priced at the most expensive row.
PRICES_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
}
