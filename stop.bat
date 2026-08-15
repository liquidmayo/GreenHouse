@echo off
title GreenHouse Monitor - Stop
cd /d "%~dp0"
echo Stopping GreenHouse Monitor processes started from this folder...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'ghmon\.main' } | ForEach-Object { Write-Host ('  stopping PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Done.
ping -n 2 127.0.0.1 >nul
