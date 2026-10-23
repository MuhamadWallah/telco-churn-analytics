<#
  build_warehouse.ps1 - build the whole data warehouse and run the data mining.
  Owners & Developers: MuhammaD_Wallah and the GROUP are the owners and Developers of this model.

  Runs, in order:
      sql/01 ... sql/08   (database, staging load, star schema, ETL, validation,
                           OLAP queries, materialized views, mining objects)
      mining/churn_mining.py
  It asks for the postgres password ONCE (hidden), stops at the first error,
  and can be run again at any time (every script is re-runnable).

  Run it from PowerShell in the project folder:
      powershell -ExecutionPolicy Bypass -File .\build_warehouse.ps1

  Options:
      -Port 5433        if your PostgreSQL uses another port (default 5432)
      -User postgres    database superuser (default postgres)
      -SkipMining       build the warehouse only
      -DatabaseUrl "postgresql://USER:PASSWORD@HOST/telco_churn_dw?sslmode=require"
                        use ONE connection string instead of -HostName/-Port/-User,
                        e.g. the cloud database on Neon (the database must exist already)
      -Cloud            also run sql/10 (tables for the web app) and sql/12 (validation
                        + refresh of the materialized views). Without -DatabaseUrl it
                        uses DATABASE_URL_UNPOOLED from .env.local (written by `neon deploy`)

  Cloud warehouse on Neon (see README, "Free Cloud Deployment"):
      powershell -ExecutionPolicy Bypass -File .\build_warehouse.ps1 -Cloud -SkipMining

  The dashboard's security script (sql/09_dashboard_access.sql) is NOT run here,
  because it asks for three new passwords; run it separately afterwards.
#>
param(
    [int]$Port = 5432,
    [string]$HostName = "localhost",
    [string]$User = "postgres",
    [string]$DatabaseUrl = "",
    [switch]$Cloud,
    [switch]$SkipMining
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot          # relative paths (data/..., sql/...) start here

# --- 0. Optional: one connection string (e.g. from Neon) ---------------------
# Split it into host, port, user, password, database and SSL settings. psql reads
# the password and the SSL settings from environment variables (PGPASSWORD,
# PGSSLMODE, PGCHANNELBINDING) that exist only while this script runs.
$startDatabase = "postgres"               # script 01 first connects here (Neon has no 'postgres' database)
$envSetHere = @()
$envBefore = @{}                          # values to put back at the end
foreach ($name in "PGPASSWORD", "PGSSLMODE", "PGCHANNELBINDING") {
    $envBefore[$name] = [Environment]::GetEnvironmentVariable($name)
}
# -Cloud without -DatabaseUrl: the DIRECT (unpooled) string that `neon deploy` wrote.
# Neon recommends a direct connection for schema changes like these scripts.
if ($Cloud -and -not $DatabaseUrl) {
    foreach ($envFile in ".env.local", ".env") {
        if (Test-Path $envFile) {
            $line = Select-String -Path $envFile -Pattern '^\s*DATABASE_URL_UNPOOLED\s*=\s*"?([^"\s]+)"?' |
                    Select-Object -First 1
            if ($line) {
                $DatabaseUrl = $line.Matches[0].Groups[1].Value
                Write-Host "Using DATABASE_URL_UNPOOLED from $envFile"
                break
            }
        }
    }
    if (-not $DatabaseUrl) { throw "-Cloud needs -DatabaseUrl, or run 'neon deploy' first to write .env.local." }
}
if ($DatabaseUrl) {
    $uri = [Uri]$DatabaseUrl
    if ($uri.Scheme -notin "postgresql", "postgres") {
        throw "-DatabaseUrl must start with postgresql:// (copy it from Neon > Connect)."
    }
    $HostName = $uri.Host
    if ($uri.Port -gt 0) { $Port = $uri.Port }
    $userInfo = $uri.UserInfo.Split(":", 2)
    $User = [Uri]::UnescapeDataString($userInfo[0])
    if ($userInfo.Count -eq 2) {
        $env:PGPASSWORD = [Uri]::UnescapeDataString($userInfo[1])
        $envSetHere += "PGPASSWORD"
    }
    $database = $uri.AbsolutePath.TrimStart("/")
    if ($database) { $startDatabase = [Uri]::UnescapeDataString($database) }
    foreach ($setting in @{ sslmode = "PGSSLMODE"; channel_binding = "PGCHANNELBINDING" }.GetEnumerator()) {
        if ($uri.Query -match "[?&]$($setting.Key)=([^&]+)") {
            Set-Item -Path "Env:$($setting.Value)" -Value $Matches[1]
            $envSetHere += $setting.Value
        }
    }
}

# --- 1. Find psql: on the PATH, or in the standard installation folder -------
$psql = (Get-Command psql -ErrorAction SilentlyContinue).Source
if (-not $psql) {
    $psql = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\psql.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $psql) {
    throw "psql.exe was not found. Install PostgreSQL with 'Command Line Tools', or add its bin folder to PATH."
}
Write-Host "Using $psql (server ${HostName}:$Port, user $User)"

# --- 2. Ask for the password once (not shown, not saved) --------------------
if (-not $env:PGPASSWORD) {
    $secure = Read-Host "Password for PostgreSQL user '$User'" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $envSetHere += "PGPASSWORD"
}

function Invoke-SqlScript([string]$Database, [string]$File) {
    Write-Host ""
    Write-Host "=== $File ===" -ForegroundColor Cyan
    # psql writes NOTICE messages to stderr; Windows PowerShell 5.1 may show them as
    # "NativeCommandError". They are harmless: success is judged by the exit code only.
    # -P pager=off: print long result tables in full instead of pausing at "-- More --".
    $ErrorActionPreference = "Continue"
    & $psql -h $HostName -p $Port -U $User -d $Database -v ON_ERROR_STOP=1 -P pager=off -f $File
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed (psql exit code $LASTEXITCODE). Read the error above, fix it, and run this script again."
    }
}

