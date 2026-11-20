# Provision Azure SQL for vh-personal: logical server (Entra-only auth, like the monorepo) + the
# `vojtah` database on the Basic tier (5 DTU, 2 GB, ~$4.90/month, always on).
#
# Basic since 2026-11-20. The database started (2026-11-16) on the FREE OFFER (General Purpose
# serverless Gen5, 100 000 vCore-seconds/month, AutoPause on exhaustion), but internet scanners hit
# the public app host about hourly, every hit woke the database, and 90 % of the month's
# vCore-seconds were gone after 4 days -- the database would then have paused until the 1st,
# taking InBody and the vojtah.cz sign-in down with it. Basic has no monthly limit and never
# auto-pauses (no cold first query). Data is ~10 MB, far below the 2 GB cap.
# Switched in place with:
#   az sql db update -g vh-personal -s vhgwcsql1 -n vojtah --service-objective Basic --max-size 2GB --use-free-limit false
# (`--edition Basic --capacity 5` fails: az keeps family Gen5 from the old SKU and finds no match.)
#
# Region: germanywestcentral. Checked 2026-11-16 with the capability API (read-only, the
# monorepo's docs/design/trading-db-split.md §1 method): `az sql db list-editions -l <region>`
# reports GeneralPurpose `Available` + serverless GP_S_Gen5_* `Available` there; westeurope and
# northeurope report `Visible` (= not offered to this subscription), swedencentral is `Available`.
# The FQDN is global, so the region never appears in connection strings.
#
# Prerequisites: AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal, az login, basics.ps1 run.
# Afterwards apply inbody_schema.sql (see AZURE.md for the exact command).

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$rg           = "vh-personal"
$location     = "germanywestcentral"
$server       = "vhgwcsql1"          # globally unique -> vhgwcsql1.database.windows.net
$db           = "vojtah"             # shared database: schema `inbody` (this app) + `vojtah` (web users)

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal."
}

# Current signed-in user becomes the Entra (Azure AD) admin of the server.
$adminUpn = az ad signed-in-user show --query userPrincipalName -o tsv
$adminSid = az ad signed-in-user show --query id -o tsv

# Logical server with Entra-only authentication (no SQL login, no password anywhere).
az sql server create `
    --name $server `
    --resource-group $rg `
    --location $location `
    --enable-ad-only-auth `
    --external-admin-principal-type User `
    --external-admin-name $adminUpn `
    --external-admin-sid $adminSid
if ($LASTEXITCODE -ne 0) { throw "az sql server create failed (exit $LASTEXITCODE)" }

# The database: Basic (5 DTU), 2 GB max size.
az sql db create `
    --resource-group $rg `
    --server $server `
    --name $db `
    --service-objective Basic `
    --max-size 2GB `
    --backup-storage-redundancy Local `
    --zone-redundant false
if ($LASTEXITCODE -ne 0) { throw "az sql db create failed (exit $LASTEXITCODE)" }

# Firewall: allow this machine's public IP (applying the .sql files, local development).
$myIp = (Invoke-RestMethod -Uri "https://api.ipify.org")
az sql server firewall-rule create `
    --resource-group $rg `
    --server $server `
    --name "local-dev" `
    --start-ip-address $myIp `
    --end-ip-address $myIp

# Firewall: allow Azure services (Container Apps have no fixed egress IP on Consumption).
az sql server firewall-rule create `
    --resource-group $rg `
    --server $server `
    --name "allow-azure-services" `
    --start-ip-address "0.0.0.0" `
    --end-ip-address "0.0.0.0"

# Read back what we got.
az sql db show -g $rg -s $server -n $db `
    --query "{sku:currentSku.name, maxSizeBytes:maxSizeBytes, status:status}" -o table

Write-Host "Server FQDN: $server.database.windows.net"
Write-Host "Next: apply .azure/sql/inbody_schema.sql against db '$db' (AZURE.md)."
