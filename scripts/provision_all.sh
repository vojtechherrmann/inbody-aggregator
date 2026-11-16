#!/usr/bin/env bash
# One-shot first provisioning + deploy + backfill of vh-personal (Germany West Central).
# Approved by the user 2026-11-16. Every step is idempotent except github-secrets (re-running it
# rotates the deploy SP's secret, which is harmless). Stops at the first error.
#
# Run it yourself (the agent's sandbox may not do Azure writes):
#   tmux new -s inbody-prov "bash /Users/vherrmann/SRC/vojtechherrmann/inbody-aggregator/scripts/provision_all.sh 2>&1 | tee /tmp/inbody-prov.log"
set -euo pipefail

REPO=/Users/vherrmann/SRC/vojtechherrmann/inbody-aggregator
cd "$REPO"
export AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal
export GH_CONFIG_DIR=/Users/vherrmann/.config/gh-personal
export PATH="/Users/vherrmann/.local/bin:/opt/homebrew/bin:$PATH"

step() { printf '\n==== [%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

step "0/11 checks"
[ "$(az account show --query name -o tsv)" = "VojtaPersonal" ] || { echo "wrong subscription"; exit 1; }
[ -s .apikeys/anthropic.env ] || { echo "missing .apikeys/anthropic.env"; exit 1; }
[ -s .apikeys/ntfy.env ] || { echo "missing .apikeys/ntfy.env"; exit 1; }
az extension add --name containerapp --upgrade --only-show-errors

step "1/11 resource group + ACR";            pwsh .azure/basics/basics.ps1
step "2/11 Log Analytics + Container Apps env"; pwsh .azure/basics/gwc.ps1
step "3/11 storage account + container";     pwsh .azure/blob/provision.ps1
step "4/11 SQL server + free-offer DB";      pwsh .azure/sql/provision.ps1
step "5/11 SQL schema + tables"
uv run --with pyodbc python .azure/sql/apply.py .azure/sql/inbody_schema.sql
uv run --with pyodbc python .azure/sql/apply.py .azure/sql/inbody_tables.sql
step "6/11 image build in ACR (uploads source; samples/ and .apikeys/ excluded by .dockerignore)"
az acr build --registry vhgwccr1 --image inbody-web:latest --file src/web/Dockerfile .
step "7/11 container app";                   pwsh .azure/services/web.ps1
step "8/11 SQL user for the app's managed identity"
MI=$(az containerapp show -n vhgwc-inbody-web -g vh-personal --query identity.principalId -o tsv)
uv run --with pyodbc python .azure/sql/apply.py .azure/sql/inbody_grant_web.sql --var "WEB_MI_OBJECT_ID=$MI"
step "9/11 accounts (passwords only into .apikeys/inbody-users.env)"
for u in vojta bara jakub; do SQL_BACKEND=azure .venv/bin/python scripts/users.py add "$u"; done
SQL_BACKEND=azure .venv/bin/python scripts/users.py add vojta-admin --admin
SQL_BACKEND=azure .venv/bin/python scripts/users.py share vojta jakub
step "10/11 GitHub deploy secrets";          pwsh .azure/basics/github-secrets.ps1
step "11/11 health check + backfill of vojta's 4 samples (CSV first)"
FQDN=$(az containerapp show -n vhgwc-inbody-web -g vh-personal --query properties.configuration.ingress.fqdn -o tsv)
for i in 1 2 3 4 5 6; do curl -fsS "https://$FQDN/healthz" && break || sleep 10; done
set -a; . .apikeys/anthropic.env; set +a
SQL_BACKEND=azure STORAGE_BACKEND=azure .venv/bin/python scripts/pipeline.py import --user vojta \
  samples/sample_csv.csv samples/sample_us_app.png \
  samples/sample_sheet_a.jpg samples/sample_sheet_b.jpg

step "DONE: https://$FQDN"
