"""inbody-aggregator web -- FastAPI + HTMX, server-rendered, one container app (`vhgwc-inbody-web`).

A private site for a few friends: each user uploads InBody result sheets (PDF, read by Claude) or
CSV exports (parsed in Python), sees their history as charts + a table, and can share it
read-only with another user. Accounts are created only from the backend (`scripts/users.py`);
there is no self-registration.

Security shape (details in AGENTS.md):
* No JSON API: /docs, /redoc, /openapi.json are off; every page is HTML.
* Two routers. `public` = landing, login, language switch, /healthz. `private` = everything else
  and carries `require_user` as a ROUTER dependency, so a new route cannot forget auth. Both
  carry `csrf_protect`, so no unsafe request is accepted without a valid CSRF token.
* Whose data: always `data.*`, which checks `can_view` -- the viewer comes from the session only.
* The guest account (config.GUEST_USER_ID, shared, password-less, fictional data) is read-only:
  `guest_guard` is a router dependency of `private`, so it refuses EVERY unsafe request of a guest
  session (except the allow-list below) with 403 before any route code runs. data.py / pipeline.py
  refuse the guest again, so a missed route stays safe.
* One middleware sets security headers, the global per-IP limit, the body-size cap, language and
  the CSRF cookie.
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

import config
import data
import db
import dbwarm
import estimates
import fields
import i18n
import pipeline
import quota
import security
import storage
import uploads
import views
import worker
from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup
from ratelimit import limiter
from security import User
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("inbody.web")

HERE = Path(__file__).resolve().parent
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# What a guest session may still POST: end its own session, and the cookie-only settings panel
# (language / units / pause length -- nothing there touches SQL).
GUEST_UNSAFE_ALLOWED = {"/logout", "/settings"}
# GET pages that only make sense for a real person (their own uploads, sharing, password).
GUEST_BLOCKED_PAGES = {"/upload", "/sharing", "/account/password"}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if config.SESSION_SECRET_EPHEMERAL and config.SQL_BACKEND == "azure":
        raise RuntimeError("SESSION_SECRET must be set (Container App secret)")
    dbwarm.ensure_warming()  # a cold container start often coincides with a paused database
    if config.WORKER_ENABLED:
        worker.start()  # also re-queues uploads a restart left pending/processing
    yield


app = FastAPI(
    title="inbody-aggregator",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.globals["current_year"] = lambda: datetime.now().year
templates.env.globals["github_url"] = config.GITHUB_URL
templates.env.globals["homepage_url"] = config.HOMEPAGE_URL
templates.env.globals["password_min_length"] = config.PASSWORD_MIN_LENGTH  # pw_form
templates.env.globals["build_label"] = lambda: config.BUILD_LABEL  # footer: commit · revision
# every CSS/JS URL carries the build, so a deploy is never mixed with stale cached assets
templates.env.globals["static_url"] = lambda path: f"/static/{path}?v={config.APP_VERSION[:12]}"
templates.env.filters["fromjson"] = json.loads


# ----------------------------------------------------------------------- "Contact Vojta" (one link)
# Every "write to / contact Vojta" in the UI is this ONE mailto link with the SAME label. The
# address comes ONLY from config.CONTACT_EMAIL (never hardcoded, never in JS or a cookie); each
# context has its own short pre-filled subject (i18n `contact.subject.<context>`), never a
# measured value.
CONTACT_CONTEXTS = ("general", "forgot", "account", "delete", "extra", "upload", "error")
NO_USER_IN_SUBJECT = ("forgot", "account")  # asked before / without an account
PUBLIC_TEMPLATES = ("landing.html", "login.html", "data.html")


@pass_context
def contact_link(ctx, context: str = "general", label: str | None = None, **fmt) -> Markup:
    """The link `<a class="contact-link" href="mailto:…?subject=…">Contact Vojta</a>`. Empty
    address: the label as plain text. A guest session (made-up data) gets no link except on the
    public pages."""
    if ctx.get("is_guest") and not ctx.get("public_page"):
        return Markup("")
    t = ctx["t"]
    label = label or t("contact.link")
    if not config.CONTACT_EMAIL:
        return Markup.escape(label)
    subject = t("contact.subject." + context, **{"id": "", "code": "", **fmt}).strip()
    user = ctx.get("user")
    if user is not None and not user.is_guest and context not in NO_USER_IN_SUBJECT:
        subject += f" ({user.id})"
    href = f"mailto:{config.CONTACT_EMAIL}?subject={quote(subject, safe='')}"
    return Markup('<a class="contact-link" href="{}">{}</a>').format(href, label)


@pass_context
def contact_text(ctx, key: str, context: str = "general", **fmt) -> Markup:
    """A sentence (i18n key with a `{contact}` placeholder) with the contact link embedded; empty
    when there is no link to offer (guest)."""
    link = contact_link(ctx, context, **fmt)
    if not link:
        return Markup("")
    mark = "\x00"
    before, _, after = ctx["t"](key, contact=mark).partition(mark)
    return Markup.escape(before) + link + Markup.escape(after)


templates.env.globals["contact_link"] = contact_link
templates.env.globals["contact_text"] = contact_text
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


class AuthRequired(Exception):
    pass


# ------------------------------------------------------------------------------------ middleware


def client_ip(request: Request) -> str:
    """Behind the Container Apps ingress the client is the LAST X-Forwarded-For hop (the one the
    ingress appended); earlier entries are client-controlled."""
    if config.TRUST_PROXY_HEADERS:
        xff = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


# Pages that never touch the database for a visitor without a session: shown at once even while the
# database is paused (the warm-up still starts in the background, so it is awake by the next click).
DB_FREE_GET = {"/", "/login", "/data"}
DB_GATE_EXEMPT = {"/healthz", "/dbstatus"}


def _db_wait_page(request: Request) -> Response:
    """ "Starting the database": no database access, polls /dbstatus and returns to the page."""
    settings = settings_of(request)
    lang = request.state.lang
    target = "/"
    if request.method == "GET":
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return templates.TemplateResponse(
        request,
        "dbwait.html",
        {
            "t": i18n.translator(lang),
            "lang": lang,
            "theme": settings.data_theme,
            "target": target,
        },
        status_code=503,
        headers={"Retry-After": "5"},
    )


@app.middleware("http")
async def db_gate(request: Request, call_next):
    """While the database is cold (not queried for a while), answer with the waiting page instead
    of letting the browser hang; see dbwarm.py. Inside `frame`: security headers still apply."""
    path = request.url.path
    if not dbwarm.enabled() or path.startswith("/static/") or path in DB_GATE_EXEMPT:
        return await call_next(request)
    if dbwarm.is_warm():
        response = await call_next(request)
        if response.status_code < 500 and not (
            request.method == "GET"
            and path in DB_FREE_GET
            and not request.cookies.get(config.SESSION_COOKIE)
        ):
            dbwarm.mark_warm()  # the request needed the database and it answered
        return response
    dbwarm.ensure_warming()
    if (
        request.method == "GET"
        and path in DB_FREE_GET
        and not request.cookies.get(config.SESSION_COOKIE)
    ):
        return await call_next(request)  # anonymous page: no database needed
    if await dbwarm.wait_until_warm(dbwarm.FAST_WAIT):
        return await call_next(request)
    return _db_wait_page(request)


CANONICAL_EXEMPT = {"/healthz", "/dbstatus"}  # probes may arrive under any host name


@app.middleware("http")
async def canonical_host(request: Request, call_next):
    """One origin only: a GET / HEAD that arrives under another host name (the old
    *.azurecontainerapps.io address after a move to a custom domain, a bookmark) is redirected to
    PUBLIC_BASE_URL, path and query kept. Unset base URL (local, tests) = off. Sits inside `frame`
    so the redirect carries the security headers."""
    base = config.PUBLIC_BASE_URL.strip().rstrip("/")
    if base and request.method in ("GET", "HEAD") and request.url.path not in CANONICAL_EXEMPT:
        want = urlsplit(base).hostname
        have = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if want and have and have != want.lower():
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"{base}{request.url.path}{query}", status_code=308)
    return await call_next(request)


@app.middleware("http")
async def frame(request: Request, call_next):
    ip = client_ip(request)
    request.state.ip = ip
    if not limiter.hit(f"global:{ip}", config.GLOBAL_LIMITS_PER_IP):
        response: Response = PlainTextResponse("Too many requests", status_code=429)
    elif request.method in UNSAFE and _body_too_big(request):
        response = PlainTextResponse("Request too large", status_code=413)
    else:
        request.state.lang = i18n.pick_lang(
            request.cookies.get(config.LANG_COOKIE), request.headers.get("accept-language")
        )
        existing = request.cookies.get(config.CSRF_COOKIE)
        valid = security.csrf_valid(existing, existing)
        request.state.csrf = existing if valid else security.new_csrf_token()
        response = await call_next(request)
        if not valid and config.CSRF_COOKIE not in _set_cookie_names(response):
            _set_csrf_cookie(response, request.state.csrf)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith("/static/"):
        # always revalidate (ETag, a cheap 304): without a Cache-Control header browsers cache
        # CSS/JS heuristically for hours, so after a deploy a visitor got new HTML with OLD CSS
        response.headers.setdefault("Cache-Control", "no-cache")
    else:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _body_too_big(request: Request) -> bool:
    length = request.headers.get("content-length")
    if length is None:
        return request.headers.get("transfer-encoding") is not None  # no chunked bodies
    try:
        return int(length) > config.MAX_REQUEST_BYTES
    except ValueError:
        return True


def _set_cookie_names(response: Response) -> set[str]:
    return {
        v.decode("latin-1").split("=", 1)[0] for k, v in response.raw_headers if k == b"set-cookie"
    }


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        config.CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", path="/"
    )


# ---------------------------------------------------------------------------------- dependencies


def current_user(request: Request) -> User | None:
    if not hasattr(request.state, "user"):
        cookie = request.cookies.get(config.SESSION_COOKIE)
        user = None
        if cookie:
            with db.engine().connect() as conn:
                user = security.session_user(conn, cookie)
        request.state.user = user
    return request.state.user


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise AuthRequired()
    return user


async def csrf_protect(request: Request) -> None:
    if request.method not in UNSAFE:
        return
    submitted = request.headers.get("x-csrf-token")
    if submitted is None:
        form = await request.form()
        submitted = form.get("csrf_token")
    if not security.csrf_valid(request.cookies.get(config.CSRF_COOKIE), submitted):
        raise StarletteHTTPException(status_code=403, detail="csrf")


CurrentUser = Annotated[User, Depends(require_user)]


def guest_guard(request: Request, user: CurrentUser) -> None:
    """The guest account is read-only: 403 for every unsafe method not on the allow-list, and for
    the pages that exist only to write (upload form, sharing, password). Central by design."""
    if not user.is_guest:
        return
    path = request.url.path
    if (request.method in UNSAFE and path not in GUEST_UNSAFE_ALLOWED) or (
        request.method not in UNSAFE and path in GUEST_BLOCKED_PAGES
    ):
        raise StarletteHTTPException(status_code=403, detail="guest")


public = APIRouter(dependencies=[Depends(csrf_protect)])
private = APIRouter(
    dependencies=[Depends(require_user), Depends(guest_guard), Depends(csrf_protect)]
)


# --------------------------------------------------------------------------------------- helpers


QUOTES = 16  # quote.1 .. quote.16 in i18n: decoration in the side margins, two random ones per page


def next_quotes(previous: str | None) -> list[int]:
    """[left, right]: two different random quotes, excluding the pair shown before ("a.b")."""
    shown: set[int] = set()
    for part in (previous or "").split("."):
        if part.isdigit() and 1 <= int(part) <= QUOTES:
            shown.add(int(part))
    pool = [n for n in range(1, QUOTES + 1) if n not in shown]
    return random.sample(pool if len(pool) >= 2 else range(1, QUOTES + 1), 2)


def shown_quotes(raw: str | None) -> list[int] | None:
    """The pair from the cookie ("a.b"), or None when absent / not two different valid numbers."""
    parts = (raw or "").split(".")
    if len(parts) == 2 and all(p.isdigit() and 1 <= int(p) <= QUOTES for p in parts):
        pair = [int(p) for p in parts]
        return pair if pair[0] != pair[1] else None
    return None


# Only landing on one of these draws a NEW pair; every other page keeps the pair from the cookie.
QUOTE_DRAW_TEMPLATES = ("report.html", "history.html")


def settings_of(request: Request) -> views.Settings:
    return views.parse_settings(request.cookies.get(config.UNITS_COOKIE))


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    lang = getattr(request.state, "lang", i18n.DEFAULT)
    user = getattr(request.state, "user", None)
    shell: dict = {
        "settings": settings_of(request),
        "view_path": "/history",
        "public_page": name in PUBLIC_TEMPLATES,
    }
    if user is not None:
        # the Viewing selector: only people data.can_view allows (me, shares to me; admin: all)
        with db.engine().connect() as conn:
            shell["subjects"] = data.viewable_subjects(conn, user)
        shell["subject"] = user.id
    # decoration: two different quotes for the side margins. A NEW pair (never either quote of the
    # pair shown before, kept in a cookie) is drawn only when the user lands on Report or History,
    # except right after a Settings change (one-shot flag: the page is the same, so is the pair).
    # Every other page keeps the cookie's pair; with no cookie yet it draws once.
    quotes_page = not name.startswith("_") and not request.headers.get("hx-request")
    previous = shown_quotes(request.cookies.get(config.QUOTE_COOKIE))
    keep_flag = bool(request.cookies.get(config.QUOTE_KEEP_COOKIE))
    draw = previous is None or (name in QUOTE_DRAW_TEMPLATES and not keep_flag)
    shell["quotes"] = (
        next_quotes(".".join(map(str, previous)) if previous else None)
        if quotes_page and draw
        else (previous or [1, 2])
    )
    shell.update(context)
    if user is not None:
        shell["viewing_own"] = shell["subject"] == user.id
    # The guest banner shows on EVERY page of a guest session, public pages (/data) included.
    probe = user if user is not None else current_user(request)
    is_guest = probe is not None and probe.is_guest
    response = templates.TemplateResponse(
        request,
        name,
        {
            "t": i18n.translator(lang),
            "lang": lang,
            "theme": shell["settings"].data_theme,
            "langs": i18n.LANGS,
            "csrf": getattr(request.state, "csrf", ""),
            "user": user,
            "is_guest": is_guest,
            "version": config.APP_VERSION,
            **shell,
        },
        status_code=status_code,
    )
    if quotes_page and draw:
        response.set_cookie(
            config.QUOTE_COOKIE,
            ".".join(str(n) for n in shell["quotes"]),
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
    if quotes_page and keep_flag:
        response.delete_cookie(config.QUOTE_KEEP_COOKIE, path="/")  # one-shot
    return response


def script_json(value) -> str:
    """JSON for a <script type="application/json"> block; `<` is escaped (no `</script>`)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def notice_key(raw: str | None) -> str | None:
    """Only known `notice.*` keys reach a template -- the query string cannot inject text."""
    return raw if raw and raw.startswith("notice.") and i18n.has_key(raw) else None


