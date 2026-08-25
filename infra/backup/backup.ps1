<#
.SYNOPSIS
    Take a backup of the EAIP database, and of Keycloak's.

.DESCRIPTION
    Runs pg_dump INSIDE each Postgres container, so nothing needs a local
    Postgres client installed. The dumps are streamed out to the host.

    Custom format (-Fc), not plain SQL. It compresses, it restores selectively,
    and pg_restore can list its contents without applying them — which matters
    when you are trying to work out whether a dump is any good at 3am.

    The EAIP dump contains embeddings. That is the expensive part of this
    corpus: restoring does NOT re-embed, which turns an hour of API calls into
    a two-second restore. See RUNBOOK.md for what recovery actually costs.

    TWO DATABASES, because since Phase 11 there are two (ADR 0009). Keycloak
    holds the users, their password hashes, their tenant_id and labels, and the
    realm configuration. Restoring EAIP alone recovers a platform nobody can
    log into — the documents survive and every account is gone.

    The realm STRUCTURE is in git (infra/keycloak/realm-eaip.json) and would
    re-import. The users are not, and never should be: a realm file that ships
    accounts ships credentials.

.PARAMETER OutputDir
    Where to write. Defaults to infra/backup/dumps/, which is gitignored —
    a dump holds every tenant's documents and encrypted credentials, and the
    Keycloak dump holds password hashes.

.PARAMETER Container
    The EAIP Postgres container name. Defaults to eaip-postgres.

.PARAMETER KeycloakContainer
    The Keycloak Postgres container name. Defaults to eaip-keycloak-db.

.PARAMETER SkipKeycloak
    Back up EAIP only. For a deployment with no identity provider — not a
    convenience flag for one that has it.

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
    [string]$KeycloakContainer = "eaip-keycloak-db",
    [switch]$SkipKeycloak,
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

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Invoke-Dump {
    <#
        One database, dumped and verified. Written once so the Keycloak dump
        gets exactly the same checks as the EAIP one — a second copy of this
        logic is a second place for the verification to be left out.
    #>
    param(
        [string]$ContainerName,
        [string]$User,
        [string]$Password,
        [string]$Database,
        [string]$Label
    )

    # Fail early and clearly if the container is not up, rather than producing
    # a zero-byte dump that looks like a file until someone tries to restore it.
    $running = docker ps --filter "name=$ContainerName" --filter "status=running" --format "{{.Names}}"
    if ($running -ne $ContainerName) {
        throw "Container '$ContainerName' is not running. Start it with: docker compose up -d"
    }

    $file = Join-Path $OutputDir "$Database-$stamp.dump"
    Write-Host "Dumping $Label ($Database) from $ContainerName..."

    # PGPASSWORD is passed via -e so it never appears in the container's process
    # list, where `docker exec ps` would show it to anyone who can run docker.
    # cmd /c handles the binary redirect. PowerShell's own pipeline decodes
    # bytes as text on 5.1 and corrupts the archive; -Encoding Byte is 5.1-only
    # and was removed in 7. Going through cmd works identically on both.
    $dumpArgs = "exec -e PGPASSWORD=$Password $ContainerName pg_dump -U $User -d $Database -Fc --no-owner --no-privileges"
    cmd /c "docker $dumpArgs > `"$file`""

    if ($LASTEXITCODE -ne 0) {
        # Remove the partial file. A truncated dump that looks like a backup is
        # worse than no backup, because it is only discovered during a restore.
        if (Test-Path $file) { Remove-Item $file -Force }
        throw "pg_dump failed for $Label with exit code $LASTEXITCODE"
    }

    $size = (Get-Item $file).Length
    if ($size -lt 1024) {
        Remove-Item $file -Force
        throw "$Label dump was only $size bytes — that is not a real backup. Check the logs above."
    }

    # Verify the dump is READABLE, not merely present. pg_restore --list parses
    # the archive's table of contents without applying anything, which catches a
    # truncated or corrupt file here rather than during a recovery.
    $listing = cmd /c "docker exec -i $ContainerName pg_restore --list < `"$file`"" 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "The $Label dump was written but pg_restore could not read it. It is not a valid archive."
    }

    $objectCount = ($listing | Where-Object { $_ -notmatch '^;' }).Count
    Write-Host "  Wrote $file"
    Write-Host ("  {0:N1} MB, {1} objects, verified readable" -f ($size / 1MB), $objectCount)
    return $file
}

$eaipDump = Invoke-Dump -ContainerName $Container -User $dbUser -Password $dbPass `
    -Database $dbName -Label "EAIP"

$keycloakDump = $null
if ($SkipKeycloak) {
    Write-Host ""
    Write-Host "Skipping Keycloak (-SkipKeycloak)."
    Write-Host "  The EAIP dump alone restores documents and integrations, NOT accounts."
}
else {
    $kcPass = $settings["KEYCLOAK_DB_PASSWORD"]
    # Matches the docker-compose default. A backup that silently skipped
    # Keycloak because a variable was unset would be discovered during a
    # recovery, which is the worst possible moment.
    if (-not $kcPass) { $kcPass = "keycloak-dev-password" }

    $kcRunning = docker ps --filter "name=$KeycloakContainer" --filter "status=running" --format "{{.Names}}"
    if ($kcRunning -ne $KeycloakContainer) {
        throw @"
Container '$KeycloakContainer' is not running, so the accounts would not be
backed up. Start it with: docker compose up -d keycloak-db keycloak

If this deployment genuinely has no identity provider, pass -SkipKeycloak.
"@
    }
    Write-Host ""
    $keycloakDump = Invoke-Dump -ContainerName $KeycloakContainer -User "keycloak" `
        -Password $kcPass -Database "keycloak" -Label "Keycloak identity"
}

if ($KeepLast -gt 0) {
    foreach ($prefix in @($dbName, "keycloak")) {
        $old = Get-ChildItem $OutputDir -Filter "$prefix-*.dump" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -Skip $KeepLast
        foreach ($file in $old) {
            Remove-Item $file.FullName -Force
            Write-Host "  removed old dump: $($file.Name)"
        }
    }
}

Write-Host ""
Write-Host "Restore with:"
Write-Host "  .\restore.ps1 -DumpFile '$eaipDump' -Database $($dbName)_restored"
if ($keycloakDump) {
    Write-Host "  .\restore.ps1 -DumpFile '$keycloakDump' -Database keycloak_restored -Container $KeycloakContainer -User keycloak"
    Write-Host ""
    Write-Host "Both are needed. EAIP alone restores the data and none of the accounts."
}
