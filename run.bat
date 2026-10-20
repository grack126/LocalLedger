@echo off
rem ---------------------------------------------------------------------------
rem LocalLedger OCR - one-click launcher for Windows
rem   First run: creates .venv, installs dependencies, generates sample data.
rem   Later runs: starts straight away. Extra arguments go to launcher.py
rem   (e.g.  run.bat --port 8600   or   run.bat --reinstall).
rem ---------------------------------------------------------------------------
setlocal EnableExtensions EnableDelayedExpansion
title LocalLedger OCR
cd /d "%~dp0"

set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" goto run

echo.
echo   LocalLedger OCR - first-time setup
echo   ----------------------------------
set "PYCMD="
for %%C in ("py -3.13" "py -3.12" "py -3.11" "py -3" "python" "python3") do (
    if not defined PYCMD (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if !errorlevel! equ 0 set "PYCMD=%%~C"
    )
)

if defined PYCMD (
    echo   Using Python: !PYCMD!
    !PYCMD! -m venv .venv
    if errorlevel 1 goto venv_failed
    goto run
)

where uv >nul 2>&1
if !errorlevel! equ 0 (
    echo   No Python 3.11+ on PATH - setting one up with uv ...
    uv venv --seed --python ">=3.11" .venv
    if errorlevel 1 goto venv_failed
    goto run
)

echo.
echo   Python 3.11 or newer is required.
echo   Download it from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" in the installer,
echo   then double-click run.bat again.
echo.
pause
exit /b 1

:venv_failed
echo.
echo   Could not create the virtual environment in .venv
echo   Delete the .venv folder if it exists and try again.
pause
exit /b 1

:run
"%VPY%" launcher.py %*
set "CODE=!errorlevel!"
if not "!CODE!"=="0" (
    echo.
    echo   LocalLedger OCR stopped with an error ^(code !CODE!^). See the messages above.
    pause
)
exit /b !CODE!