try {
    # --- 3. Build the warehouse (01 connects to 'postgres', or to the database in -DatabaseUrl) ---
    Invoke-SqlScript $startDatabase "sql/01_create_database_and_schemas.sql"
    foreach ($name in "02_staging_load", "03_star_schema_ddl", "04_etl_procedure", "05_validation_checks",
                      "06_olap_queries", "07_materialized_views", "08_mining_objects") {
        Invoke-SqlScript "telco_churn_dw" "sql/$name.sql"
    }
    if ($Cloud) {
        foreach ($name in "10_cloud_ml_tables", "12_monthly_refresh") {
            Invoke-SqlScript "telco_churn_dw" "sql/$name.sql"
        }
    }
    Write-Host ""
    Write-Host "Warehouse built and validated." -ForegroundColor Green

    # --- 4. Data mining ----------------------------------------------------------
    if (-not $SkipMining) {
        Write-Host ""
        Write-Host "=== mining/churn_mining.py ===" -ForegroundColor Cyan
        $ErrorActionPreference = "Continue"
        python mining/churn_mining.py --host $HostName --port $Port --user $User
        if ($LASTEXITCODE -ne 0) { throw "churn_mining.py failed (exit code $LASTEXITCODE)." }
    }

    Write-Host ""
    if ($Cloud) {
        Write-Host "Done. Next: the read-only role for the web app:" -ForegroundColor Green
        Write-Host "  & `"$psql`" `"`$env:DATABASE_URL`" -f sql/11_app_reader_role.sql"
    } else {
        Write-Host "Done. Next (optional): dashboard security, then the dashboard:" -ForegroundColor Green
        Write-Host "  & `"$psql`" -U $User -h $HostName -p $Port -d telco_churn_dw -f sql/09_dashboard_access.sql"
        Write-Host "  python -m streamlit run dashboard/app.py"
    }
}
finally {
    foreach ($name in $envSetHere) { [Environment]::SetEnvironmentVariable($name, $envBefore[$name]) }
}
