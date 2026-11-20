# Create `vhgwc-inbody-web` (src/web): the inbody-aggregator website -- one container app on the
# Consumption plan, PUBLIC (no EasyAuth: the app has its own login), HTTPS only, scale 0..1 with a
# 10 min scale-down cooldown. Max replicas = 1 is load-bearing: the in-memory rate limits and the
# single background worker assume exactly one process (AGENTS.md).
#
# Secrets (Container App secrets, the monorepo pattern -- no Key Vault), read from the gitignored
# .apikeys/ files and never printed:
#   anthropic-api-key  <- .apikeys/anthropic.env   ANTHROPIC_API_KEY=...   (required)
#   session-secret     <- .apikeys/inbody-web.env  SESSION_SECRET=...      (generated if missing)
#   ntfy-topic         <- .apikeys/ntfy.env        INBODY_NTFY_TOPIC=...   (optional; the user
#                         creates the topic -- this script never generates it; empty = push off)
# Also set a monthly spend limit on the API key's workspace in the Anthropic Console ($50, equal
# to the app's MONTHLY_BUDGET_USD -- the app's own check stays the primary guard).
#
# Prerequisites: AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal, az login,
# `az extension add --name containerapp`, basics.ps1 + gwc.ps1 + blob + sql run, and an image in
# ACR at $image (first time: `az acr build`, see AZURE.md; afterwards web-bpd.yml).

param(
    # After the move to a custom domain (custom-domain.ps1): the canonical public host name, so a
    # re-provision does not reset PUBLIC_BASE_URL to the *.azurecontainerapps.io address.
    [string]$CustomDomain = ""
)

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$rg           = "vh-personal"
$cae          = "vhgwccae"
$app          = "vhgwc-inbody-web"
$acr          = "vhgwccr1"
$registry     = "$acr.azurecr.io"
$image        = "$registry/inbody-web:latest"
$sa           = "vhgwcsa1"
$container    = "inbody"
$sqlServer    = "vhgwcsql1.database.windows.net"
$sqlDatabase  = "vojtah"                       # shared database; this app owns schema `inbody` in it
$contactEmail = "herrmann.vojtech@gmail.com"   # public on the landing page (approved 2026-11-16)
$cooldownSec  = 600                            # scale to zero after 10 min without traffic (bots hit the public host ~hourly)
$repo         = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal."
}

function Read-EnvValue([string]$file, [string]$name) {
    $path = Join-Path $repo ".apikeys/$file"
    if (-not (Test-Path $path)) { return "" }
    $line = Get-Content $path | Where-Object { $_ -match "^\s*$name=" } | Select-Object -First 1
    if (-not $line) { return "" }
    return ($line -replace "^\s*$name=", "").Trim().Trim('"')
}

$anthropicKey = Read-EnvValue "anthropic.env" "ANTHROPIC_API_KEY"
if (-not $anthropicKey) { throw "Put ANTHROPIC_API_KEY=... into .apikeys/anthropic.env first." }

