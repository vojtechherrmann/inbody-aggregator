# Azure Resources

Subscription **`VojtaPersonal`** (`03d8a2fd-…`, Pay-As-You-Go — the same one as the monorepo's
`vhncus-rg`), resource group **`vh-personal`**, region **Germany West Central (Frankfurt)**.
Provisioning scripts live in `.azure/` (basics, blob, sql, services); each is idempotent and refuses
to run outside `VojtaPersonal`.

Status: **not provisioned yet** (scripts prepared 2026-11-16).

`vh-personal` is meant for personal projects in general, so the RG, ACR, CAE, Log Analytics and the
storage account are **shared** and named by region; the app, the database and the blob container
carry the project name.

| Resource | Type | Name | Purpose | Est. cost / month |
|---|---|---|---|---|
| Resource Group | `Microsoft.Resources/resourceGroups` | `vh-personal` | everything below | $0 |
| Container Registry | `Microsoft.ContainerRegistry/registries` | `vhgwccr1` (Basic, admin on) | images (`vhgwccr1.azurecr.io/inbody-web`) | ~$5 |
| Log Analytics Workspace | `Microsoft.OperationalInsights/workspaces` | `vhgwcworkspace` (30 d) | Container Apps logs | ~$0–0.20 (a few MB/month) |
| Container Apps Environment | `Microsoft.App/managedEnvironments` | `vhgwccae` (Consumption) | runtime env | $0 |
| Container App | `Microsoft.App/containerApps` | `vhgwc-inbody-web` | the website (`src/web`): public HTTPS-only ingress :8000, 0.5 CPU / 1 Gi, **scale 0–1, 1 h cooldown**, system MI; secrets `anthropic-api-key`, `session-secret`, `ntfy-topic`; `APP_ENV=production` | ~$1–4 (≈ $0.054 per active hour; every visit keeps it up ≥ 1 h; $0 idle) |
| Storage Account | `Microsoft.Storage/storageAccounts` | `vhgwcsa1` (StorageV2, LRS, Hot) | blob container `inbody` (MI: Storage Blob Data Contributor on that container only) | ~$0.10 |
| Azure SQL Server | `Microsoft.Sql/servers` | `vhgwcsql1` | Entra-only auth; FQDN `vhgwcsql1.database.windows.net` | $0 |
| Azure SQL Database | `Microsoft.Sql/servers/databases` | `vojtah` (schemas `inbody` = this app, `vojtah` = the site's web users; renamed from `inbody`, see `.azure/sql/rename-database.ps1`) | **free offer**: GP serverless Gen5 (2 vCore max), 100k vCore-s + 32 GB/month, `AutoPause` when exhausted; schema `inbody` (`001`), MI grant (`002`), tables (`003`, generated) | $0 |
| Entra service principal | — | `gh-inbody-aggregator` | GitHub Actions deploy, Contributor on `vh-personal` only | $0 |

| Claude API (not Azure) | — | `claude-opus-5-5` | sheet extraction (PDF/JPG/PNG); CSV costs nothing | ~$0.10–0.15 per sheet; **hard cap $50/month global**; per user $5/month base quota + optional extra-usage grants (`scripts/users.py grant-extra`); + $50 Console workspace limit |

**Azure total ≈ $6–10 / month**, dominated by ACR Basic. Container Apps' monthly free grant (180k vCPU-s,
360k GiB-s, 2M requests) is per subscription and is most likely already used up by the monorepo's
fleet, so the app is priced at list rates above. The 5 GB/month Log Analytics free ingestion is per
billing account (shared with the monorepo too).

## Provisioning order

Every command needs the personal profile; from a clean shell:

```bash
cd /Users/vherrmann/SRC/vojtechherrmann/inbody-aggregator
export AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal GH_CONFIG_DIR=/Users/vherrmann/.config/gh-personal
pwsh .azure/basics/basics.ps1                 # RG vh-personal + ACR vhgwccr1
pwsh .azure/basics/gwc.ps1                    # vhgwcworkspace + vhgwccae
pwsh .azure/blob/provision.ps1                # vhgwcsa1 + container inbody
pwsh .azure/sql/provision.ps1                 # vhgwcsql1 + free DB vojtah + firewall
uv run --with pyodbc python .azure/sql/apply.py .azure/sql/inbody_schema.sql
uv run --with pyodbc python .azure/sql/apply.py .azure/sql/inbody_tables.sql
az acr build --registry vhgwccr1 --image inbody-web:latest --file src/web/Dockerfile .   # first image (ACR Tasks, ~2–4 min)
# needs .apikeys/anthropic.env (ANTHROPIC_API_KEY=...); optional .apikeys/ntfy.env (INBODY_NTFY_TOPIC=...)
pwsh .azure/services/web.ps1                  # vhgwc-inbody-web (secrets, HTTPS only, 1 h cooldown) + blob RBAC; prints the MI GUID
uv run --with pyodbc python .azure/sql/apply.py .azure/sql/inbody_grant_web.sql --var WEB_MI_OBJECT_ID=<MI GUID from web.ps1>
SQL_BACKEND=azure .venv/bin/python scripts/users.py add vojta            # passwords -> .apikeys/inbody-users.env (0600), never printed
SQL_BACKEND=azure .venv/bin/python scripts/users.py add vojta-admin --admin
SQL_BACKEND=azure .venv/bin/python scripts/users.py add bara
pwsh .azure/basics/github-secrets.ps1         # SP + AZURE_CREDENTIALS / REGISTRY_* repo secrets
```