def safe_next(raw: str | None) -> str:
    if raw and raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        return raw
    return "/"


# --------------------------------------------------------------------------------- error pages


@app.exception_handler(AuthRequired)
async def _auth_required(request: Request, _exc: AuthRequired):
    if request.headers.get("hx-request"):
        return PlainTextResponse("", status_code=401, headers={"HX-Redirect": "/login"})
    if request.method == "GET":
        return redirect("/login")
    return PlainTextResponse("Unauthorized", status_code=401)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if not hasattr(request.state, "lang"):
        request.state.lang = i18n.DEFAULT
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _bad_request(request: Request, _exc: RequestValidationError):
    return render(request, "error.html", status_code=400, code=400)


@app.exception_handler(data.GuestReadOnly)
async def _guest_read_only(request: Request, _exc: data.GuestReadOnly):
    return render(request, "error.html", status_code=403, code=403)


@app.exception_handler(data.NotFound)
async def _not_found(request: Request, _exc: data.NotFound):
    return render(request, "error.html", status_code=404, code=404)


@app.exception_handler(DBAPIError)
async def _database_unreachable(request: Request, exc: DBAPIError):
    """The database dropped away DURING a request although `db_gate` let it through as warm (it went
    away while the process was not looking: a reset connection, a login timeout). That is the
    cold-database case, not a bug: mark it cold, start the warm-up and show the same "Starting the
    database" page, which returns to the page by itself. Any other database error stays a 500."""
    unreachable = isinstance(exc, (OperationalError, InterfaceError)) or exc.connection_invalidated
    if not unreachable or not dbwarm.enabled():
        raise exc
    log.warning(
        "database unreachable during %s %s: %s",
        request.method,
        request.url.path,
        type(exc.orig).__name__ if exc.orig is not None else type(exc).__name__,
    )
    dbwarm.mark_cold()
    if not hasattr(request.state, "lang"):
        request.state.lang = i18n.DEFAULT
    return _db_wait_page(request)