$sessionSecret = Read-EnvValue "inbody-web.env" "SESSION_SECRET"
if (-not $sessionSecret) {
    # 48 random bytes, base64url; saved (0600) so a re-run keeps sessions valid. Never printed.
    $bytes = [byte[]]::new(48); [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $sessionSecret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $envFile = Join-Path $repo ".apikeys/inbody-web.env"
    New-Item -ItemType Directory -Force -Path (Split-Path $envFile) | Out-Null
    Add-Content -Path $envFile -Value "SESSION_SECRET=$sessionSecret"
    chmod 600 $envFile
}

$ntfyTopic = Read-EnvValue "ntfy.env" "INBODY_NTFY_TOPIC"

# Wait until the app's last write has finished: a second write while ARM is still provisioning
# fails with ContainerAppOperationInProgress (seen 2026-11-16 -- PUBLIC_BASE_URL got lost).
function Wait-AppReady([string]$name, [string]$group) {
    for ($i = 0; $i -lt 36; $i++) {   # 36 x 5 s = 3 min
        $state = az containerapp show -n $name -g $group --query properties.provisioningState -o tsv
        if ($state -eq "Succeeded") { return }
        if ($state -eq "Failed") { throw "$name provisioningState=Failed" }
        Start-Sleep -Seconds 5
    }
    throw "$name still not Succeeded after 3 min (last state: $state)"
}

# The public URL is known BEFORE the app exists: <app>.<environment default domain>. So it goes
# into the create call itself -- one write instead of a racy follow-up update.
$envDomain = az containerapp env show -n $cae -g $rg --query properties.defaultDomain -o tsv
if ($LASTEXITCODE -ne 0 -or -not $envDomain) { throw "cannot read the default domain of $cae" }
$fqdn = "$app.$envDomain"

# Registry admin credentials read straight from the ACR (never printed, never in the environment).
$regUser = az acr credential show --name $acr --query username -o tsv
$regPass = az acr credential show --name $acr --query "passwords[0].value" -o tsv

$secrets = @("anthropic-api-key=$anthropicKey", "session-secret=$sessionSecret")
$envVars = @(
    "APP_ENV=production",
    "APP_VERSION=bootstrap",
    "ANTHROPIC_API_KEY=secretref:anthropic-api-key",
    "SESSION_SECRET=secretref:session-secret",
    "SQL_BACKEND=azure",
    "SQL_SERVER=$sqlServer",
    "SQL_DATABASE=$sqlDatabase",
    "STORAGE_BACKEND=azure",
    "AZURE_STORAGE_ACCOUNT=$sa",
    "AZURE_CONTAINER_NAME=$container",
    "CONTACT_EMAIL=$contactEmail",
    "TRUST_PROXY_HEADERS=1",
    "PUBLIC_BASE_URL=https://$(if ($CustomDomain) { $CustomDomain } else { $fqdn })"
)
if ($ntfyTopic) {
    $secrets += "ntfy-topic=$ntfyTopic"
    $envVars += "INBODY_NTFY_TOPIC=secretref:ntfy-topic"
} else {
    Write-Host "No .apikeys/ntfy.env -> pushes to Vojta are OFF (add the secret later, see AZURE.md)."
}

# 0.25 CPU / 0.5 Gi (since 2026-11-20; was 0.5 / 1 Gi on a pre-launch guess): measured over 7 days the
# working set peaked at 297 MiB and CPU at 95 m. A 10 MB PDF upload (base64 + JSON body) is the
# memory worst case, estimated 350-400 MiB; an OOM is exit 137 and the upload is re-queued on restart.
az containerapp create `
    --name $app `
    --resource-group $rg `
    --environment $cae `
    --image $image `
    --registry-server $registry `
    --registry-username $regUser `
    --registry-password $regPass `
    --system-assigned `
    --ingress external --target-port 8000 --transport auto `
    --min-replicas 0 --max-replicas 1 `
    --cpu 0.25 --memory 0.5Gi `
    --secrets $secrets `
    --env-vars $envVars

# $ErrorActionPreference does NOT catch a native command's exit code -- without this the script
# happily prints "created" after az failed (e.g. MANIFEST_UNKNOWN when the image is not pushed).
if ($LASTEXITCODE -ne 0) { throw "az containerapp create failed (exit $LASTEXITCODE)" }

Wait-AppReady $app $rg
$sub        = az account show --query id -o tsv
$mi         = az containerapp show -n $app -g $rg --query identity.principalId -o tsv
$actualFqdn = az containerapp show -n $app -g $rg --query "properties.configuration.ingress.fqdn" -o tsv
if ($actualFqdn -ne $fqdn) { throw "FQDN is $actualFqdn, expected $fqdn (PUBLIC_BASE_URL would be wrong)" }

# HTTPS only (HTTP requests are redirected) + the 10 min scale-down cooldown -- the only follow-up
# write; it touches ingress + scale only, never the container env. `--cooldown-period` is
# not an `az containerapp` flag, so PATCH the ARM resource (the monorepo's vhncus-web recipe).
az rest --method patch `
    --url "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.App/containerApps/$app`?api-version=2024-10-02-preview" `
    --headers "Content-Type=application/json" `
    --body "{`"properties`":{`"configuration`":{`"ingress`":{`"allowInsecure`":false}},`"template`":{`"scale`":{`"cooldownPeriod`":$cooldownSec}}}}"
if ($LASTEXITCODE -ne 0) { throw "ingress/cooldown patch failed (exit $LASTEXITCODE)" }
Wait-AppReady $app $rg   # before any further write

# ------------------------------------------------------------------------------------------ RBAC
# Blob: Data Contributor on the project's OWN container, not the whole (shared) account.
az role assignment create --assignee-object-id $mi --assignee-principal-type ServicePrincipal `
    --role "Storage Blob Data Contributor" `
    --scope "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.Storage/storageAccounts/$sa/blobServices/default/containers/$container"
if ($LASTEXITCODE -ne 0) { throw "blob role assignment failed (exit $LASTEXITCODE)" }

Write-Host "$app created (public HTTPS ingress, target 8000, scale 0..1, cooldown $cooldownSec s, 0.25 CPU/0.5 Gi)."
Write-Host "URL: https://$fqdn"
Write-Host ""
Write-Host "NEXT:"
Write-Host " 1) SQL user for the app's managed identity (as the Entra admin):"
Write-Host "    AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal uv run --with pyodbc python .azure/sql/apply.py .azure/sql/inbody_grant_web.sql --var WEB_MI_OBJECT_ID=$mi"
Write-Host " 2) Accounts: SQL_BACKEND=azure .venv/bin/python scripts/users.py add vojta | add vojta-admin --admin | add bara"
Write-Host " 3) Verify: curl -fsS https://$fqdn/healthz   (cold start: allow ~10-20 s)"
Write-Host " 4) GitHub deploys: .azure/basics/github-secrets.ps1, then dispatch web-bpd.yml."
