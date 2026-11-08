@echo off
title Build knowledge graphs (all projects)
rem Builds and publishes the odoo-kg graph of every wired project, each with the
rem tools of its own Odoo version (odoo-ai picks them). Log: %TEMP%\odoo-ai-build-graphs.log
set LOG=%TEMP%\odoo-ai-build-graphs.log
echo Build started %DATE% %TIME% > "%LOG%"
for /d %%V in (C:\odoo\odoo*) do (
  if exist "%%V\projects" for /d %%P in ("%%V\projects\*") do (
    if exist "%%P\.mcp.json" (
      echo. >> "%LOG%"
      echo ===== %%P >> "%LOG%"
      call "%~dp0odoo-ai.bat" kg index -p "%%P" >> "%LOG%" 2>&1
      if errorlevel 1 (echo EXIT %%P: FAILED >> "%LOG%") else (echo EXIT %%P: OK >> "%LOG%")
    )
  )
)
echo BUILD-DONE >> "%LOG%"
type "%LOG%" | findstr /C:"=====" /C:"EXIT" /C:"published" /C:"FAIL" /C:"BUILD-DONE"
echo.
pause
