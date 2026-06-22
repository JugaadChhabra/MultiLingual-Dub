@echo off
REM Double-click this file to stop AutoDub.

cd /d "%~dp0"

echo Stopping AutoDub...
docker compose down
echo.
echo AutoDub has been stopped.
pause
