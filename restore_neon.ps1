<#
  restore_neon.ps1 - put a backup made by backup_neon.ps1 back into a database.
  Owners & Developers: MuhammaD_Wallah and the GROUP are the owners and Developers of this model.

  Run it from PowerShell in the project folder.

  Practice / test (recommended first - your live database is not touched):
      neon branches create --name restore-test --parent production
      neon databases create --branch restore-test --name restore_check --owner-name neondb_owner
      $test = neon connection-string restore-test --database-name restore_check
      powershell -ExecutionPolicy Bypass -File .\restore_neon.ps1 -DatabaseUrl $test
      ... check it, then:   neon branches delete restore-test

  Real emergency (the live warehouse was damaged and Neon's own 6-hour restore
  is too late): restore the newest backup over the LIVE database:
      powershell -ExecutionPolicy Bypass -File .\restore_neon.ps1
  Without -DatabaseUrl it uses DATABASE_URL_UNPOOLED from .env.local = production.

  What it does:
      1. picks the backup: -File, or the newest backups\telco_churn_dw_*.dump
      2. shows the target and asks you to type its database name (-Yes skips this)
      3. pg_restore --clean --if-exists --single-transaction:
         objects in the backup are dropped and re-created with the backup's data,
         all in ONE transaction - if anything fails, NOTHING is changed
      4. skips two entries that belong to Neon's internal admin role cloud_admin
         (Neon adds them to every database; restoring them is not allowed)

  Options:
      -File backups\telco_churn_dw_2026-10-23_0641.dump   restore this backup
      -DatabaseUrl "postgresql://..."                    restore into this database
      -NoAcl     skip GRANTs (use it for a NEW Neon project where the role
                 churn_app_reader does not exist yet; afterwards run
                 sql/11_app_reader_role.sql to create the role and its rights)
      -Yes       do not ask for confirmation
#>
param(
    [string]$File = "",
    [string]$DatabaseUrl = "",
    [switch]$NoAcl,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --- 1. Which backup ---------------------------------------------------------
if (-not $File) {
    $newest = Get-ChildItem "backups\telco_churn_dw_*.dump" -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { throw "No backup found in backups\. Run backup_neon.ps1 first, or pass -File." }
    $File = $newest.FullName
}
if (-not (Test-Path $File)) { throw "Backup file not found: $File" }

# --- 2. Which database (default: production, from .env.local) -----------------
$target = "the database in -DatabaseUrl"
if (-not $DatabaseUrl) {
    foreach ($envFile in ".env.local", ".env") {
        if (Test-Path $envFile) {
            $line = Select-String -Path $envFile -Pattern '^\s*DATABASE_URL_UNPOOLED\s*=\s*"?([^"\s]+)"?' |
                    Select-Object -First 1
            if ($line) { $DatabaseUrl = $line.Matches[0].Groups[1].Value; $target = "PRODUCTION (from $envFile)"; break }
        }
    }
}
if (-not $DatabaseUrl) { throw "No target database. Pass -DatabaseUrl, or run 'neon deploy' to write .env.local." }
$uri = [Uri]$DatabaseUrl
$database = [Uri]::UnescapeDataString($uri.AbsolutePath.TrimStart("/"))

Write-Host ""
Write-Host "Backup : $File"
Write-Host "Target : database '$database' on $($uri.Host)  - $target" -ForegroundColor Yellow
Write-Host "Every object in the backup is replaced by the backup's version (all or nothing)."
if (-not $Yes) {
    $answer = Read-Host "Type the database name ($database) to continue, or press Enter to stop"
    if ($answer -ne $database) { Write-Host "Stopped. Nothing was changed."; exit 1 }
}

# --- 3. Find pg_restore --------------------------------------------------------
$pgRestore = (Get-Command pg_restore -ErrorAction SilentlyContinue).Source
if (-not $pgRestore) {
    $pgRestore = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_restore.exe" -ErrorAction SilentlyContinue |
                 Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $pgRestore) { throw "pg_restore.exe was not found. Install PostgreSQL with 'Command Line Tools'." }

# --- 4. The list of entries to restore, without Neon's cloud_admin entries ----
$listFile = [IO.Path]::GetTempFileName()
try {
    $entries = & $pgRestore --list $File
    if ($LASTEXITCODE -ne 0) { throw "pg_restore could not read $File - the backup is damaged." }
    $skipped = @($entries | Where-Object { $_ -match '\scloud_admin\s*$' }).Count
    $entries | Where-Object { $_ -notmatch '\scloud_admin\s*$' } | Set-Content -Path $listFile -Encoding ascii
    Write-Host "Skipping $skipped Neon-internal entries (role cloud_admin)."

    $restoreArgs = @("--dbname=$DatabaseUrl", "--use-list=$listFile", "--clean", "--if-exists",
                     "--no-owner", "--single-transaction")
    if ($NoAcl) { $restoreArgs += "--no-acl" }

    # --- 5. Restore -------------------------------------------------------------
    $ErrorActionPreference = "Continue"   # messages on stderr are judged by the exit code
    & $pgRestore @restoreArgs $File
    $restoreExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($restoreExit -ne 0) {
        throw "pg_restore failed (exit code $restoreExit). Because of --single-transaction NOTHING was changed. Read the error above."
    }
}
finally {
    Remove-Item $listFile -ErrorAction SilentlyContinue
}
Write-Host "Restored $File into '$database'." -ForegroundColor Green
Write-Host "Next: python model/score.py  (optional - re-score with the current model), then open the app."