# ---------------------------------------------------------------------------------- public pages


@public.get("/healthz")
def healthz():
    """Liveness only -- deliberately no DB call, so a probe never wakes the paused database."""
    return PlainTextResponse(f"ok {config.APP_VERSION}")


@public.get("/dbstatus")
def dbstatus(request: Request):
    """Is the database awake? Polled by the waiting page and by the InBody card on vojtah.cz. No
    database access (it must not block on the paused database); asking also (re)starts the warm-up.
    `session` = a session cookie came with the request (presence only, not checked: no database):
    without one the front page needs no database, so the start page need not wait for it.
    CORS (with credentials) only for config.HOMEPAGE_ORIGINS; the session cookie is SameSite=Strict
    and vojtah.cz is the same site, so it comes along on that fetch."""
    dbwarm.ensure_warming()
    body = {"warm": dbwarm.is_warm(), "session": bool(request.cookies.get(config.SESSION_COOKIE))}
    response = Response(json.dumps(body), media_type="application/json")
    origin = request.headers.get("origin")
    if origin in config.HOMEPAGE_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Vary"] = "Origin"
    return response


@public.get("/", response_class=HTMLResponse)
def landing(request: Request):
    if current_user(request):
        return redirect("/history")
    return render(request, "landing.html")


@public.get("/data", response_class=HTMLResponse)
def data_handling(request: Request):
    """How data is handled -- public, so a visitor can read it before asking for an account."""
    return render(request, "data.html")


