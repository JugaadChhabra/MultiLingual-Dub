#!/bin/bash
# One-shot setup for Content Hub on the shared office Mac mini (which also runs
# Midas). Safe to run again: it is also the way to update to a newer image.
#
# Put this script in one folder on the Mac mini together with:
#   docker-compose.yml, .env, Start Content Hub.command, Stop Content Hub.command
# then run:   bash setup-mac-mini.sh
#
# Content Hub only opens port 8080. Midas uses 8000, 8001, 3000, 6006 and
# 55432, so the two do not collide.

set -u

APP_PORT=8080
MIDAS_PORT=8000
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1

ok()   { echo "  [ok]   $*"; }
warn() { echo "  [warn] $*"; }
fail() { echo "  [FAIL] $*"; echo ""; echo "Setup stopped. Nothing else was changed."; exit 1; }
step() { echo ""; echo "== $* =="; }

echo "Content Hub setup  ($HERE)"

# ---------------------------------------------------------------------------
step "1. Files"
[ "$(uname)" = "Darwin" ] || fail "This script is for macOS."
[ -f docker-compose.yml ] || fail "docker-compose.yml is missing from $HERE."
[ -f .env ] || fail ".env is missing from $HERE. Copy it over from your laptop (it holds the API keys)."
ok "docker-compose.yml and .env found"

# A .env saved on Windows has CRLF line endings, which put a stray \r at the end
# of every key. Strip them.
if grep -q $'\r' .env; then
  sed -i '' $'s/\r$//' .env
  ok "removed Windows line endings from .env"
fi

chmod 600 .env
ok ".env is readable by this user only"

mkdir -p output
ok "output/ folder ready"

# Files copied over AirDrop, USB or a download are quarantined, and macOS then
# refuses to open them on double-click. Clear that and make them executable.
for f in "Start Content Hub.command" "Stop Content Hub.command"; do
  if [ -f "$f" ]; then
    xattr -d com.apple.quarantine "$f" 2>/dev/null
    chmod +x "$f"
    ok "\"$f\" can be double-clicked"
  else
    warn "\"$f\" not found: you will have to start and stop from Terminal"
  fi
done

# ---------------------------------------------------------------------------
step "2. Docker"
command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install Docker Desktop for Mac (Apple silicon) and run this again."

if ! docker info >/dev/null 2>&1; then
  echo "  Docker Desktop is not running, starting it..."
  open -a Docker 2>/dev/null || fail "Could not open Docker Desktop."
  for _ in $(seq 1 90); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
  docker info >/dev/null 2>&1 || fail "Docker Desktop did not come up within 3 minutes. Open it by hand, wait for it to say it is running, then run this again."
fi
ok "Docker is running"

docker compose version >/dev/null 2>&1 || fail "'docker compose' is not available. Update Docker Desktop."
ok "docker compose available"

# ---------------------------------------------------------------------------
step "3. Conflicts with other apps on this Mac"

# Compose names a project after its folder. If another app's project has the
# same name, 'docker compose down' here would stop that app's containers.
PROJECT="$(docker compose config 2>/dev/null | awk '/^name:/ {print $2; exit}')"
[ -n "$PROJECT" ] || fail "docker-compose.yml could not be read. Run 'docker compose config' to see why."
THIS_COMPOSE="$HERE/docker-compose.yml"
if docker compose ls -a --format json 2>/dev/null \
    | tr '{' '\n' | grep "\"Name\":\"$PROJECT\"" | grep -vq "$THIS_COMPOSE"; then
  fail "Another app's Docker project is already called '$PROJECT'. Rename this folder (e.g. to content-hub) and run the script from there."
fi
ok "project name '$PROJECT' is not used by any other app"

# Port 8080 must be free, or already held by our own container.
HOLDER="$(docker ps --filter "publish=$APP_PORT" --format '{{.Names}}' 2>/dev/null | head -1)"
if [ -n "$HOLDER" ]; then
  if docker compose ps --format '{{.Name}}' 2>/dev/null | grep -qx "$HOLDER"; then
    ok "port $APP_PORT is held by Content Hub itself (will be restarted)"
  else
    fail "port $APP_PORT is already used by container '$HOLDER'. Stop it, or change the left side of \"$APP_PORT:8080\" in docker-compose.yml."
  fi
elif lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "port $APP_PORT is already used by: $(lsof -nP -iTCP:$APP_PORT -sTCP:LISTEN | awk 'NR==2 {print $1, "(pid", $2 ")"}'). Stop it, or change the left side of \"$APP_PORT:8080\" in docker-compose.yml."
else
  ok "port $APP_PORT is free"
