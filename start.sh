#!/bin/bash
# Quick start script for AutoDub GHCR setup
# Usage: ./start.sh

set -e

IMAGE="ghcr.io/jugaadchhabra/autodub:latest"

echo "AutoDub Quick Start"
echo "==================="

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "Docker is not installed. Please install from https://www.docker.com/products/docker-desktop"
    exit 1
fi

# Check if .env exists
if [ ! -f .env ]; then
    echo "Creating empty .env file..."
    cat > .env <<'EOF'
# Fill all required values before running start.sh again.
SARVAM_API=
GEMINI_API_KEY=
WASABI_ACCESS_KEY=
WASABI_SECRET_KEY=
WASABI_BUCKET=
WASABI_REGION=
WASABI_ENDPOINT_URL=
AWS_ACCESS_KEY=
AWS_SECRET_KEY=
AWS_BUCKET=
AWS_REGION=
BATCH_ENABLE_WASABI_UPLOAD=true
BATCH_ENABLE_QC=true
QC_LOG_SINK=s3
ELEVEN_LABS=
AI_STUDIO_VOICE=
DESI_VOCAL_VOICE=
EOF
    echo "Created .env file."
    echo ""
    echo "Edit .env and add all required keys:"
    echo "  - SARVAM_API"
    echo "  - GEMINI_API_KEY"
    echo "  - WASABI_ACCESS_KEY"
    echo "  - WASABI_SECRET_KEY"
    echo "  - WASABI_BUCKET"
    echo "  - WASABI_REGION"
    echo "  - WASABI_ENDPOINT_URL"
    echo "  - AWS_ACCESS_KEY"
    echo "  - AWS_SECRET_KEY"
    echo "  - AWS_BUCKET"
    echo "  - AWS_REGION"
    echo "  - BATCH_ENABLE_WASABI_UPLOAD"
    echo "  - BATCH_ENABLE_QC"
    echo "  - ELEVEN_LABS"
    echo "  - AI_STUDIO_VOICE"
    echo "  - DESI_VOCAL_VOICE"
    echo ""
    echo "Then run this script again."
    exit 1
fi

required_keys=(
  "SARVAM_API"
  "GEMINI_API_KEY"
  "WASABI_ACCESS_KEY"
  "WASABI_SECRET_KEY"
  "WASABI_BUCKET"
  "WASABI_REGION"
  "WASABI_ENDPOINT_URL"
  "AWS_ACCESS_KEY"
  "AWS_SECRET_KEY"
  "AWS_BUCKET"
  "AWS_REGION"
  "BATCH_ENABLE_WASABI_UPLOAD"
  "BATCH_ENABLE_QC"
  "ELEVEN_LABS"
  "AI_STUDIO_VOICE"
  "DESI_VOCAL_VOICE"
)

missing=()
for key in "${required_keys[@]}"; do
  if ! grep -Eq "^[[:space:]]*${key}[[:space:]]*=[[:space:]]*.+$" .env; then
    missing+=("${key}")
  fi
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing required keys in .env:"
  for key in "${missing[@]}"; do
    echo "  - ${key}"
  done
  exit 1
fi

echo "Config looks good."
echo ""

# Create necessary directories
mkdir -p uploads output data logs
echo "Created directories: uploads, output, data, logs"

echo ""
echo "Pulling image: ${IMAGE}"
docker pull "${IMAGE}"

echo ""
echo "Starting AutoDub service..."
docker compose up -d

# Wait for service to be ready
echo "Waiting for service to start..."
sleep 5

# Check health
if curl -s http://localhost:8080/health > /dev/null; then
    echo "Service is healthy."
    echo ""
    echo "AutoDub is running."
    echo ""
    echo "Open your browser: http://localhost:8080"
    echo ""
    echo "To view logs: docker compose logs -f"
    echo "To stop:      docker compose down"
else
    echo "Service started but may still be initializing (first run can take a minute)."
    echo "Check status: docker compose logs"
fi
