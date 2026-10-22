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

  The dashboard's security script (sql/09_dashboard_access.sql) is NOT run here,
  because it asks for three new passwords; run it separately afterwards.
#>
param(
    [int]$Port = 5432,
    [string]$HostName = "localhost",
    [string]$User = "postgres",
    [switch]$SkipMining
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot          # relative paths (data/..., sql/...) start here

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
$passwordSetHere = $false
if (-not $env:PGPASSWORD) {
    $secure = Read-Host "Password for PostgreSQL user '$User'" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $passwordSetHere = $true
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
    # --- 3. Build the warehouse (01 connects to the default 'postgres' database) ---
    Invoke-SqlScript "postgres" "sql/01_create_database_and_schemas.sql"
    foreach ($name in "02_staging_load", "03_star_schema_ddl", "04_etl_procedure", "05_validation_checks",
                      "06_olap_queries", "07_materialized_views", "08_mining_objects") {
        Invoke-SqlScript "telco_churn_dw" "sql/$name.sql"
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
    Write-Host "Done. Next (optional): dashboard security, then the dashboard:" -ForegroundColor Green
    Write-Host "  & `"$psql`" -U $User -h $HostName -p $Port -d telco_churn_dw -f sql/09_dashboard_access.sql"
    Write-Host "  python -m streamlit run dashboard/app.py"
}
finally {
    if ($passwordSetHere) { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
}
