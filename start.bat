@echo off
title GreenHouse Monitor - Master Dashboard
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo Starting GreenHouse Monitor master dashboard...
".venv\Scripts\python.exe" -m ghmon.main --config monitors.yml --mode master
pause
