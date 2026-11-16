# Wire GitHub repos to Azure for their deploy workflows (web-bpd.yml here, site-api-bpd.yml in
# vojtah-internal): three repo secrets, the same names the monorepo uses.
#   pwsh github-secrets.ps1 [owner/repo ...]     default: inbody-aggregator AND vojtah-internal
#   AZURE_CREDENTIALS  -- an Entra service principal, Contributor on the vh-personal RG ONLY
#                         (narrower than the monorepo's, which spans the subscription).
#   REGISTRY_USERNAME / REGISTRY_PASSWORD -- ACR vhgwccr1 admin credentials.
#
# No secret is ever printed: each value is piped straight from az into `gh secret set`.
#
# NOT idempotent in one respect: `az ad sp create-for-rbac` on an existing name RESETS that SP's
# client secret, so a re-run rotates AZURE_CREDENTIALS and INVALIDATES the one on every repo it was
# not set on in this run. That is why all repos are given in ONE run: the same new credential is
# set on each of them.
#
# Prerequisites: AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal and
# GH_CONFIG_DIR=/Users/vherrmann/.config/gh-personal, both logged in; basics.ps1 already run.

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$rg           = "vh-personal"
$acr          = "vhgwccr1"
$repos        = if ($args.Count -gt 0) { $args } else { @("vojtechherrmann/inbody-aggregator", "vojtechherrmann/vojtah-internal") }
$spName       = "gh-inbody-aggregator"

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal."
}
$sub   = az account show --query id -o tsv
$scope = "/subscriptions/$sub/resourceGroups/$rg"

# --json-auth emits the object azure/login@v2 `creds:` expects. Kept in memory only, never printed.
$creds = az ad sp create-for-rbac --name $spName --role Contributor --scopes $scope --json-auth
if ($LASTEXITCODE -ne 0 -or -not $creds) { throw "service principal credential failed (exit $LASTEXITCODE)" }
$regUser = az acr credential show --name $acr --query username -o tsv
$regPass = az acr credential show --name $acr --query "passwords[0].value" -o tsv
if ($LASTEXITCODE -ne 0) { throw "registry credential failed (exit $LASTEXITCODE)" }

foreach ($repo in $repos) {
    $creds   | gh secret set AZURE_CREDENTIALS --repo $repo
    if ($LASTEXITCODE -ne 0) { throw "AZURE_CREDENTIALS on $repo failed (exit $LASTEXITCODE)" }
    $regUser | gh secret set REGISTRY_USERNAME --repo $repo
    $regPass | gh secret set REGISTRY_PASSWORD --repo $repo
    if ($LASTEXITCODE -ne 0) { throw "registry secrets on $repo failed (exit $LASTEXITCODE)" }
    gh secret list --repo $repo
    Write-Host "Secrets set on $repo (names above, values never printed)."
}
