# Bootstrap a local Postgres for NexoCrypto.
# Run once. Idempotent — safe to re-run.
#
#   .\examples\bootstrap_db.ps1
#
# Adds the scoop Postgres bin to PATH for the current shell session, makes sure
# the server is running, drops + recreates the `nexocrypto` DB, then applies
# the auth shim + every migration in order.

$ErrorActionPreference = "Stop"
$pgBin  = "$env:USERPROFILE\scoop\apps\postgresql\current\bin"
$pgData = "$env:USERPROFILE\scoop\apps\postgresql\current\data"
$pgLog  = "$env:USERPROFILE\scoop\apps\postgresql\current\pg.log"

$env:PATH = "$pgBin;$env:PATH"

# 1. Make sure the server is up.
$ready = & "$pgBin\pg_isready.exe" -U postgres -h 127.0.0.1 -p 5432 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Host "Postgres not running — starting..."
  & "$pgBin\pg_ctl.exe" -D $pgData -l $pgLog start | Out-Null
  Start-Sleep -Seconds 2
}

# 2. Drop + recreate the DB so re-runs work.
Write-Host "Dropping/creating database 'nexocrypto'..."
& "$pgBin\psql.exe" -U postgres -d postgres -c "drop database if exists nexocrypto" | Out-Null
& "$pgBin\psql.exe" -U postgres -d postgres -c "create database nexocrypto" | Out-Null

# 3. Apply auth shim (Supabase's auth.users + auth.uid() for local Postgres).
Write-Host "Applying auth shim..."
& "$pgBin\psql.exe" -U postgres -d nexocrypto -f "supabase\test_auth_shim.sql" | Out-Null

# 4. Apply every migration in sorted order.
$migrations = Get-ChildItem "supabase\migrations\*.sql" | Sort-Object Name
foreach ($m in $migrations) {
  Write-Host "Applying $($m.Name)..."
  & "$pgBin\psql.exe" -U postgres -d nexocrypto -f $m.FullName | Out-Null
}

Write-Host ""
Write-Host "Database ready:" -ForegroundColor Green
Write-Host "  postgresql://postgres@127.0.0.1:5432/nexocrypto"
Write-Host ""
Write-Host "Next:"
Write-Host "  `$env:NEXOCRYPTO_STORE = 'pg'"
Write-Host "  `$env:NEXOCRYPTO_DATABASE_URL = 'postgresql://postgres@127.0.0.1:5432/nexocrypto'"
Write-Host "  .venv\Scripts\python.exe examples\run_api.py"
