# Wire the GitHub repo to Azure for .github/workflows/web-bpd.yml: three repo secrets, the same
# names the monorepo uses.
#   AZURE_CREDENTIALS  -- an Entra service principal, Contributor on the vh-personal RG ONLY
#                         (narrower than the monorepo's, which spans the subscription).
#   REGISTRY_USERNAME / REGISTRY_PASSWORD -- ACR vhgwccr1 admin credentials.
#
# No secret is ever printed: each value is piped straight from az into `gh secret set`.
#
# NOT idempotent in one respect: `az ad sp create-for-rbac` on an existing name RESETS that SP's
# client secret, so a re-run rotates AZURE_CREDENTIALS (harmless -- it is set again right after).
#
# Prerequisites: AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal and
# GH_CONFIG_DIR=/Users/vherrmann/.config/gh-personal, both logged in; basics.ps1 already run.

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$rg           = "vh-personal"
$acr          = "vhgwccr1"
$repo         = if ($args.Count -gt 0) { $args[0] } else { "vojtechherrmann/inbody-aggregator" }  # optional arg: owner/repo
$spName       = "gh-inbody-aggregator"

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal."
}
$sub   = az account show --query id -o tsv
$scope = "/subscriptions/$sub/resourceGroups/$rg"

# --json-auth emits the object azure/login@v2 `creds:` expects.
az ad sp create-for-rbac --name $spName --role Contributor --scopes $scope --json-auth `
    | gh secret set AZURE_CREDENTIALS --repo $repo
if ($LASTEXITCODE -ne 0) { throw "AZURE_CREDENTIALS failed (exit $LASTEXITCODE)" }

az acr credential show --name $acr --query username -o tsv `
    | gh secret set REGISTRY_USERNAME --repo $repo
az acr credential show --name $acr --query "passwords[0].value" -o tsv `
    | gh secret set REGISTRY_PASSWORD --repo $repo
if ($LASTEXITCODE -ne 0) { throw "registry secrets failed (exit $LASTEXITCODE)" }

gh secret list --repo $repo
Write-Host "Secrets set on $repo (names above, values never printed)."
