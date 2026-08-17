@echo off
title GreenHouse Monitor - Companion Agent
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo Starting GreenHouse Monitor companion agent in the background (no console)...
echo Log: %~dp0data\agent.log
echo Stop it with stop.bat
if not exist "data" mkdir data
start "" ".venv\Scripts\pythonw.exe" -m ghmon.main --config monitors.yml --mode agent --log data\agent.log
ping -n 3 127.0.0.1 >nul
