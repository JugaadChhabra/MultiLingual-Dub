#!/bin/bash
# Double-click this file to stop AutoDub.

cd "$(dirname "$0")" || exit 1

echo "Stopping AutoDub..."
if ! docker compose down; then
  echo ""
  echo "AutoDub may still be running -- Docker reported a problem above."
  echo "Read that output, or check Docker Desktop."
  echo ""
  read -n 1 -s -r -p "Press any key to close this window..."
  exit 1
fi

echo ""
echo "AutoDub has been stopped."
read -n 1 -s -r -p "Press any key to close this window..."

# The pause above is a courtesy, not the result. Without this an EOF on
# stdin makes `read` fail and a successful run exits non-zero.
exit 0
