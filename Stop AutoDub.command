#!/bin/bash
# Double-click this file to stop AutoDub.

cd "$(dirname "$0")" || exit 1

echo "Stopping AutoDub..."
docker compose down
echo ""
echo "AutoDub has been stopped."
read -n 1 -s -r -p "Press any key to close this window..."
