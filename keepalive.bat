@echo off
rem Monitor keep-alive: if the dashboard port stopped listening, start it.
rem Register as a 5-minute scheduled task running: wscript.exe //B keepalive.vbs
rem (hidden, no console flash). Match the port below to server: port in
rem monitors.yml. Does nothing while the dashboard is up.
netstat -an | findstr /C:":8090 " | findstr LISTENING >nul
if %errorlevel%==0 exit /b 0
echo %date% %time% port 8090 not listening - restarting >> "%~dp0data\keepalive.log"
call "%~dp0start.bat"
