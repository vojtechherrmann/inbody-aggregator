# Provision the shared foundation of the personal resource group: the resource group itself and
# the container registry. Same split as the monorepo's .azure/basics/: region-agnostic pieces here,
# the region's Log Analytics + Container Apps env in <region>.ps1 (gwc.ps1).
#
# `vh-personal` is meant to host more than this one app (personal projects), so the RG, ACR,
# Container Apps env and storage account are SHARED, named by region (vhgwc*), and only the app,
# the blob container carries the project name (inbody); the SQL database is the shared `vojtah`.
#
# Run this FIRST. Idempotent: re-running `create` on existing resources is a no-op / harmless
# update.
#
# Other shared resources have their own provisioning scripts (not duplicated here):
#   Log Analytics + CAE   vhgwcworkspace / vhgwccae   -> .azure/basics/gwc.ps1
#   storage account       vhgwcsa1                    -> .azure/blob/provision.ps1
#   SQL server + db       vhgwcsql1 / inbody          -> .azure/sql/provision.ps1
#
# Prerequisites: az login into the PERSONAL profile -- every call with
#   AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal   (unset = the WORK tenant!)
# The guard below refuses to run anywhere else.

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$rg           = "vh-personal"
$rgRegion     = "germanywestcentral"   # RG metadata home; decided 2026-11-16
$acr          = "vhgwccr1"             # globally unique, no hyphen -> vhgwccr1.azurecr.io

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal."
}

# --- Resource group ----------------------------------------------------------------------------

az group create --name $rg --location $rgRegion
if ($LASTEXITCODE -ne 0) { throw "az group create failed (exit $LASTEXITCODE)" }

# --- Container registry ------------------------------------------------------------------------
# Own registry, NOT the monorepo's vhncuscr1 (decided 2026-11-16): this project must not depend on
# the trading RG. Basic SKU (~$5/month, 10 GiB). Admin user enabled so web.ps1 and the deploy
# workflow can pull/push with REGISTRY_USERNAME / REGISTRY_PASSWORD -- the monorepo's pattern.

az acr create `
    --name $acr `
    --resource-group $rg `
    --location $rgRegion `
    --sku Basic `
    --admin-enabled true
if ($LASTEXITCODE -ne 0) { throw "az acr create failed (exit $LASTEXITCODE)" }

Write-Host "Shared foundation ready: $rg + ACR $acr ($acr.azurecr.io)."
Write-Host "Next: .azure/basics/gwc.ps1, then storage/SQL, then services."
