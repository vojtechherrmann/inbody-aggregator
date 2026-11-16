# Provision the Germany West Central (Frankfurt) region: Log Analytics workspace + Container Apps
# environment. The RG + ACR come from basics.ps1, which must run first. The app
# (.azure/services/web.ps1) targets the CAE created here.
#
# Idempotent -- re-running `create` on an existing resource is a no-op / harmless update.
#
# Prerequisites: AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal, az login,
# `az extension add --name containerapp`, basics.ps1 already run.

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$location     = "germanywestcentral"   # Frankfurt; decided 2026-11-16
$rg           = "vh-personal"
$workspace    = "vhgwcworkspace"
$cae          = "vhgwccae"

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal."
}

# --- Log Analytics workspace (backs the Container Apps env's console/system logs) ---------------
# PerGB2018, 30-day retention (the free retention window). A scale-to-zero placeholder web logs a
# few MB per month -- well inside the first free 5 GB/month of ingestion per billing account.

az monitor log-analytics workspace create `
    --resource-group $rg `
    --workspace-name $workspace `
    --location $location `
    --retention-time 30
if ($LASTEXITCODE -ne 0) { throw "workspace create failed (exit $LASTEXITCODE)" }

$logId  = az monitor log-analytics workspace show `
    --resource-group $rg --workspace-name $workspace --query customerId -o tsv
$logKey = az monitor log-analytics workspace get-shared-keys `
    --resource-group $rg --workspace-name $workspace --query primarySharedKey -o tsv

# --- Container Apps environment (Consumption only; the environment itself costs nothing) --------

az containerapp env create `
    --name $cae `
    --resource-group $rg `
    --location $location `
    --logs-workspace-id $logId `
    --logs-workspace-key $logKey
if ($LASTEXITCODE -ne 0) { throw "az containerapp env create failed (exit $LASTEXITCODE)" }

Write-Host "Germany West Central ready: $cae (workspace $workspace) in $rg."
Write-Host "Next: storage (.azure/blob/provision.ps1), SQL (.azure/sql/provision.ps1), then services."
