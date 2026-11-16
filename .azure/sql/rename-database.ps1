# ONE-OFF MIGRATION: delete this script and rowcounts.py once the rename has been applied and verified.
#
# Rename the Azure SQL database `inbody` -> `vojtah` WITHOUT losing data. The database becomes the
# shared home of two schemas: `inbody` (this app, unchanged) and `vojtah` (the site's web users).
#
# `az sql db rename` renames the SAME database in place: tables, rows, schemas, the managed-identity
# SQL users and the point-in-time-restore history all stay. Even so, the script protects the data
# twice: it makes an independent copy first and compares exact row counts before / on the copy /
# after. The copy is NOT deleted automatically.
#
# Two steps (the same convention as custom-domain.ps1):
#   1) pwsh .azure/sql/rename-database.ps1           READ-ONLY plan: checks both names and records
#                                                    the row counts of the current database
#   2) pwsh .azure/sql/rename-database.ps1 -Apply    AZURE WRITES (needs the user's OK):
#        a) az sql db copy  inbody -> inbody_prerename_<date>   (safety copy; verified by row counts)
#        b) az sql db rename inbody -> vojtah                   (in place)
#        c) row counts of `vojtah` must equal the recorded ones, otherwise STOP (nothing is deleted)
#        d) the Container App gets SQL_DATABASE=vojtah          (new revision)
#
# DOWNTIME: from (b) until (d) is healthy the live app still points at the old name and cannot reach
# its database (about 1-3 minutes). Do it when nobody uses the site; the app's own "database is
# starting" page covers the gap. Re-running -Apply after a failure is safe: it detects what is done.
#
# Prerequisites: AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal, az login, `uv`, ODBC Driver 18.
#   `az sql db copy` flags below (serverless, auto-pause) follow the az docs but are UNVERIFIED here
#   because nothing was executed; the plan step prints the exact command first.

param(
    [string]$From = "inbody",
    [string]$To = "vojtah",
    [switch]$SkipCopy,   # no safety copy: Azure refuses `az sql db copy` of this free-offer DB (CannotUpdateIsLedgerDatabase); the 7-day automatic backups (PITR) are the net
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$subscription = "VojtaPersonal"
$rg           = "vh-personal"
$server       = "vhgwcsql1"
$app          = "vhgwc-inbody-web"
$stamp        = Get-Date -Format "yyyyMMdd"
$backup       = "${From}_prerename_$stamp"
$here         = $PSScriptRoot
$tmp          = [System.IO.Path]::GetTempPath()

$current = az account show --query name -o tsv
if ($current -ne $subscription) {
    throw "Signed in to '$current', expected '$subscription'. Set AZURE_CONFIG_DIR=/Users/vherrmann/.azure-personal"
}

function Get-DbNames { az sql db list -g $rg -s $server --query "[].name" -o tsv }
function Get-RowCounts([string]$db) {
    $out = & uv run --with pyodbc python "$here/rowcounts.py" --database $db
    if ($LASTEXITCODE -ne 0) { throw "row counts of '$db' failed (exit $LASTEXITCODE)" }
    return ($out | Where-Object { $_ })
}

$names = @(Get-DbNames)
Write-Host "Databases on ${server}: $($names -join ', ')"
$haveFrom = $names -contains $From
$haveTo   = $names -contains $To
if ($haveTo -and -not $haveFrom) { Write-Host "'$To' already exists and '$From' does not: the rename is done." }
elseif ($haveTo -and $haveFrom) { throw "Both '$From' and '$To' exist: resolve this by hand (nothing was changed)." }
elseif (-not $haveFrom) { throw "Neither '$From' nor '$To' exists on $server." }

$beforeFile = Join-Path $tmp "rowcounts-$From-before.txt"
if ($haveFrom) {
    Write-Host "`nRow counts of '$From' (counts only, no contents):"
    $before = Get-RowCounts $From
    $before | ForEach-Object { Write-Host "  $_" }
    $before | Set-Content $beforeFile
    Write-Host "(saved to $beforeFile)"
}

if (-not $Apply) {
    Write-Host "`nPLAN (nothing was changed):"
    Write-Host "  1. az sql db copy -g $rg -s $server -n $From --dest-name $backup --dest-server $server"
    Write-Host "  2. az sql db rename -g $rg -s $server -n $From --new-name $To"
    Write-Host "  3. compare row counts, then: az containerapp update -n $app -g $rg --set-env-vars SQL_DATABASE=$To"
    Write-Host "Run again with -Apply after the user's OK."
    return
}

if ($haveFrom) {
    # a) independent safety copy, verified
    if ($SkipCopy) {
        Write-Host "Safety copy SKIPPED (-SkipCopy). Safety net: automatic backups, point-in-time restore (7 days) and the row counts below."
    }
    elseif ($names -notcontains $backup) {
        az sql db copy -g $rg -s $server -n $From --dest-name $backup --dest-server $server `
            --service-objective GP_S_Gen5_2 --auto-pause-delay 60
        if ($LASTEXITCODE -ne 0) { throw "safety copy failed (exit $LASTEXITCODE); nothing was renamed" }
    }
    if (-not $SkipCopy) {
        $copyCounts = Get-RowCounts $backup
        if ((Compare-Object $before $copyCounts).Count -ne 0) {
            throw "The safety copy '$backup' differs from '$From' (row counts). NOTHING was renamed."
        }
        Write-Host "Safety copy '$backup' verified (row counts identical)."
    }

    # b) rename in place
    az sql db rename -g $rg -s $server -n $From --new-name $To
    if ($LASTEXITCODE -ne 0) { throw "rename failed (exit $LASTEXITCODE); '$From' is unchanged" }
}

# c) the data must be all there
$beforeSaved = if (Test-Path $beforeFile) { Get-Content $beforeFile } else { $null }
$after = Get-RowCounts $To
if ($beforeSaved -and (Compare-Object $beforeSaved $after).Count -ne 0) {
    throw "Row counts of '$To' differ from the recorded ones. STOP. The safety copy '$backup' is untouched."
}
Write-Host "Row counts of '$To' match the recorded ones."

# d) point the app at the new name
az containerapp update -n $app -g $rg --set-env-vars "SQL_DATABASE=$To"
if ($LASTEXITCODE -ne 0) { throw "app env update failed (exit $LASTEXITCODE): set SQL_DATABASE=$To by hand" }

Write-Host "`nDone. Verify: curl -fsS https://<app host>/healthz, log in once, open History."
Write-Host "Keep '$backup' for a few days; when you are sure, delete it yourself:"
Write-Host "  az sql db delete -g $rg -s $server -n $backup --yes"
Write-Host "Next: provisioning of schema 'vojtah' (site-api/.azure/sql) and `web.ps1` already use '$To'."
