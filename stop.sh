#!/bin/bash
# Stop AutoDub service
# Usage: ./stop.sh

echo "Stopping AutoDub service..."
docker compose down
echo "✅ Service stopped"
echo ""
echo "To view logs of the stopped container:"
echo "  docker compose logs"
echo ""
echo "To remove everything including data:"
echo "  docker compose down -v"
