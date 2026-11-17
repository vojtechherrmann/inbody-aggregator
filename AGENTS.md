# Project Context

> The InBody Aggregator is one part of the **vojtah.cz** site and lives in its own repository.
> Related repositories: **`vojtah`** (public; the static start page of vojtah.cz) and
> **`vojtah-internal`** (private; the site's login API, its web-user tables in schema `vojtah`,
> the project list). Nothing here depends on them at build time.

> **The single instruction source for EVERY coding agent** — Claude Code (via `CLAUDE.md` →
> `@AGENTS.md`), Codex, Cursor, … Edit **this** file, not `CLAUDE.md`, which holds only Claude
> Code specifics. Conventions are carried over from the user's monorepo
> (`vojtechherrmann/monorepo`); when in doubt, do what it does.

**inbody-aggregator** — a small private website (public landing, login-only app) where a few
friends upload InBody body-composition results and follow them over time. FastAPI + HTMX,
server-rendered, no JSON API. **The binding spec of the upload logic is
[`docs/upload-pipeline.md`](docs/upload-pipeline.md)** (owned by the coordinator — do not edit it;
when the code must deviate, say so). The extraction package (field registry, prompt, schemas,
goldens) is [`extraction/`](extraction/README.md). The Report and History tab UI (charts) is being
designed separately (`docs/ui-design.md`, `docs/prototype/`); their templates are placeholders.

## Layout

| Path | What |
|---|---|
| `src/web/app.py` | routes, middleware (security headers, global rate limit, body cap, CSRF cookie, language) |
| `src/web/data.py` | ALL user-data reads/writes with authorization: `can_view` / `can_edit` (anti-IDOR) |
| `src/web/pipeline.py` | admission → processing (CSV parser or Claude) → day check → report; reprocess, rebuild-index, delete |
| `src/web/daycheck.py` | one measurement per user per day, first come wins; reports |
| `src/web/extract.py` | the Claude call: budget check, retry, usage/cost accounting |
| `src/web/quota.py` | spend limits: global cap, base monthly quota, extra-usage grants, allocation |
| `src/web/sheet_schema.py`, `csv_parsers.py`, `prechecks.py` | the sheet / CSV adapters over `extraction/`, defensive checks, pre-LLM checks |
| `src/web/fields.py`, `sheetpkg.py` | canonical columns DERIVED from `extraction/validate.py` (never a second field list) |
| `src/web/notify.py` | ntfy push to Vojta (value-free) |
| `src/web/security.py`, `ratelimit.py` | Argon2id, sessions, CSRF, lockout; in-memory limiter |
| `src/web/i18n/{cs,en}.json` | all UI strings (same keys; a test checks) |
| `scripts/users.py` | the ONLY way to create accounts (no web registration); extra-usage grants; refuses the guest id |
| `scripts/seed_guest.py` | (re)creates the guest account (id `guest`) + its fictional sample data (idempotent; `SQL_BACKEND=azure` like users.py) |
| `scripts/pipeline.py` | `import --user U files…`, `reprocess`, `rebuild-index` |
| `scripts/gen_sql.py` | generates `.azure/sql/inbody_tables.sql` from `db.py` (a test checks it is current) |
| `scripts/live_extract_check.py` | live API check of the 3 sample sheets — sends health data; only with the user's OK |
| `tests/` | pytest, mirrors `src/`; golden tests skip without `samples/` |
| `.azure/`, `.github/workflows/` | provisioning (pwsh) and CI/deploy, see `AZURE.md` |

Root holds only README / AGENTS / CLAUDE / AZURE. No new `.md` files in the root.

## Health data and privacy

- ⛔ `samples/` (and `samples/expected/`) is **real health data**: gitignored, never committed,
  never uploaded, never sent to any API, never read into an agent's context. Tests that use it skip
  when it is absent and print field names / counts only, never values.
- Names and sheet IDs are never stored. Pushes (ntfy.sh is a public relay) carry status, reason
  CODE, user id, upload id and a link — never a measured value or an error text.
