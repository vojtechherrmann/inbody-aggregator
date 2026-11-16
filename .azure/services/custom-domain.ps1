# Move the website to a custom domain (default inbody.vojtah.cz): bind the host name to
# `vhgwc-inbody-web`, let Azure issue the free managed certificate, and switch PUBLIC_BASE_URL.
# After that the app redirects every GET/HEAD on the old *.azurecontainerapps.io host to the new
# one (app.canonical_host). Cookies are `__Host-` bound, so everybody signs in once more.
#
# Two steps, because DNS has to exist first:
#   1) pwsh .azure/services/custom-domain.ps1            READ-ONLY: prints the DNS records to
#                                                         create at the registrar, checks them
#   2) pwsh .azure/services/custom-domain.ps1 -Apply     AZURE WRITES (needs the user's OK):
#                                                         hostname add + managed cert + env var
# The apex (vojtah.cz) is NOT this app: it is the static homepage (repository vojtah), hosted elsewhere.
#
# Prerequisites: AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal, az login, the containerapp
# extension, the app already deployed (web.ps1 + web-bpd.yml).

param(
    [string]$Hostname = "inbody.vojtah.cz",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$rg           = "vh-personal"
$cae          = "vhgwccae"
$app          = "vhgwc-inbody-web"

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal"
}

$fqdn = az containerapp show -n $app -g $rg --query "properties.configuration.ingress.fqdn" -o tsv
$verification = az containerapp env show -n $cae -g $rg --query "properties.customDomainConfiguration.customDomainVerificationId" -o tsv
if ($LASTEXITCODE -ne 0 -or -not $fqdn -or -not $verification) { throw "cannot read the app / environment" }

Write-Host "DNS records for $Hostname (create them at the registrar; TTL 300 is fine):"
Write-Host "  CNAME  $Hostname                 ->  $fqdn"
Write-Host "  TXT    asuid.$Hostname   ->  $verification"
Write-Host ""
$cname = (& dig +short CNAME $Hostname 2>$null) -join ""
$txt   = (& dig +short TXT "asuid.$Hostname" 2>$null) -join ""
Write-Host ("  CNAME seen in DNS: " + ($(if ($cname) { $cname } else { "(none yet)" })))
Write-Host ("  TXT   seen in DNS: " + ($(if ($txt) { "present" } else { "(none yet)" })))
$ready = ($cname.TrimEnd(".") -eq $fqdn) -and ($txt -like "*$verification*")

if (-not $Apply) {
    Write-Host ""
    Write-Host $(if ($ready) { "DNS is ready. Run again with -Apply (after the user's OK)." } else { "DNS is not ready yet; nothing was changed." })
    return
}
if (-not $ready) { throw "DNS records are not in place yet -- refusing to bind (see above)." }

# 1) host name, 2) managed certificate + SNI binding (CNAME validation), 3) the canonical URL.
az containerapp hostname add -n $app -g $rg --hostname $Hostname
if ($LASTEXITCODE -ne 0) { throw "hostname add failed (exit $LASTEXITCODE)" }
az containerapp hostname bind -n $app -g $rg --hostname $Hostname --environment $cae --validation-method CNAME
if ($LASTEXITCODE -ne 0) { throw "hostname bind failed (exit $LASTEXITCODE)" }
az containerapp update -n $app -g $rg --set-env-vars "PUBLIC_BASE_URL=https://$Hostname"
if ($LASTEXITCODE -ne 0) { throw "env update failed (exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "Done. Verify: curl -fsS https://$Hostname/healthz ; curl -sI https://$fqdn/ (expect 308 -> https://$Hostname/)"
Write-Host "Keep PUBLIC_BASE_URL on re-provisioning: pass -CustomDomain $Hostname to web.ps1."