After that, every deploy is `.github/workflows/web-bpd.yml` (needs the code pushed to GitHub).

Backfill (after the deploy, with the user's OK; same path + limits as the web):
`SQL_BACKEND=azure STORAGE_BACKEND=azure .venv/bin/python scripts/pipeline.py import --user vojta samples/sample_csv.csv samples/sample_us_app.png samples/sample_sheet_a.jpg samples/sample_sheet_b.jpg`
(order matters — first come wins per day: CSV first; expected 12 measurements, the Gym C sheet
`nothing_new`; needs ANTHROPIC_API_KEY in the environment for the 3 sheets, ~$0.40 in total).

Adding the push later: put the topic into `.apikeys/ntfy.env`, then
`az containerapp secret set -n vhgwc-inbody-web -g vh-personal --secrets ntfy-topic=<from the file>` +
`az containerapp update -n vhgwc-inbody-web -g vh-personal --set-env-vars INBODY_NTFY_TOPIC=secretref:ntfy-topic`.

## Database rename `inbody` -> `vojtah` (shared database, no data loss)

The SQL database is renamed in place to **`vojtah`**; it holds two schemas: `inbody` (this app, tables
unchanged) and `vojtah` (the site's web users, owned by the private `vojtah-internal` repo). `az sql db rename` keeps tables, rows,
the managed-identity SQL users and the restore history; `rename-database.ps1` additionally makes an
independent verified copy first and compares exact row counts before / on the copy / after.

1. `pwsh .azure/sql/rename-database.ps1` (read-only plan: names + row counts, nothing changed).
2. After the user's OK, when nobody uses the site: `pwsh .azure/sql/rename-database.ps1 -Apply`
   (safety copy `inbody_prerename_<date>` -> rename -> counts must match -> `SQL_DATABASE=vojtah` on the
   Container App; about 1-3 minutes during which the live app cannot reach its database).
3. Verify `/healthz`, log in, open History; keep the copy a few days, then delete it by hand.
4. `web.ps1`, `provision.ps1`, `apply.py` and the `SQL_DATABASE` default already use `vojtah`.
5. One-off: afterwards delete `rename-database.ps1` and `rowcounts.py` (and this section). Provisioning from
   scratch creates `vojtah` directly; there is no migration chain to keep.

## Custom domain (move to `inbody.vojtah.cz`)

The registrar domain `vojtah.cz` has **no hosting** (DNS only). The apex serves the static homepage
(repository `vojtah`, GitHub Pages); the app moves to the subdomain
`inbody.vojtah.cz`. Steps, in this order:

1. `pwsh .azure/services/custom-domain.ps1` (read-only): prints the two DNS records to create at the
   registrar (`CNAME inbody -> <app fqdn>`, `TXT asuid.inbody -> <environment verification id>`) and
   checks whether they resolve.
2. After the user's OK: `pwsh .azure/services/custom-domain.ps1 -Apply` (hostname add, free managed
   certificate, `PUBLIC_BASE_URL=https://inbody.vojtah.cz`). Verify `/healthz` on the new host.
3. From then on the app redirects every GET/HEAD on the old `*.azurecontainerapps.io` host to
   `PUBLIC_BASE_URL` (`canonical_host` middleware; `/healthz` and `/dbstatus` are exempt). Cookies are
   `__Host-` bound, so everybody signs in again once. ntfy links follow `PUBLIC_BASE_URL`.
4. A later `web.ps1` re-run must get `-CustomDomain inbody.vojtah.cz`, or it resets the URL.

## Region / offer checks (read-only, 2026-11-16)

- SQL capability API (`az sql db list-editions -l <region>` / `--available`, the monorepo's method
  from `docs/design/trading-db-split.md` §1): **germanywestcentral** GeneralPurpose `Available`,
  serverless `GP_S_Gen5_*` `Available`; **swedencentral** `Available`; **westeurope** and
  **northeurope** `Visible` = not offered to this subscription (like NC US / East US in the
  monorepo). ServerQuota 0/250 everywhere.
- Free offer: the subscription's only SQL server (`vhncussql1`: `data` S0, `trading` S1) has
  `useFreeLimit = null` on every database → the one free database per subscription is unused.

## Auth & conventions

- **Authentication:** Managed Identity / Entra (`DefaultAzureCredential`); Azure SQL is
  Entra-only, admin = the signed-in user at provisioning time.
- **Naming:** prefix `vhgwc` (vh + Germany West Central). Globally unique resources have no hyphen
  (`vhgwccr1`, `vhgwcsa1`, `vhgwcsql1`), container apps use hyphens (`vhgwc-inbody-web`).
