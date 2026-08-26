@echo off
REM Double-click this file to stop AutoDub.

cd /d "%~dp0"

echo Stopping AutoDub...
docker compose down
if errorlevel 1 (
  echo.
  echo AutoDub may still be running -- Docker reported a problem above.
  echo Read that output, or check Docker Desktop.
  echo.
  pause
  exit /b 1
)

echo.
echo AutoDub has been stopped.
pause
exit /b 0
