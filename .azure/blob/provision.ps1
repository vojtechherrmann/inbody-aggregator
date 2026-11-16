# Provision Azure Blob Storage for vh-personal.
# Creates the shared storage account (Standard_LRS, locally-redundant to save cost) and the blob
# container this project uses:
#   inbody : inbody-aggregator's files (content to be decided -- e.g. uploaded scan exports)
#
# Blob access is via the app's system-assigned managed identity, granted "Storage Blob Data
# Contributor" on THIS CONTAINER ONLY in .azure/services/web.ps1. No account keys in apps.
#
# Prerequisites: AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal, az login, basics.ps1 run.
# Re-running is idempotent (create is a no-op on existing resources).

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$rg           = "vh-personal"
$location     = "germanywestcentral"
$account      = "vhgwcsa1"           # globally unique, no hyphen
$containers   = @("inbody")

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal."
}

# StorageV2, locally-redundant, TLS 1.2 min, no anonymous blob access (RBAC/MI only).
az storage account create `
    --name $account `
    --resource-group $rg `
    --location $location `
    --sku Standard_LRS `
    --kind StorageV2 `
    --access-tier Hot `
    --min-tls-version TLS1_2 `
    --allow-blob-public-access false
if ($LASTEXITCODE -ne 0) { throw "az storage account create failed (exit $LASTEXITCODE)" }

# Containers through ARM (`container-rm`), NOT the data plane (`container create --auth-mode
# login`) the monorepo uses: on a brand-new account the signed-in owner holds no "Storage Blob
# Data *" role yet, so the data-plane call fails with AuthorizationPermissionMismatch.
foreach ($container in $containers) {
    az storage container-rm create `
        --storage-account $account `
        --resource-group $rg `
        --name $container `
        --public-access off | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "container '$container' failed (exit $LASTEXITCODE)" }
    Write-Host "container '$container' ready"
}

# The signed-in user (the owner) gets Storage Blob Data Contributor on the project container, so
# the Mac-side CLI (scripts/pipeline.py import / reprocess / rebuild-index) can read and write
# raw files with its az CLI identity. Owner of the subscription != data-plane access.
$me  = az ad signed-in-user show --query id -o tsv
$sub = az account show --query id -o tsv
foreach ($container in $containers) {
    az role assignment create --assignee-object-id $me --assignee-principal-type User `
        --role "Storage Blob Data Contributor" `
        --scope "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.Storage/storageAccounts/$account/blobServices/default/containers/$container" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "role assignment on '$container' failed (exit $LASTEXITCODE)" }
}

Write-Host "Storage account '$account' + containers $($containers -join ', ') ready."
Write-Host "The app's MI role grant happens in .azure/services/web.ps1."