@public.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request):
        return redirect("/history")
    return render(request, "login.html")


@public.post("/login")
def login_submit(
    request: Request,
    username: Annotated[str, Form(max_length=64)] = "",
    password: Annotated[str, Form(max_length=1024)] = "",
    website: Annotated[str, Form(max_length=256)] = "",  # honeypot: hidden, humans leave it empty
):
    if not limiter.hit(f"login:{request.state.ip}", config.LOGIN_LIMITS_PER_IP):
        return render(request, "login.html", status_code=429, error="login.rate_limited")
    uid = username.strip().lower()
    now = db.utcnow()
    with db.engine().begin() as conn:
        row = None
        # the guest has no password: its id never logs in here (only POST /login/guest does), and
        # failed attempts must not touch (lock) the shared account
        if not website and data.USER_ID_RE.match(uid) and not data.is_guest_id(uid):
            row = data.get_login_row(conn, uid)
        # Always one Argon2 verify (dummy hash when there is no user) -- uniform timing.
        ok = security.verify_password(row.password_hash if row else None, password)
        usable = row is not None and not row.is_disabled
        locked = usable and security.is_locked(row.locked_until, now)
        if not (usable and not locked and ok):
            if usable and not locked:
                failures = (row.failed_login_count or 0) + 1
                lock = security.lockout_seconds(failures)
                conn.execute(
                    update(db.app_user)
                    .where(db.app_user.c.id == uid)
                    .values(
                        failed_login_count=failures,
                        last_failed_login_at=now,
                        locked_until=now + _seconds(lock) if lock else None,
                    )
                )
            log.info("login failed ip=%s", request.state.ip)
            return render(request, "login.html", status_code=401, error="login.failed")
        values = {"failed_login_count": 0, "locked_until": None, "last_login_at": now}
        if security.hasher.check_needs_rehash(row.password_hash):
            values["password_hash"] = security.hash_password(password)
        conn.execute(update(db.app_user).where(db.app_user.c.id == uid).values(**values))
        cookie = security.create_session(conn, uid)
    # Once per login, never blocking: a password the admin assigned should be changed.
    landing = "/history?notice=notice.password_admin_set" if row.password_admin_set else "/history"
    response = redirect(landing)
    _set_session_cookie(response, cookie, uid)
    _set_csrf_cookie(response, security.new_csrf_token())  # rotate on privilege change
    return response


@public.post("/login/guest")
def login_guest(request: Request):
    """ "Log in as Guest": a normal session for the shared sample account, no password. Rate
    limited per IP like /login (own bucket); never pushes anything; each visitor gets their OWN
    short session, so one guest logging out does not end the others."""
    if not limiter.hit(f"guest:{request.state.ip}", config.GUEST_LOGIN_LIMITS_PER_IP):
        return render(request, "login.html", status_code=429, error="login.rate_limited")
    with db.engine().begin() as conn:
        row = data.get_login_row(conn, config.GUEST_USER_ID)
        if row is None or row.is_disabled:  # not seeded yet / switched off by `users.py disable`
            return render(request, "login.html", status_code=503, error="login.guest_unavailable")
        security.prune_guest_sessions(conn)
        cookie = security.create_session(conn, config.GUEST_USER_ID)
    log.info("guest login ip=%s", request.state.ip)
    response = redirect("/history")
    _set_session_cookie(response, cookie, config.GUEST_USER_ID)
    _set_csrf_cookie(response, security.new_csrf_token())  # rotate on privilege change
    return response


SSO_SITES = {"same-site", "same-origin"}


