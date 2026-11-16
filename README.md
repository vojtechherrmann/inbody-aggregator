# InBody Aggregator

A small private website where a few friends upload their [InBody](https://inbody.com)
body-composition results and follow them over time. The landing page is public; everything else
sits behind a login. Accounts are created by the admin from the command line (there is no
registration). A read-only **guest** account with fictional data lets anybody look around
("Show as Guest" on the landing page).

* **Upload** an InBody CSV export, a result-sheet PDF / photo or an app screenshot. A CSV is parsed
  deterministically; a sheet is read by Claude and then checked by plain code (cross-checks,
  ranges, date order). Anything unexpected goes to *needs review* and nothing is saved: the app
  never guesses.
* **Report** shows one measurement with evaluation pills, normal-range bars and changes against the
  previous one. **History** shows charts (body composition, body parts, other metrics) over a
  range you pick, with pauses in the time axis shrunk.
* **Sharing**: you can let a friend or trainer view your data (view only); deleting stays with the
  owner. Czech and English UI, light / dark / system theme.
* One measurement per user per day, first come wins. Deleting is soft (rows and files are kept and
  flagged), so a mistake can be restored by the admin.

## Stack

FastAPI + Jinja + HTMX, server-rendered (no JSON API, no inline scripts or styles: strict CSP).
SQLAlchemy over SQLite locally and Azure SQL in production; blobs on the local disk or Azure Blob
Storage; Anthropic API for sheet extraction. One process, one replica (the rate limits and the
background worker are in memory). Details for contributors and agents: [`AGENTS.md`](AGENTS.md).

| Path | What |
|---|---|
| `src/web/` | the app (routes, data access with authorization, upload pipeline, quota, i18n, templates, static) |
| `extraction/` | the versioned extraction package: field registry, prompt, schemas ([README](extraction/README.md)) |
| `docs/upload-pipeline.md` | the binding spec of the upload logic; `docs/ui-design.md` + `docs/prototype/` the UI design |
| `scripts/` | CLI: users, guest seed, pipeline import / reprocess, SQL generation |
| `.azure/` | provisioning (PowerShell), see [`AZURE.md`](AZURE.md); the CI / deploy workflows are in `.github/workflows/` |

## Run locally

```sh
cd inbody-aggregator
uv sync                                   # creates .venv (Python >= 3.12)
.venv/bin/python scripts/users.py add me  # the only way to create an account
cd src/web && ../../.venv/bin/uvicorn app:app --port 8000
```

It uses SQLite and a local blob directory under `.storage/`. Without `ANTHROPIC_API_KEY` CSV
uploads work; sheet extraction needs a key (and costs money: see the spend limits below).
Copy `.env.example` for the available settings.

```sh
.venv/bin/pytest                          # the test suite (no network, no Azure)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests that compare against real result sheets skip themselves when `samples/` is absent.

## Security and privacy

* Argon2id password hashes, signed `__Host-` cookies, CSRF token on every unsafe request, login
  lockout and rate limits. Every read of somebody's data goes through one authorization function
  (owner, admin or an active share); missing and forbidden look the same (404).
* Azure: managed identity and Entra-only SQL, no keys in the app. Secrets live in Container App
  secrets or the (git-ignored) `.apikeys/` directory, never in the repository.
* Health data is never committed: `samples/` is git-ignored, the tests and examples use synthetic
  values. The app does not store names or sheet IDs, and push notifications (ntfy) carry only a
  status, a reason code, ids and a link, never a measured value.
* Spend is capped: a global monthly limit and a per-user quota with admin-granted extras. The
  worst case of every API call must fit before it is made.

## Deploy

A manual GitHub Actions workflow (`web-bpd.yml`) lints and tests, builds the image, pushes it to a
private registry and updates the Azure Container App to the immutable image tag. Provisioning
scripts and the resource table are in [`AZURE.md`](AZURE.md).

## License

Not licensed for reuse: all rights reserved. The code is public to read.
