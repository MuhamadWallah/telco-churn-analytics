<#
  backup_neon.ps1 - save a full copy (a "backup") of the cloud warehouse on Neon
  to a file on your PC.
  Owners & Developers: MuhammaD_Wallah and the GROUP are the owners and Developers of this model.

  Why: Neon's free plan can only rewind the database 6 hours back, so a mistake
  noticed the next day cannot be undone there. This file can bring everything
  back, including the history in dw.model_runs that the CSV cannot rebuild.

  Run it from PowerShell in the project folder (once a month, after the
  monthly job, and before any risky change):
      powershell -ExecutionPolicy Bypass -File .\backup_neon.ps1

  What it does:
      1. reads DATABASE_URL_UNPOOLED from .env.local (written by `neon deploy`)
      2. runs pg_dump  ->  backups\telco_churn_dw_<date>_<time>.dump
         (custom format: compressed, and pg_restore can restore all or part of it)
      3. checks the file with pg_restore --list (it must contain table data)
      4. keeps the newest 6 backups in backups\ and deletes older ones
  backups\ is listed in .gitignore: the file holds all the data, so it is never
  committed. Keep an extra copy somewhere safe (USB stick, Google Drive).

  Options:
      -DatabaseUrl "postgresql://..."   use this connection string instead of .env.local
      -Keep 12                          how many backups to keep (default 6)

  How to restore - see docs/MAINTENANCE.md, section "Backups and restore".
#>
param(
    [string]$DatabaseUrl = "",
    [int]$Keep = 6
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --- 1. Connection string: DIRECT (unpooled), as Neon recommends for pg_dump ---
if (-not $DatabaseUrl) {
    foreach ($envFile in ".env.local", ".env") {
        if (Test-Path $envFile) {
            $line = Select-String -Path $envFile -Pattern '^\s*DATABASE_URL_UNPOOLED\s*=\s*"?([^"\s]+)"?' |
                    Select-Object -First 1
            if ($line) { $DatabaseUrl = $line.Matches[0].Groups[1].Value; break }
        }
    }
}
if (-not $DatabaseUrl) {
    throw "No connection string. Run 'neon deploy' (writes .env.local) or pass -DatabaseUrl."
}
$uri = [Uri]$DatabaseUrl
$database = [Uri]::UnescapeDataString($uri.AbsolutePath.TrimStart("/"))
Write-Host "Database: $database on $($uri.Host)"

# --- 2. Find pg_dump / pg_restore: on the PATH, or in the PostgreSQL folder ---
function Find-PgTool([string]$Name) {
    $tool = (Get-Command $Name -ErrorAction SilentlyContinue).Source
    if (-not $tool) {
        $tool = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\$Name.exe" -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $tool) { throw "$Name.exe was not found. Install PostgreSQL with 'Command Line Tools'." }
    return $tool
}
$pgDump = Find-PgTool "pg_dump"
$pgRestore = Find-PgTool "pg_restore"
Write-Host "Using $pgDump"

# --- 3. Make the backup -----------------------------------------------------
New-Item -ItemType Directory -Force -Path "backups" | Out-Null
$file = "backups\${database}_$(Get-Date -Format 'yyyy-MM-dd_HHmm').dump"
$ErrorActionPreference = "Continue"       # pg_dump messages on stderr are judged by the exit code
& $pgDump --dbname=$DatabaseUrl --format=custom --compress=6 --file=$file
$dumpExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($dumpExit -ne 0) {
    if (Test-Path $file) { Remove-Item $file }
    throw "pg_dump failed (exit code $dumpExit). Is Neon awake and is the connection string correct?"
}

# --- 4. Check the file: it must list the warehouse tables and their data -------
$contents = & $pgRestore --list $file
if ($LASTEXITCODE -ne 0) { throw "pg_restore could not read $file - the backup is damaged." }
$tables = @($contents | Select-String -Pattern ' TABLE DATA ').Count
$views = @($contents | Select-String -Pattern ' MATERIALIZED VIEW DATA ').Count
if ($tables -eq 0) { throw "$file contains no table data - do not rely on it." }
$sizeMb = [math]::Round((Get-Item $file).Length / 1MB, 2)
Write-Host "Saved $file ($sizeMb MB): $tables tables with data, $views materialized views." -ForegroundColor Green

# --- 5. Keep only the newest $Keep backups ------------------------------------
$old = Get-ChildItem "backups\${database}_*.dump" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
foreach ($f in $old) {
    Remove-Item $f.FullName
    Write-Host "Deleted old backup $($f.Name)"
}
Write-Host "Backups kept in backups\: $(@(Get-ChildItem "backups\${database}_*.dump").Count) (newest $Keep)."
Write-Host "Tip: copy the newest file to a USB stick or Google Drive too."
