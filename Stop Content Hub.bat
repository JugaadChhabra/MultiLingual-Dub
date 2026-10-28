@echo off
REM Double-click this file to stop Content Hub.

cd /d "%~dp0"

echo Stopping Content Hub...
docker compose down
if errorlevel 1 (
  echo.
  echo Content Hub may still be running -- Docker reported a problem above.
  echo Read that output, or check Docker Desktop.
  echo.
  pause
  exit /b 1
)

echo.
echo Content Hub has been stopped.
pause
exit /b 0
