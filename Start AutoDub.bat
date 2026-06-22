@echo off
REM Double-click this file to start AutoDub. It will always fetch the latest
REM version automatically, then open the app in your browser.

cd /d "%~dp0"

echo ============================
echo    Starting AutoDub...
echo ============================
echo.

REM Make sure Docker Desktop is running before we try anything.
docker info >nul 2>&1
if errorlevel 1 (
  echo Docker is not running.
  echo Please open Docker Desktop, wait until it says it's running,
  echo then double-click this file again.
  echo.
  pause
  exit /b 1
)

REM Start (and auto-update) the app.
docker compose up -d
if errorlevel 1 (
  echo.
  echo Something went wrong while starting AutoDub.
  echo Make sure the .env file is filled in and try again.
  pause
  exit /b 1
)

echo.
echo Getting AutoDub ready...
set /a tries=0
:waitloop
curl -fs http://localhost:8080/health >nul 2>&1
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% geq 60 goto ready
timeout /t 1 /nobreak >nul
goto waitloop

:ready
echo.
echo AutoDub is running!  --^>  http://localhost:8080
echo Opening it in your browser now...
start "" http://localhost:8080

echo.
echo You can close this window. AutoDub keeps running in the background.
pause