- Blob is the source of truth (`raw/<user>/<yyyy-mm>/<uuid>-<name>`, private container; ONE blob
  per user + SHA-256, in `archive/…` while no live upload references it); files are never
  exposed by URL; the raw file is streamed through the app to the ADMIN only (`/files/<id>`, the
  review/audit pages), for everybody else it is a 404: no page offers a download to users.

## Working with this user

- Chat in **Czech, informal (tykání)**; code, comments, commit messages in **English**. Say
  plainly when a premise is wrong.
- **The user is usually not at this Mac** (Remote Control from another Mac, SSH shell here: clean
  zsh in `$HOME`, no screen, no clipboard). Run what you can yourself; a command for them is one
  copyable block with absolute paths / `cd /Users/vherrmann/SRC/vojtechherrmann/inbody-aggregator && …`, no
  secrets, no `open` / `pbcopy` / bare `localhost`. Claude: personal skill `remote-user`.
- **Profiles:** every `az` / `gh` call needs the PERSONAL profile —
  `AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal` (subscription `VojtaPersonal`) and
  `GH_CONFIG_DIR=/Users/vherrmann/.config/gh-personal`. Unset = the WORK tenant. Claude Code gets
  them from `.claude/settings.local.json`; put them explicitly in front of any command you hand over.
  Never `az account set`.
- Big outputs go to a file, not the chat. Never print a secret.

## Git

- Commit = **one command, named paths, and the `--`**:
  `git add <my paths> && git commit -m "..." -- <my paths>`. Never leave anything staged between
  commands. Other sessions/subagents may share this tree: do not revert or "fix" files you did not
  touch.
- ⛔ Never `git reset --hard`, `git checkout .`, `git restore .`, whole-tree `git stash`,
  `git clean -fd`, amend/rebase of pushed commits, force push. Use the path-limited forms.
- Work on `master`; **ask the user before committing and before pushing**.
- End the commit message with a trailer naming the agent and model, e.g.
  `Co-Authored-By: Codex (gpt-5.x) <noreply@openai.com>`.
- Claude Code enforces the destructive/sweeping cases with `.claude/hooks/git-guard.py` (escape
  hatch `#multi-agent-ok`; tests `tests/hooks`). Other agents: the rule is on you.

## Azure

