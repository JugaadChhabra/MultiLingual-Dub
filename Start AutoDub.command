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

# Start (and auto-update) the app.
docker compose up -d
if [ $? -ne 0 ]; then
  echo ""
  echo "Something went wrong while starting AutoDub."
  echo "Make sure the .env file is filled in and try again."
  read -n 1 -s -r -p "Press any key to close this window..."
  exit 1
fi

echo ""
echo "Getting AutoDub ready..."
for i in $(seq 1 60); do
  if curl -fs http://localhost:8080/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo ""
echo "AutoDub is running!  ->  http://localhost:8080"
echo "Opening it in your browser now..."
open http://localhost:8080

echo ""
echo "You can close this window. AutoDub keeps running in the background."
read -n 1 -s -r -p "Press any key to close this window..."
