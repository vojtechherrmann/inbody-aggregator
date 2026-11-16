# `.azure/basics/` — shared foundation + regional bootstrap

Same split as the monorepo: **shared** (region-agnostic) pieces in `basics.ps1`, the
**region-locked** pieces (Log Analytics + Container Apps env) in `<region>.ps1`. `vh-personal` is
meant to hold more personal projects later, so the RG, ACR, CAE and storage account are shared and
named by region (`vhgwc*`); only the app, the database and the blob container carry the project
name (`inbody`).

Every script is **idempotent** and refuses to run unless `az account show` says `VojtaPersonal`.
Always run with `AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal` (unset = the WORK tenant).

| Script | Scope | Creates |
|---|---|---|
| `basics.ps1` | shared | RG `vh-personal`, ACR `vhgwccr1` |
| `gwc.ps1` | Germany West Central (Frankfurt) | `vhgwcworkspace`, `vhgwccae` |
| `github-secrets.ps1` | GitHub repo | Entra SP `gh-inbody-aggregator` (Contributor on `vh-personal` only) + repo secrets `AZURE_CREDENTIALS`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` |

**Not** in scope here: storage `.azure/blob/provision.ps1`, SQL `.azure/sql/provision.ps1`,
the app `.azure/services/web.ps1`.

## Order of operations

1. `basics.ps1` → 2. `gwc.ps1` → 3. `blob/provision.ps1`, `sql/provision.ps1` (+ `sql/001`) →
4. first image in ACR → 5. `services/web.ps1` → 6. `sql/002` with the app's MI GUID →
7. `github-secrets.ps1` → 8. `web-bpd.yml` for every later deploy. Full commands: `AZURE.md`.