@public.get("/sso")
def sso(request: Request, t: Annotated[str, Query(max_length=security.SSO_TICKET_MAX_LEN)] = ""):
    """Single sign-on from vojtah.cz (accounts are shared, see db.py): redeem the one-time ticket
    the site's API minted for the signed-in visitor and start a normal session here. Anything
    wrong (no / used / expired ticket, disabled account) just lands on the login page.

    Only a navigation that starts on our own site (Sec-Fetch-Site same-site, i.e. vojtah.cz) may
    redeem: a ticket planted in a link on another site cannot sign a victim into the planter's
    account (login CSRF). A browser that sends no Sec-Fetch-Site falls back to the login page."""
    if not limiter.hit(f"login:{request.state.ip}", config.LOGIN_LIMITS_PER_IP):
        return redirect("/login")
    if request.headers.get("sec-fetch-site") not in SSO_SITES:
        log.info("sso refused (not same-site) ip=%s", request.state.ip)
        return redirect("/login")
    with db.engine().begin() as conn:
        uid = security.redeem_sso_ticket(conn, t)
        if uid is None:
            log.info("sso ticket rejected ip=%s", request.state.ip)
            return redirect("/login")
        security.revoke_session(conn, request.cookies.get(config.SESSION_COOKIE))
        conn.execute(
            update(db.app_user).where(db.app_user.c.id == uid).values(last_login_at=db.utcnow())
        )
        cookie = security.create_session(conn, uid)
    log.info("sso login ip=%s", request.state.ip)
    response = redirect("/history")
    _set_session_cookie(response, cookie, uid)
    _set_csrf_cookie(response, security.new_csrf_token())  # rotate on privilege change
    return response


def _seconds(n: int):
    from datetime import timedelta

    return timedelta(seconds=n)


@public.post("/lang")
def set_language(
    request: Request,
    lang: Annotated[str, Form(max_length=8)] = i18n.DEFAULT,
    next: Annotated[str, Form(max_length=512)] = "/",
):
    response = redirect(safe_next(next))
    _set_lang_cookie(response, lang)
    return response


def _set_lang_cookie(response: Response, lang: str) -> None:
    if lang in i18n.LANGS:
        response.set_cookie(
            config.LANG_COOKIE,
            lang,
            max_age=365 * 24 * 3600,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )


# --------------------------------------------------------------------------------- private pages


@private.post("/logout")
def logout(request: Request):
    """Ends THIS session only (never revoke_all_sessions: the guest account is shared)."""
    with db.engine().begin() as conn:
        security.revoke_session(conn, request.cookies.get(config.SESSION_COOKIE))
    response = redirect("/")
    response.delete_cookie(config.SESSION_COOKIE, path="/", secure=True, httponly=True)
    return response


def _subject(conn, user: User, subject: str | None) -> str:
    """The history being viewed. Never trusted: `can_view` decides, a refusal is a 404."""
    owner = (subject or user.id).strip().lower()
    data.require_view(conn, user, owner)
    return owner


def _rows(conn, user: User, owner: str) -> list[dict]:
    """The owner's measurements as Report / History rows (SI units, gym resolved). can_view."""
    measurements = data.list_measurements(conn, user, owner)
    uploads_by_id = {u.id: u for u in data.list_uploads(conn, user, owner)}
    return views.rows_for(measurements, uploads_by_id)


@private.get("/history", response_class=HTMLResponse)
def history(
    request: Request, user: CurrentUser, subject: str | None = None, notice: str | None = None
):
    """History tab: the chart module renders from the JSON embedded in the page (no JSON API)."""
    with db.engine().connect() as conn:
        owner = _subject(conn, user, subject)
        rows = _rows(conn, user, owner)
        has_friends = len(data.viewable_subjects(conn, user)) > 1
    settings = settings_of(request)
    lang = request.state.lang
    payload = {
        "lang": lang,
        "units": settings.units(),
        "measurements": rows,
        "reportUrl": f"/report?subject={owner}",
        "uploadUrl": "/upload" if owner == user.id and not user.is_guest else None,
        # the empty state also points at the friends' results when somebody shared theirs
        "hasFriends": has_friends and owner == user.id,
    }
    return render(
        request,
        "history.html",
        subject=owner,
        view_path="/history",
        history_json=script_json(payload),
        i18n_json=script_json(i18n.client_table(lang)),
        notice=notice_key(notice),
    )


@private.get("/report", response_class=HTMLResponse)
def report(
    request: Request,
    user: CurrentUser,
    subject: str | None = None,
    m: str | None = None,
    confirm: str | None = None,
    notice: str | None = None,
):
    """Report tab: one measurement (latest by default), server-rendered (docs/ui-design.md 4)."""
    with db.engine().connect() as conn:
        owner = _subject(conn, user, subject)
        rows = _rows(conn, user, owner)
        can_edit = data.can_edit(conn, user, owner)
    settings = settings_of(request)
    shown = [views.to_display(r, settings) for r in rows]
    idx = len(shown) - 1
    if m is not None:
        idx = next((i for i, r in enumerate(shown) if r["id"] == m), -2)
        if idx == -2:
            raise data.NotFound(m)
    current = shown[idx] if shown else {}
    return render(
        request,
        "report.html",
        subject=owner,
        view_path="/report",
        rows=shown,
        idx=idx,
        f=views.Fmt(request.state.lang, settings),
        segs=views.SEGS,
        present=[k for k, v in current.items() if v is not None],
        can_edit=can_edit,
        confirm=confirm == "delete",
        notice=notice_key(notice),
    )


@private.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, user: CurrentUser, notice: str | None = None):
    """The upload page: form, three-phase progress bar, my uploads. Uploads go to MY account."""
    with db.engine().connect() as conn:
        upload_rows = data.list_uploads(conn, user, user.id, limit=100)
        last_source = data.last_source_name(conn, user.id)
    return render(
        request,
        "upload_new.html",
        uploads=upload_rows,
        polling=any(u.status in data.ACTIVE_STATUSES for u in upload_rows),
        last_source=last_source or "",
        max_mb=config.MAX_UPLOAD_BYTES // (1024 * 1024),
        per_day=config.UPLOADS_PER_USER_PER_DAY,
        notice=notice_key(notice),
    )


