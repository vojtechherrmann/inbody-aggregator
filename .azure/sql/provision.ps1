# Provision Azure SQL for vh-personal: logical server (Entra-only auth, like the monorepo) + the
# `inbody` database on the Azure SQL Database FREE OFFER.
#
# Free offer (decided 2026-11-16): General Purpose serverless Gen5, 100 000 vCore-seconds +
# 32 GB data + 32 GB backup per month at $0. ONE free database per subscription -- checked
# 2026-11-16: the only other server (monorepo vhncussql1: data S0, trading S1) does not use it.
# --free-limit-exhaustion-behavior AutoPause = when the month's vCore-seconds run out the database
# PAUSES until the 1st (never bills); the alternative BillOverUsage would silently start billing.
# Serverless also auto-pauses when idle, so the first query after a pause waits ~1 min for resume
# -- the app must retry its first connect (no SQL reads exist yet).
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

# The free database. `--use-free-limit` is accepted on exactly one database per subscription; a
# second attempt fails rather than billing, which is the behaviour we want.
az sql db create `
    --resource-group $rg `
    --server $server `
    --name $db `
    --edition GeneralPurpose `
    --family Gen5 `
    --capacity 2 `
    --compute-model Serverless `
    --use-free-limit true `
    --free-limit-exhaustion-behavior AutoPause `
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

# Read back what we got -- the free flag is the one thing that must not silently be false.
az sql db show -g $rg -s $server -n $db `
    --query "{sku:currentSku.name, free:useFreeLimit, onExhaustion:freeLimitExhaustionBehavior, status:status}" -o table

Write-Host "Server FQDN: $server.database.windows.net"
Write-Host "Next: apply .azure/sql/inbody_schema.sql against db '$db' (AZURE.md)."
