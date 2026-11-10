# Creates (or updates) the PostgreSQL role odoo20 with the db_password from odoo.conf.
# psql asks for the password of the "postgres" superuser.
Start-Transcript -Path "C:\odoo\odoo20\tools\setup\create_db_role_odoo20.log" -Force | Out-Null
$conf = "C:\odoo\odoo20\odoo.conf"
$psql = "C:\Program Files\PostgreSQL\16\bin\psql.exe"
Write-Host "== Creating PostgreSQL role odoo20 =="
if (-not (Test-Path $psql)) { Write-Host "psql not found at $psql" -ForegroundColor Red; Stop-Transcript | Out-Null; exit 1 }
$pw = (Select-String -Path $conf -Pattern '^db_password\s*=\s*(.+)$').Matches[0].Groups[1].Value.Trim()
$pwSql = $pw.Replace("'", "''")
$sql = "DO `$`$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='odoo20') THEN CREATE ROLE odoo20 LOGIN CREATEDB PASSWORD '$pwSql'; ELSE ALTER ROLE odoo20 LOGIN CREATEDB PASSWORD '$pwSql'; END IF; END`$`$;"
Write-Host "Type the password of the PostgreSQL user 'postgres' when asked:"
$check = "SELECT 'ROLE-CHECK: ' || rolname || ' login=' || rolcanlogin || ' createdb=' || rolcreatedb FROM pg_roles WHERE rolname='odoo20'"
& $psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 -tA -c $sql -c $check
if ($LASTEXITCODE -eq 0) {
    Write-Host "RESULT: role odoo20 is ready" -ForegroundColor Green
} else {
    Write-Host "RESULT: failed (exit $LASTEXITCODE)" -ForegroundColor Red
}
Stop-Transcript | Out-Null