@private.post("/settings")
def save_settings(
    request: Request,
    lang: Annotated[str, Form(max_length=8)] = "",
    mass: Annotated[str, Form(max_length=16)] = "",
    energy: Annotated[str, Form(max_length=8)] = "",
    theme: Annotated[str, Form(max_length=8)] = "",
    next: Annotated[str, Form(max_length=512)] = "/",
):
    """Settings panel: language (the existing cookie) + units and colour theme (one cookie).
    Cookie only, no SQL: allowed for the guest too."""
    response = redirect(safe_next(next))
    _set_lang_cookie(response, lang)
    response.set_cookie(
        config.UNITS_COOKIE,
        views.settings_from_form(mass, energy, theme).cookie(),
        max_age=365 * 24 * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    # a settings round trip lands on the same page: the margin quotes stay as they are
    response.set_cookie(
        config.QUOTE_KEEP_COOKIE,
        "1",
        max_age=60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@private.get("/history/uploads", response_class=HTMLResponse)
def history_uploads(request: Request, user: CurrentUser, subject: str | None = None):
    """HTMX fragment: the upload list, polled while something is still processing."""
    with db.engine().connect() as conn:
        owner = _subject(conn, user, subject)
        upload_rows = data.list_uploads(conn, user, owner)
    return render(
        request,
        "_uploads.html",
        subject=owner,
        uploads=upload_rows,
        polling=any(u.status in data.ACTIVE_STATUSES for u in upload_rows),
    )


@private.get("/files/{upload_id}")
def download(upload_id: str, user: CurrentUser):
    """Raw file for the ADMIN's review (audit) only: no page offers a download to users any more,
    so for everybody else it is a plain 404. Streamed through the app after the authorization
    check; no blob URL ever leaves."""
    if not user.is_admin:
        raise data.NotFound(upload_id)
    try:
        uuid.UUID(upload_id)
    except ValueError as exc:
        raise data.NotFound(upload_id) from exc
    with db.engine().connect() as conn:
        row = data.get_upload(conn, user, upload_id)
    if row.deleted and not user.is_admin:
        raise data.NotFound(upload_id)  # deleted = gone from the user's data (admin: audit)
    try:
        body = storage.store().get(row.blob_path)
    except Exception as exc:  # missing blob
        raise data.NotFound(upload_id) from exc
    filename = storage.sanitize_name(row.original_name)
    return Response(
        body,
        media_type=uploads.CONTENT_TYPES.get(row.kind, "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


REJECT_STATUS = {
    "upload.err.daily": 429,
    "upload.err.size": 413,
    "upload.err.image_size": 413,
    "upload.err.csv_size": 413,
    "upload.err.type": 415,
    "upload.err.empty": 415,
}


@private.post("/upload")
def upload_file(
    request: Request,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    source_name: Annotated[str, Form(max_length=128)] = "",
):
    """Section 1 of docs/upload-pipeline.md. The page's script posts via XHR (real upload
    progress) and gets 201 + Location; without JS the form posts normally and gets a 303."""
    xhr = request.headers.get("x-upload") == "xhr"
    content = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    try:
        upload_id = pipeline.admit(
            user.id, file.filename or "upload", content, source_name=source_name.strip() or None
        )
    except uploads.Rejected as exc:
        status = REJECT_STATUS.get(exc.key, 400)
        template = "_reject.html" if xhr else "upload_rejected.html"
        return render(request, template, status_code=status, key=exc.key)
    if config.WORKER_ENABLED:
        worker.enqueue(upload_id)
    if xhr:
        return Response(status_code=201, headers={"Location": f"/uploads/{upload_id}"})
    return redirect(f"/uploads/{upload_id}")


def _upload_context(conn, user: User, upload_id: str) -> dict:
    """Everything the upload page / its live fragment shows. NotFound unless can_view."""
    try:
        uuid.UUID(upload_id)
    except ValueError as exc:
        raise data.NotFound(upload_id) from exc
    row = data.get_upload(conn, user, upload_id)  # the authorization check
    report = json.loads(row.report_json) if row.report_json else None
    cache_hit = bool(row.cache_hit)
    if row.cache_hit is None and row.kind in uploads.SHEET_KINDS:
        cache_hit = pipeline._cached_answer(conn, row.sha256) is not None
    elapsed = 0.0
    if row.phase_started_at:
        elapsed = max(0.0, (db.utcnow() - row.phase_started_at).total_seconds())
    final = row.status in pipeline.FINAL
    duplicate_of = None
    if row.duplicate_of:
        try:
            duplicate_of = data.get_upload(conn, user, row.duplicate_of)
        except data.NotFound:
            duplicate_of = None
    return {
        "u": row,
        "report": report,
        "final": final,
        "is_owner": row.user_id == user.id,
        "retryable": not user.is_guest
        and row.user_id == user.id
        and row.status in pipeline.RETRYABLE,
        "deletable": data.can_edit(conn, user, row.user_id)
        and row.status in pipeline.PROCESSED
        and not row.deleted,
        # the guest sees the controls, disabled (the server refuses them anyway)
        "guest_locked": user.is_guest and row.status in pipeline.PROCESSED and not row.deleted,
        "duplicate_of": duplicate_of,
        "progress": {
            "status": row.status,
            "phase": row.phase or "parse",
            "elapsed": round(elapsed, 2),
            "parse_estimate": round(
                estimates.parse_seconds(conn, row.kind, row.pages, cache_hit), 1
            ),
            "save_estimate": round(estimates.save_seconds(conn), 1),
            "final": final,
        },
        "field_label": _field_label,
    }


def _field_label(t, name: str) -> str:
    key = f"field.{name}"
    text = t(key)
    return name if text == key else text


@private.get("/uploads/{upload_id}", response_class=HTMLResponse)
def upload_page(request: Request, upload_id: str, user: CurrentUser, confirm: str | None = None):
    with db.engine().connect() as conn:
        ctx = _upload_context(conn, user, upload_id)
    return render(request, "upload.html", confirm=confirm == "delete", **ctx)


@private.get("/uploads/{upload_id}/live", response_class=HTMLResponse)
def upload_live(request: Request, upload_id: str, user: CurrentUser):
    """HTMX fragment polled every 1 s until the status is final: progress state + report."""
    with db.engine().connect() as conn:
        ctx = _upload_context(conn, user, upload_id)
    return render(request, "_upload_live.html", **ctx)


@private.post("/uploads/{upload_id}/retry")
def upload_retry(upload_id: str, user: CurrentUser):
    """The owner's "try again" for failed / skipped_budget. Anyone else: 404."""
    with db.engine().begin() as conn:
        row = data.get_upload(conn, user, upload_id)  # NotFound unless can_view
        if not pipeline.mark_for_retry(conn, user.id, row.id):  # owner + retryable only
            raise data.NotFound(upload_id)
    if config.WORKER_ENABLED:
        worker.enqueue(upload_id)
    return redirect(f"/uploads/{upload_id}")


@private.post("/measurements/{measurement_id}/delete")
def measurement_delete(measurement_id: str, user: CurrentUser):
    """Owner / admin deletes one day's measurement (rows flagged, never removed). Else 404."""
    with db.engine().begin() as conn:
        owner = pipeline.delete_measurement(conn, user, measurement_id)
    return redirect(f"/report?subject={owner}&notice=notice.measurement_deleted")


@private.post("/uploads/{upload_id}/delete")
def upload_delete(upload_id: str, user: CurrentUser):
    """Owner / admin deletes an upload and its measurements (rows flagged, file archived)."""
    with db.engine().begin() as conn:
        pipeline.delete_upload(conn, user, upload_id)
    return redirect(f"/uploads/{upload_id}")


# --------------------------------------------------------------------------------- admin review


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise data.NotFound("admin")


def _log_review(conn, admin: User, upload_id: str, action: str, note: str | None = None) -> None:
    conn.execute(
        db.review_action.insert().values(
            id=str(uuid.uuid4()),
            upload_id=upload_id,
            admin_id=admin.id,
            action=action,
            note=(note or "")[:500] or None,
            created_at=db.utcnow(),
        )
    )


def _review_row(conn, upload_id: str):
    row = conn.execute(select(db.upload).where(db.upload.c.id == upload_id)).first()
    if row is None:
        raise data.NotFound(upload_id)
    return row


@private.get("/admin/review", response_class=HTMLResponse)
def review_queue(request: Request, user: CurrentUser):
    _require_admin(user)
    with db.engine().connect() as conn:
        rows = conn.execute(
            select(db.upload)
            .where(db.upload.c.status == "needs_review")
            .order_by(db.upload.c.created_at)
        ).all()
    return render(request, "review_queue.html", rows=rows, fromjson=json.loads)


@private.get("/admin/review/{upload_id}", response_class=HTMLResponse)
def review_detail(request: Request, upload_id: str, user: CurrentUser):
    _require_admin(user)
    with db.engine().connect() as conn:
        row = _review_row(conn, upload_id)
        exts = conn.execute(
            select(db.extraction)
            .where(db.extraction.c.upload_id == upload_id)
            .order_by(db.extraction.c.measured_date)
        ).all()
        actions = conn.execute(
            select(db.review_action)
            .where(db.review_action.c.upload_id == upload_id)
            .order_by(db.review_action.c.created_at)
        ).all()
    review = json.loads(row.review_json) if row.review_json else {}
    parsed = [
        {
            k: getattr(e, k)
            for k in ("measured_date", "measured_time", "origin", *fields.CORE, "checks_json")
        }
        for e in exts
    ]
    return render(
        request,
        "review_detail.html",
        u=row,
        review=review,
        parsed=parsed,
        actions=actions,
        llm_json=json.dumps(json.loads(row.llm_json), indent=1, ensure_ascii=False)
        if row.llm_json
        else None,
    )


@private.post("/admin/review/{upload_id}/{action}")
def review_action(
    upload_id: str, action: str, user: CurrentUser, note: Annotated[str, Form(max_length=500)] = ""
):
    _require_admin(user)
    if action not in ("approve", "reject", "reprocess"):
        raise data.NotFound(action)
    with db.engine().begin() as conn:
        row = _review_row(conn, upload_id)
        if action == "approve":
            if row.status != "needs_review":
                raise data.NotFound(upload_id)
            # accept the held extractions as they are -> day check (refresh) -> report
            conn.execute(
                update(db.upload)
                .where(db.upload.c.id == upload_id)
                .values(status="done", error=None, error_code=None)
            )
            pipeline.refresh_user(conn, row.user_id)
        elif action == "reject":
            conn.execute(
                update(db.upload)
                .where(db.upload.c.id == upload_id)
                .values(
                    status="failed",
                    error_code="review_rejected",
                    error=note.strip()[:500] or "rejected after review",
                )
            )
            pipeline.refresh_user(conn, row.user_id)
        else:
            conn.execute(
                update(db.upload).where(db.upload.c.id == upload_id).values(status="pending")
            )
        _log_review(conn, user, upload_id, action, note)
    if action == "reprocess" and config.WORKER_ENABLED:
        worker.enqueue(upload_id)
    return redirect(f"/admin/review/{upload_id}")


@private.get("/sharing", response_class=HTMLResponse)
def sharing(
    request: Request, user: CurrentUser, notice: str | None = None, revoke: str | None = None
):
    with db.engine().connect() as conn:
        grants = data.my_grants(conn, user)  # only the session user's own grants
        incoming = data.shared_with_me(conn, user)
    return render(
        request,
        "sharing.html",
        grants=grants,
        incoming=incoming,
        revoke=revoke,  # shows the in-page confirmation for that (own) grant only
        notice=notice_key(notice),
    )


@private.post("/sharing/grant")
def sharing_grant(user: CurrentUser, username: Annotated[str, Form(max_length=64)] = ""):
    if not limiter.hit(f"share:{user.id}", config.SHARE_LOOKUP_LIMITS_PER_USER):
        return redirect("/sharing?notice=notice.share_rate")
    target = username.strip().lower()
    if target == user.id:
        return redirect("/sharing?notice=notice.share_self")
    with db.engine().begin() as conn:
        # the guest account is never a share target: looks exactly like an unknown user
        if data.is_guest_id(target) or not (
            data.USER_ID_RE.match(target) and data.user_exists(conn, target)
        ):
            return redirect("/sharing?notice=notice.share_not_found")
        data.grant_share(conn, user, target)
    return redirect("/sharing?notice=notice.share_ok")


@private.post("/sharing/revoke")
def sharing_revoke(user: CurrentUser, viewer: Annotated[str, Form(max_length=64)] = ""):
    with db.engine().begin() as conn:
        data.revoke_share(conn, user, viewer.strip().lower())  # owner = session user, always
    return redirect("/sharing?notice=notice.share_revoked")


@private.get("/admin", response_class=HTMLResponse)
def admin(request: Request, user: CurrentUser):
    now = db.utcnow()
    with db.engine().connect() as conn:
        users = data.all_users(conn, user)  # NotFound (404) for non-admins
        recent = data.recent_uploads_all(conn, user)
        month = data.spent_microusd(conn, data.month_start(now))
        standing = {u.id: quota.standing(conn, u.id, now) for u in users}
    return render(
        request,
        "admin.html",
        users=users,
        recent=recent,
        month_usd=month / 1e6,
        month_budget=config.MONTHLY_BUDGET_USD,
        quota_left={k: (v.base_left / 1e6, v.extra_left / 1e6) for k, v in standing.items()},
        user_quota=config.USER_MONTHLY_QUOTA_USD,
    )


# ---------------------------------------------------------------------------------- own password
# Passwords never reach a log, a template or an error text -- only reason codes do.

_PW_FIELD = Annotated[str, Form(max_length=config.PASSWORD_MAX_LENGTH)]


def _password_page(request: Request, status_code: int = 200, **context) -> HTMLResponse:
    return render(
        request,
        "account_password.html",
        status_code=status_code,
        min_length=config.PASSWORD_MIN_LENGTH,
        **context,
    )


def _set_session_cookie(response: Response, cookie: str, user_id: str) -> None:
    response.set_cookie(
        config.SESSION_COOKIE,
        cookie,
        max_age=security.session_ttl(user_id),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


@private.get("/account/password", response_class=HTMLResponse)
def password_form(request: Request, user: CurrentUser, notice: str | None = None):
    return _password_page(request, notice=notice_key(notice))


@private.post("/account/password")
def password_submit(
    request: Request,
    user: CurrentUser,
    current_password: _PW_FIELD = "",
    new_password: _PW_FIELD = "",
    new_password_repeat: _PW_FIELD = "",
):
    """The session user changes their own password. A wrong current password counts toward the
    login lockout (a stolen session cannot brute-force it); attempts are also limited per user.
    Success revokes EVERY session of the user and gives this device a fresh one + CSRF token."""
    if not limiter.hit(f"password:{user.id}", config.PASSWORD_CHANGE_LIMITS_PER_USER):
        log.info("password change refused user=%s reason=rate_limited", user.id)
        return _password_page(request, 429, errors=["account.pw.err.rate_limited"])
    now = db.utcnow()
    status, errors, cookie = 400, [], None
    with db.engine().begin() as conn:
        row = data.get_login_row(conn, user.id)
        if security.is_locked(row.locked_until, now):
            status, errors = 429, ["account.pw.err.rate_limited"]
        elif not security.verify_password(row.password_hash, current_password):
            failures = (row.failed_login_count or 0) + 1
            lock = security.lockout_seconds(failures)
            conn.execute(
                update(db.app_user)
                .where(db.app_user.c.id == user.id)
                .values(
                    failed_login_count=failures,
                    last_failed_login_at=now,
                    locked_until=now + _seconds(lock) if lock else None,
                )
            )
            errors = ["account.pw.err.wrong_current"]
        else:
            if new_password != new_password_repeat:
                errors.append("account.pw.err.mismatch")
            errors += [f"account.pw.err.{r}" for r in security.password_policy_errors(new_password)]
            if new_password == current_password:
                errors.append("account.pw.err.same")
            if not errors:
                conn.execute(
                    update(db.app_user)
                    .where(db.app_user.c.id == user.id)
                    .values(
                        password_hash=security.hash_password(new_password),
                        password_changed_at=now,
                        password_admin_set=False,  # the user's own password now
                        failed_login_count=0,
                        locked_until=None,
                    )
                )
                security.revoke_all_sessions(conn, user.id)
                cookie = security.create_session(conn, user.id)
    if cookie is None:
        reasons = ",".join(e.rsplit(".", 1)[-1] for e in errors)
        log.info("password change refused user=%s reason=%s", user.id, reasons)
        return _password_page(request, status, errors=errors)
    log.info("password changed user=%s (all sessions revoked)", user.id)
    response = redirect("/account/password?notice=notice.password_changed")
    _set_session_cookie(response, cookie, user.id)
    _set_csrf_cookie(response, security.new_csrf_token())  # rotate on privilege change
    return response


app.include_router(public)
app.include_router(private)
