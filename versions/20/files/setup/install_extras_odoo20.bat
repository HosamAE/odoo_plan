@echo off
title Odoo 20 venv extras
rem websocket-client: without it every HttpCase tour is "skipped" (Acme CLAUDE.md). markdown2: mail markdown rendering.
C:\odoo\odoo20\venv\Scripts\python.exe -m pip install websocket-client markdown2 > C:\odoo\odoo20\tools\setup\install_extras_odoo20.log 2>&1
echo EXTRAS-EXIT: %ERRORLEVEL%>> C:\odoo\odoo20\tools\setup\install_extras_odoo20.log
type C:\odoo\odoo20\tools\setup\install_extras_odoo20.log
echo.
pause
