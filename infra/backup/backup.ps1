<#
.SYNOPSIS
    Take a backup of the EAIP database.

.DESCRIPTION
    Runs pg_dump INSIDE the Postgres container, so nothing needs a local
    Postgres client installed. The dump is streamed out to the host.

    Custom format (-Fc), not plain SQL. It compresses, it restores selectively,
    and pg_restore can list its contents without applying them — which matters
    when you are trying to work out whether a dump is any good at 3am.

    The dump contains embeddings. That is the expensive part of this corpus:
    restoring does NOT re-embed, which turns an hour of API calls into a
    two-second restore. See RUNBOOK.md for what recovery actually costs.

.PARAMETER OutputDir
    Where to write. Defaults to infra/backup/dumps/, which is gitignored —
    a dump holds every tenant's documents and encrypted credentials.

.PARAMETER Container
    The Postgres container name. Defaults to eaip-postgres.

.PARAMETER KeepLast
    Delete older dumps, keeping this many. 0 keeps everything.

.EXAMPLE
    .\backup.ps1
    .\backup.ps1 -KeepLast 7
#>
[CmdletBinding()]
param(
    [string]$OutputDir = "",
    [string]$Container = "eaip-postgres",
    [int]$KeepLast = 0
)

$ErrorActionPreference = "Stop"

# Resolved here rather than in the param block: $PSScriptRoot is not populated
# when parameter defaults are evaluated on Windows PowerShell 5.1.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutputDir) { $OutputDir = Join-Path $scriptDir "dumps" }

# Read connection details from .env rather than taking them as parameters.
# A backup script that needs its own copy of the credentials is a second place
# for them to go stale — and a second place for them to leak.
$envFile = Join-Path $scriptDir "..\..\.env"
if (-not (Test-Path $envFile)) {
    throw "No .env found at $envFile. Copy .env.example and fill it in."
}

$settings = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
        $settings[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}

$dbUser = $settings["POSTGRES_USER"]
$dbName = $settings["POSTGRES_DB"]
$dbPass = $settings["POSTGRES_PASSWORD"]

if (-not $dbUser -or -not $dbName -or -not $dbPass) {
    throw "POSTGRES_USER, POSTGRES_DB, and POSTGRES_PASSWORD must all be set in .env"
}

# Fail early and clearly if the container is not up, rather than producing a
# zero-byte dump that looks like a file until someone tries to restore it.
$running = docker ps --filter "name=$Container" --filter "status=running" --format "{{.Names}}"
if ($running -ne $Container) {
    throw "Container '$Container' is not running. Start it with: docker compose up -d"
}

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dumpFile = Join-Path $OutputDir "$dbName-$stamp.dump"

Write-Host "Dumping $dbName from $Container..."

# PGPASSWORD is passed via -e so it never appears in the container's process
# list, where `docker exec ps` would show it to anyone who can run docker.
# cmd /c handles the binary redirect. PowerShell's own pipeline decodes
# bytes as text on 5.1 and corrupts the archive; -Encoding Byte is 5.1-only
# and was removed in 7. Going through cmd works identically on both.
$dumpArgs = "exec -e PGPASSWORD=$dbPass $Container pg_dump -U $dbUser -d $dbName -Fc --no-owner --no-privileges"
cmd /c "docker $dumpArgs > `"$dumpFile`"" 

if ($LASTEXITCODE -ne 0) {
    # Remove the partial file. A truncated dump that looks like a backup is
    # worse than no backup, because it is only discovered during a restore.
    if (Test-Path $dumpFile) { Remove-Item $dumpFile -Force }
    throw "pg_dump failed with exit code $LASTEXITCODE"
}

$size = (Get-Item $dumpFile).Length
if ($size -lt 1024) {
    Remove-Item $dumpFile -Force
    throw "Dump was only $size bytes — that is not a real backup. Check the logs above."
}

# Verify the dump is READABLE, not merely present. pg_restore --list parses the
# archive's table of contents without applying anything, which catches a
# truncated or corrupt file here rather than during a recovery.
$listing = cmd /c "docker exec -i $Container pg_restore --list < `"$dumpFile`"" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "The dump was written but pg_restore could not read it. It is not a valid archive."
}

$objectCount = ($listing | Where-Object { $_ -notmatch '^;' }).Count
Write-Host "Wrote $dumpFile"
Write-Host ("  {0:N1} MB, {1} objects, verified readable" -f ($size / 1MB), $objectCount)

if ($KeepLast -gt 0) {
    $old = Get-ChildItem $OutputDir -Filter "$dbName-*.dump" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $KeepLast
    foreach ($file in $old) {
        Remove-Item $file.FullName -Force
        Write-Host "  removed old dump: $($file.Name)"
    }
}

Write-Host ""
$restoreTarget = $dbName + "_restored"
Write-Host "Restore with:"
Write-Host "  .\restore.ps1 -DumpFile '$dumpFile' -Database $restoreTarget"
