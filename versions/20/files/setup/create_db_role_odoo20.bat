@echo off
title Create PostgreSQL role odoo20
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\odoo\odoo20\tools\setup\create_db_role_odoo20.ps1"
echo.
pause