fi

MIDAS_BEFORE=0
if curl -fs -o /dev/null --max-time 3 "http://localhost:$MIDAS_PORT"; then
  MIDAS_BEFORE=1
  ok "Midas is answering on $MIDAS_PORT (will re-check after)"
fi

# ---------------------------------------------------------------------------
step "4. Jobs in flight"
# Restarting fails every running or queued audio job (their state is in memory).
if curl -fs -o /dev/null --max-time 3 "http://localhost:$APP_PORT/health"; then
  ACTIVE="$(curl -fs --max-time 5 "http://localhost:$APP_PORT/batch/active" | tr -d '[:space:]')"
  if [ -n "$ACTIVE" ] && [ "$ACTIVE" != "[]" ]; then
    echo "  Content Hub is running these jobs right now:"
    echo "  $ACTIVE"
    fail "Restarting would fail them. Wait for them to finish (or cancel them), then run this again."
  fi
  ok "Content Hub is running with no jobs in flight, safe to restart"
else
  ok "Content Hub is not running yet"
fi

# ---------------------------------------------------------------------------
step "5. Pull and start"
docker compose pull || fail "Could not pull the image. Check the internet connection on this Mac."
docker compose up -d || fail "Docker could not start the container. The reason is in the output above."
ok "container started"

echo "  waiting for Content Hub to answer..."
READY=0
for _ in $(seq 1 90); do
  if curl -fs -o /dev/null --max-time 2 "http://localhost:$APP_PORT/health"; then READY=1; break; fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  docker compose logs --tail 30
  fail "Content Hub did not answer on port $APP_PORT within 90 seconds. The log is above."
fi
ok "Content Hub is healthy"

# ---------------------------------------------------------------------------
step "6. Checks"
STATUS="$(curl -fs --max-time 5 "http://localhost:$APP_PORT/config/session-env/status")"
if echo "$STATUS" | grep -q '"configured":true'; then
  ok "all required keys in .env are set"
else
  MISSING="$(echo "$STATUS" | sed -n 's/.*"missing_keys":\[\([^]]*\)\].*/\1/p' | tr -d '"')"
  warn "keys missing from .env: ${MISSING:-unknown}"
  warn "fill them in, then run: docker compose up -d --force-recreate"
fi

if [ "$MIDAS_BEFORE" -eq 1 ]; then
  if curl -fs -o /dev/null --max-time 3 "http://localhost:$MIDAS_PORT"; then
    ok "Midas is still answering on $MIDAS_PORT"
  else
    warn "Midas stopped answering on $MIDAS_PORT. Check it in Docker Desktop."
  fi
fi

# ---------------------------------------------------------------------------
step "7. Comp engine (Compilations)"
# Runs outside Docker, as a LaunchAgent: see comp_gen/macos/install.sh. Optional:
# without it, only the Compilations pane is unavailable.
if [ -f "$HERE/comp_gen/macos/install.sh" ]; then
  bash "$HERE/comp_gen/macos/install.sh" || warn "Comp engine install did not finish (the reason is above). Everything else is running."
elif curl -fs -o /dev/null --max-time 2 "http://127.0.0.1:8765/health"; then
  ok "Comp engine already running on this Mac"
else
  warn "Comp engine not installed. To enable Compilations, copy the repo's comp_gen/ folder next to this script and run it again."
fi
if curl -fs -o /dev/null --max-time 2 "http://127.0.0.1:8765/health"; then
  if docker compose exec -T autodub python -c "import httpx,sys; sys.exit(httpx.get('http://host.docker.internal:8765/health', timeout=3).status_code != 200)" 2>/dev/null; then
    ok "Content Hub can reach the Comp engine"
  else
    warn "the container cannot reach host.docker.internal:8765. Compilations will say the engine isn't answering."
  fi
fi

# ---------------------------------------------------------------------------
HOSTNAME_LOCAL="$(scutil --get LocalHostName 2>/dev/null).local"
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"

echo ""
echo "============================================================"
echo " Content Hub is running."
echo ""
echo " On this Mac:            http://localhost:$APP_PORT"
echo " From the office network: http://$HOSTNAME_LOCAL:$APP_PORT"
[ -n "$IP" ] && echo "                         http://$IP:$APP_PORT"
echo ""
echo " It restarts by itself after a reboot, as long as Docker Desktop"
echo " starts at login (Docker Desktop > Settings > General)."
echo ""
echo " Update later:  run this script again"
echo " Logs:          docker compose logs -f autodub"
echo " Stop:          double-click 'Stop Content Hub.command'"
echo "============================================================"
