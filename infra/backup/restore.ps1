<#
.SYNOPSIS
    Restore an EAIP database dump.

.DESCRIPTION
    Restores into a NEW database by default. Overwriting an existing one
    requires -Force, because a restore is the operation most likely to be run
    in a hurry by someone who is already having a bad day.

    Runs pg_restore inside the Postgres container, so nothing needs a local
    Postgres client.

    The dump contains embeddings, so this does NOT re-embed. That is the
    difference between a two-second restore and an hour of API calls — see
    RUNBOOK.md for what recovery actually costs.

.PARAMETER DumpFile
    The .dump file to restore. Required.

.PARAMETER Database
    Target database name. Required, and deliberately not defaulted: naming the
    target is how you notice you are about to overwrite the live one.

.PARAMETER Force
    Drop the target database first if it exists. Destructive.

.EXAMPLE
    .\restore.ps1 -DumpFile dumps\eaip-20260820-120000.dump -Database eaip_restored
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DumpFile,
    [Parameter(Mandatory = $true)][string]$Database,
    [string]$Container = "eaip-postgres",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$envFile = Join-Path $scriptDir "..\..\.env"
if (-not (Test-Path $envFile)) {
    throw "No .env found at $envFile."
}

$settings = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
        $settings[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}

$dbUser = $settings["POSTGRES_USER"]
$dbPass = $settings["POSTGRES_PASSWORD"]
$liveDb = $settings["POSTGRES_DB"]

if (-not (Test-Path $DumpFile)) {
    throw "No such dump file: $DumpFile"
}
$DumpFile = (Resolve-Path $DumpFile).Path

$running = docker ps --filter "name=$Container" --filter "status=running" --format "{{.Names}}"
if ($running -ne $Container) {
    throw "Container '$Container' is not running."
}

# Refuse to overwrite the live database without -Force. The parameter is
# mandatory precisely so this check has something to compare against.
if ($Database -eq $liveDb -and -not $Force) {
    throw "'$Database' is the LIVE database. Restore somewhere else, or pass -Force if you mean it."
}

$exists = cmd /c "docker exec -e PGPASSWORD=$dbPass $Container psql -U $dbUser -d postgres -tAc ""SELECT 1 FROM pg_database WHERE datname='$Database'"""
if ($exists -match "1") {
    if (-not $Force) {
        throw "Database '$Database' already exists. Pass -Force to drop and recreate it."
    }
    Write-Host "Dropping existing database '$Database'..."
    # Terminate connections first: DROP DATABASE fails while anything is
    # attached, and a restore that fails halfway is worse than one that
    # refuses to start.
    cmd /c "docker exec -e PGPASSWORD=$dbPass $Container psql -U $dbUser -d postgres -c ""SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$Database'""" | Out-Null
    cmd /c "docker exec -e PGPASSWORD=$dbPass $Container dropdb -U $dbUser $Database"
    if ($LASTEXITCODE -ne 0) { throw "Could not drop '$Database'." }
}

Write-Host "Creating database '$Database'..."
cmd /c "docker exec -e PGPASSWORD=$dbPass $Container createdb -U $dbUser $Database"
if ($LASTEXITCODE -ne 0) { throw "Could not create '$Database'." }

# pgvector must exist BEFORE the restore: the dump contains vector columns, and
# restoring them into a database without the extension fails partway through
# with an error that reads like a corrupt dump rather than a missing extension.
cmd /c "docker exec -e PGPASSWORD=$dbPass $Container psql -U $dbUser -d $Database -c ""CREATE EXTENSION IF NOT EXISTS vector""" | Out-Null

Write-Host "Restoring $DumpFile into '$Database'..."
$restoreOutput = cmd /c "docker exec -i -e PGPASSWORD=$dbPass $Container pg_restore -U $dbUser -d $Database --no-owner --no-privileges < ""$DumpFile""" 2>&1

# pg_restore exits non-zero for warnings as well as failures, so the exit code
# alone is not a verdict. What matters is whether the data arrived, checked
# below.
if ($restoreOutput) {
    $errors = $restoreOutput | Where-Object { $_ -match "error" }
    if ($errors) {
        Write-Host "pg_restore reported:" -ForegroundColor Yellow
        $errors | Select-Object -First 10 | ForEach-Object { Write-Host "  $_" }
    }
}

# Verify the restore is USABLE, not merely finished. Row counts on the tables
# that matter: a restore that produced empty tables is a failure that looks
# like a success.
# One line, deliberately. A here-string does not survive the quoting through
# cmd /c, and the first version of this check failed for that reason while the
# restore itself had worked perfectly.
$check = "SELECT (SELECT count(*) FROM tenant), (SELECT count(*) FROM document), (SELECT count(*) FROM chunk), (SELECT count(*) FROM chunk WHERE embedding IS NOT NULL)"
$counts = cmd /c "docker exec -e PGPASSWORD=$dbPass $Container psql -U $dbUser -d $Database -tAc ""$check"""

if (-not $counts) { throw "The restored database did not answer a query." }

$parts = $counts.Trim() -split '\|'
Write-Host ""
Write-Host "Restored '$Database':"
Write-Host "  tenants:   $($parts[0])"
Write-Host "  documents: $($parts[1])"
Write-Host "  chunks:    $($parts[2])"
Write-Host "  embedded:  $($parts[3])  <- not re-embedded; the dump carried these"

if ([int]$parts[2] -gt 0 -and [int]$parts[3] -eq 0) {
    throw "Chunks restored but NO embeddings survived. Retrieval would return nothing."
}

Write-Host ""
Write-Host "Point the app at it with:  POSTGRES_DB=$Database"
