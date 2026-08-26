@echo off
REM Double-click this file to start AutoDub. It will always fetch the latest
REM version automatically, then open the app in your browser.
REM
REM This pulls a fresh image on every run, which makes Docker consult its
REM credential helper. That helper needs an interactive desktop session, so
REM running this over SSH or Remote PowerShell fails with "A specified logon
REM session does not exist". Run it at the machine, or start whichever image
REM is already on disk with:  docker compose up -d --pull never

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

REM Start the app, pulling a newer image if one has been published.
docker compose up -d
if errorlevel 1 (
  echo.
  echo AutoDub did not start.
  echo.
  echo The reason is in the Docker output above -- read that first, it names
  echo the actual cause. Two that come up often:
  echo.
  echo   credentials / logon session  you are running this over SSH or Remote
  echo                                Desktop. Run it at the machine instead.
  echo   missing or empty variable    check the .env file next to this script.
  echo.
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
if %tries% geq 60 goto timedout
timeout /t 1 /nobreak >nul
goto waitloop

:timedout
echo.
echo The container started, but AutoDub never answered on
echo http://localhost:8080 after 60 seconds.
echo.
echo The last few log lines:
echo.
docker compose logs --tail 25
echo.
echo AutoDub is NOT ready. Fix what the log points at, then run this again.
pause
exit /b 1

:ready
echo.
echo AutoDub is running!  --^>  http://localhost:8080
echo Opening it in your browser now...
start "" http://localhost:8080

echo.
echo You can close this window. AutoDub keeps running in the background.
pause
exit /b 0
