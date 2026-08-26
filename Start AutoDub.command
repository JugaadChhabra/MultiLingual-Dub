#!/bin/bash
# Double-click this file to start AutoDub. It will always fetch the latest
# version automatically, then open the app in your browser.

cd "$(dirname "$0")" || exit 1

echo "============================"
echo "   Starting AutoDub..."
echo "============================"
echo ""

# Make sure Docker Desktop is running before we try anything.
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running."
  echo "Please open Docker Desktop, wait until it says it's running,"
  echo "then double-click this file again."
  echo ""
  read -n 1 -s -r -p "Press any key to close this window..."
  exit 1
fi

# Start the app, pulling a newer image if one has been published.
if ! docker compose up -d; then
  echo ""
  echo "AutoDub did not start."
  echo ""
  echo "The reason is in the Docker output above -- read that first, it names"
  echo "the actual cause. A missing or empty key in the .env file next to this"
  echo "script is one common reason, but it is not the only one."
  echo ""
  read -n 1 -s -r -p "Press any key to close this window..."
  exit 1
fi

echo ""
echo "Getting AutoDub ready..."
ready=0
for _ in $(seq 1 60); do
  if curl -fs http://localhost:8080/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  echo ""
  echo "The container started, but AutoDub never answered on"
  echo "http://localhost:8080 after 60 seconds."
  echo ""
  echo "The last few log lines:"
  echo ""
  docker compose logs --tail 25
  echo ""
  echo "AutoDub is NOT ready. Fix what the log points at, then run this again."
  read -n 1 -s -r -p "Press any key to close this window..."
  exit 1
fi

echo ""
echo "AutoDub is running!  ->  http://localhost:8080"
echo "Opening it in your browser now..."
open http://localhost:8080

echo ""
echo "You can close this window. AutoDub keeps running in the background."
read -n 1 -s -r -p "Press any key to close this window..."

# The pause above is a courtesy, not the result. Without this an EOF on
# stdin makes `read` fail and a successful run exits non-zero.
exit 0