- Everything lives in RG **`vh-personal`**, **Germany West Central**; full table in `AZURE.md`.
- ⛔ **No Azure write and no deploy (`web-bpd.yml` dispatch) without the user's explicit OK for
  that action.** Reads are fine. Claude Code's permission rules deny direct `az … create/update/
  delete`, role assignments, ACR build/credential, `gh secret set` and `gh workflow run`.
- Names: `vh` + region code (`gwc`) + role. Globally unique names have no hyphen (`vhgwccr1`,
  `vhgwcsa1`, `vhgwcsql1`); apps use hyphens and the project name (`vhgwc-inbody-web`).
- Auth: managed identity / Entra only. Azure SQL is **Entra-only** (no SQL logins). Blob via MI
  RBAC, no account keys in apps. App secrets (Anthropic key, session secret, ntfy topic) are
  Container App secrets, locally in `.apikeys/` — never printed, never in the repo or memory.
- SQL: provisioning is written FROM SCRATCH for the target state (no numbered migration chain):
  `.azure/sql/inbody_tables.sql` is GENERATED from `db.py` (`scripts/gen_sql.py`, a test checks it is
  current). A change to the LIVE database is a one-off, re-runnable ALTER script that is applied once
  and then deleted (the generated file already holds the final shape).
  A grant to an MI user is `DROP USER IF EXISTS` + `CREATE USER … WITH OBJECT_ID`, never an
  `IF NOT EXISTS` guard. The database is `vojtah` (schema `inbody` = this app; schema `vojtah` = the site's web users, owned by the private `vojtah-internal` repo; renamed from `inbody`, see `AZURE.md`) and is on the **free offer** (serverless, auto-pauses): the first
  query after a pause takes ~1 min — connect with retry.

## App rules that are easy to break

- **Authorization:** the viewer is ALWAYS the session user; every read of someone's data goes
  through `data.can_view` (owner, admin, or an active share); deletes through `data.can_edit`
  (owner or admin, never a shared viewer). Missing and forbidden look the same (404). Every
  non-public route sits on the `private` router (auth as a router dependency); every unsafe
  request needs the CSRF token (router dependency on both routers). Tests walk all routes.
- **One process, one replica.** The rate limits are in memory and the background worker is a
  single thread (sequential budget checks); Container App max replicas = 1, uvicorn 1 worker.
  Lockout and the daily upload count are in SQL.
- **Spend** (`config.py` + `quota.py`): a GLOBAL cap of $50 per calendar month on top of
  everything; per user a base quota of $5 per calendar month (same for all) + "extra usage" grants
  (`scripts/users.py grant-extra <user> <usd> [--days 7]`, CLI only; expired / revoked grants
  lapse). The worst case (input + max_tokens, primary + fallback model) must fit
  min(global remaining, user remaining) before every call incl. the retry; the actual cost per
  attempt goes to `api_usage` and is allocated base-first, then soonest-expiring grant
  (`usage_allocation`, rebuildable by replay). The Anthropic Console workspace limit is also $50.
- **Be defensive:** an unexpected format → `needs_review` + push, nothing saved; never guess.
  Model warnings that send a sheet to review: `unreadable`, `ambiguous`, `inconsistent`, `other`.
  **Non-blocking** (stored in `warnings` only; confirmed by the user 2026-11-16):
  `truncated_value` (e.g. impedance decimals cut off by a narrow column) and `not_printed` (e.g.
  no device model on the Gym B sheet). The exercise table (`exercise_kcal_30min`) is
  informational: stored, never a reason for review. Lists: `src/web/sheet_schema.py`.
- **Deleting never deletes a SQL row or a file:** it sets `deleted = 1` (+ `deleted_at`,
  `deleted_by`) on the measurement and/or the upload. Deleting an upload, or the measurement that
  was its last live day, flags the upload (and its `duplicate_file` rows) deleted and MOVES the
  file `raw/…` → `archive/…` (copy + verify + delete source). Every user-facing read of
  measurements (Report, History, charts) skips deleted rows; deleted uploads stay listed with the
  status label "deleted". reprocess / rebuild-index / duplicate detection ignore deleted uploads.
  Uploading the same file (same SHA-256) again is a normal new upload: the archived blob moves
  back to `raw/` (never stored twice) and the cached Claude answer is reused (no API call, no
  spend); the user is never told about the cache, the earlier upload or the archive.
  `scripts/pipeline.py restore <upload_id>` un-deletes a mistake. Shared viewers can never delete
  (`can_edit`).
- **Accounts are the site's** (since 2026-11-17): `app_user`, `user_session` and `sso_ticket` live
  in schema `vojtah`, created and owned by the site (private repository `vojtah-internal`); this
  app declares them in `db.py` (`USERS_SCHEMA`), they are NOT in `inbody_tables.sql`, and its
  identity has object grants on exactly these three (`inbody_grant_web.sql`). One account and one
  password for vojtah.cz and this app; `scripts/users.py` here and the site's users CLI edit the
  same rows. **Single sign-on:** a visitor signed in on vojtah.cz is sent to `GET /sso?t=<ticket>`
  with a one-time ticket the site's API minted (60 s, sha256 at rest); `security.redeem_sso_ticket`
  burns it in the checking UPDATE, the route requires `Sec-Fetch-Site: same-site` (no login CSRF
  through a planted link), an enabled non-guest account, and starts a normal session (replacing
  the previous one); anything wrong = the login page. Tests: `tests/web/test_sso.py`.
- **Passwords from the admin:** `scripts/users.py add` / `reset-password` set
  `app_user.password_admin_set = 1`; a self-service change clears it. While it is 1, login lands
  on `/history?notice=notice.password_admin_set` (a recommendation with a link, never blocking).
- **Guest account** (`config.GUEST_USER_ID = "guest"`; "Show as Guest" on the landing page `/` only →
  `POST /login/guest`, rate-limited, no password, 2 h session, one account shared by all visitors):
  fictional sample data seeded by `scripts/seed_guest.py` (same generator as the design prototype,
  `docs/prototype/build_example.py`), unusable `password_hash`, never admin. **Read-only, enforced
  on the server:** the `guest_guard` dependency on the `private` router 403s every unsafe request
  of a guest session except `/logout` and `/settings` (cookie-only), and the pages `/upload`,
  `/sharing`, `/account/password`; `data.can_edit` is False for the guest's data (admin too),
  `pipeline.admit` / delete / retry / restore raise `data.GuestReadOnly`. The guest sees only
  itself; no share to or from it (`grant_share` refuses, `can_view` ignores stale rows); only the
  admin can read it. Logout revokes ONLY the caller's session (never `revoke_all_sessions` for the
  guest). The UI shows a banner under the app bar and renders Upload / Sharing / bins / Change
  password as `aria-disabled` with a title. `users.py add / reset-password / apply-passwords /
  grant-extra / share` refuse the id; `users.py disable guest` is the off switch (login → 503).
- **Theme** (Settings > Appearance: dark / light / system, **default dark**, whatever the OS prefers):
  the 4th part of the `inbody_units` cookie (always written: `dark|light|system`; a cookie without it
  or an unknown value = dark), rendered as `<html data-theme="dark|light">`, no attribute for "system"
  (then `prefers-color-scheme` decides); app.css keeps the dark tokens twice (media query +
  `[data-theme="dark"]`), `tests/web/test_theme.py` keeps them equal.
- **Cold database** (`src/web/dbwarm.py`): Azure SQL auto-pauses; a request arriving while nothing succeeded for
  30 min starts a background `SELECT 1` warm-up and waits 1.5 s; if the DB is still down the visitor gets
  `dbwait.html` (503, amber "Starting the database" strip, polls `/dbstatus` and returns to the page).
  Anonymous `/`, `/login`, `/data`, `/healthz`, `/dbstatus` and static files never wait. SQLite is always warm.
- **Canonical host:** when `PUBLIC_BASE_URL` is set, `app.canonical_host` redirects (308) every GET /
  HEAD that arrives under another host name to it (path + query kept; `/healthz`, `/dbstatus` exempt).
  The planned move to `inbody.vojtah.cz` is `.azure/services/custom-domain.ps1` (see `AZURE.md`).
- **Day rule:** one measurement per user per day, first come wins; times are informational and
  never converted or "fixed". **Enrichment:** a later file for the same day fills only the values the
  day's measurement is missing (NULL), when it matches the same measurement (`daycheck.enrich`,
  `docs/upload-pipeline.md` section 3); it never overwrites.

## Development

- `uv sync` creates `.venv`; call binaries directly: `.venv/bin/python`, `.venv/bin/pytest`,
  `.venv/bin/ruff` (no `source activate`). Local `.venv` is Python 3.14, the image is
  `python:3.12-slim`, `requires-python >= 3.12` — same as the monorepo.
- Run locally: `cd src/web && ../../.venv/bin/uvicorn app:app --port 8000` (SQLite + local blob
  dir under `.storage/`; create a user with `scripts/users.py add <id>`; secure cookies work on
  `localhost`).
- Dependencies resolve only releases ≥ 2 weeks old (`[tool.uv] exclude-newer` in pyproject).
- Front-end libraries are vendored in `src/web/static/vendor/` (CSP `script-src 'self'`).
- Before a commit: `ruff check --fix` + `ruff format` on touched files, `pytest`. `pre-commit
  install` once per clone runs ruff for you.
- **TDD is the default**: red → green → refactor; a bugfix starts with a failing test.
  Exceptions (say so): infra scripts, docs.
- ⛔ **Tests never touch real Azure** (SQL, blob, ARM). Mock at the client boundary.
- All code, comments and strings in English.

## Deploy

- `web-bpd.yml` (manual): lint + test → build → push `:latest` + `:<sha>` → `az containerapp
  update` to the immutable `:<sha>`. It **builds HEAD** — everything committed ships.
- Rollback = re-deploy the previous revision's image sha (`az containerapp revision list`).
- After a deploy, verify `https://<fqdn>/healthz` returns the new `version` (= commit sha).
