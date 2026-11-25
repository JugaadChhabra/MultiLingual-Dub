# Multi-stage build for minimal image size

# The Comp engine (Compilations) runs natively on the Mac mini, outside this
# container, but ships inside the image so the image stays the only thing that
# is deployed: setup-mac-mini.sh copies /app/comp_engine out and installs it.
# Built once on the build machine for Apple silicon; Go signs it itself.
FROM --platform=$BUILDPLATFORM golang:1.24 AS compengine
WORKDIR /src
COPY comp_gen/source/ .
RUN CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -ldflags "-s -w" -o /out/CompCreator .
COPY comp_gen/macos/install.sh comp_gen/macos/config.example.json /out/

FROM python:3.13-slim as base

# Set working directory
WORKDIR /app

# Install system dependencies required for audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire project
COPY . .

COPY --from=compengine /out/ /app/comp_engine/

# Create necessary directories
RUN mkdir -p /app/output

# Expose API port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health', timeout=5.0)" || exit 1

# Run the FastAPI server
CMD ["uvicorn", "api.routes:app", "--host", "0.0.0.0", "--port", "8080"]
