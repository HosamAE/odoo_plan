@echo off
title Build knowledge graphs (all projects)
rem Builds and publishes the odoo-kg graph of every wired project. Output: build_graphs.log
set PY=C:\Users\COMPUMARTS\AppData\Local\Programs\Python\Python312\python.exe
set LOG=C:\odoo\odoo_ai_cli\build_graphs.log
echo Build started %DATE% %TIME% > %LOG%
for %%P in (
  ProjectsOdoo20\Acme
  ProjectsOdoo19\Acme ProjectsOdoo19\sample_engine ProjectsOdoo19\sample_helper
  ProjectsOdoo18\SampleProject18 ProjectsOdoo18\sample_engine ProjectsOdoo18\sample_helper
  ProjectsOdoo17\core ProjectsOdoo17\acme ProjectsOdoo17\sample_shared ProjectsOdoo17\clientb ProjectsOdoo17\fo ProjectsOdoo17\hc ProjectsOdoo17\clientc ProjectsOdoo17\km ProjectsOdoo17\sw ProjectsOdoo17\sample_engine ProjectsOdoo17\sample_helper
) do (
  echo. >> %LOG%
  echo ===== %%P >> %LOG%
  %PY% C:\odoo\odoo_ai_cli\cli.py kg index -p C:\odoo\%%P >> %LOG% 2>&1
  if errorlevel 1 (echo EXIT %%P: FAILED >> %LOG%) else (echo EXIT %%P: OK >> %LOG%)
)
echo BUILD-DONE >> %LOG%
type %LOG% | findstr /C:"=====" /C:"EXIT" /C:"published" /C:"FAIL" /C:"BUILD-DONE"
echo.
pause
