@echo off
rem Double-click to set up Odoo 20. Output is also written to setup_odoo20.log.
cd /d C:\odoo\odoo20
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { & 'C:\odoo\odoo20\tools\setup\setup_odoo20.ps1' *>&1 | Out-String -Stream | Tee-Object -FilePath 'C:\odoo\odoo20\tools\setup\setup_odoo20.log'; Add-Content 'C:\odoo\odoo20\tools\setup\setup_odoo20.log' 'SETUP-EXIT: done' } catch { Add-Content 'C:\odoo\odoo20\tools\setup\setup_odoo20.log' ('SETUP-EXIT: failed - ' + $_); Write-Host $_ -ForegroundColor Red }"
echo.
pause
