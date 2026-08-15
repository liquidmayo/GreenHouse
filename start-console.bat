@echo off
title GreenHouse Monitor - Master Dashboard (console)
cd /d "%~dp0"
REM Foreground variant: keeps a console window with live log output.
REM Use start.bat for normal (windowless) operation.

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

".venv\Scripts\python.exe" -m ghmon.main --config monitors.yml --mode master
pause
