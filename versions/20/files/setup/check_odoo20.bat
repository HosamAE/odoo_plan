@echo off
title Odoo 20 health check
cd /d C:\odoo\odoo20
C:\odoo\odoo20\venv\Scripts\python.exe C:\odoo\odoo20\tools\setup\check_odoo20.py > C:\odoo\odoo20\tools\setup\check_odoo20.log 2>&1
type C:\odoo\odoo20\tools\setup\check_odoo20.log
echo.
pause
