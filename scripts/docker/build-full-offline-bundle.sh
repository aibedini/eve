#!/usr/bin/env bash
set -euo pipefail

# Build a FULL offline bundle for Ubuntu 22.04 (Jammy) amd64.
# The resulting archive contains:
#   - Docker Engine + Compose plugin .deb packages (downloaded in a clean
#     Ubuntu 22.04 container so all transitive deps are captured)
#   - Eve app, PostgreSQL 16, Redis 7, and Caddy 2 Docker images
#   - docker-compose.yml, Caddyfile, example .env, and the installer
#
# Requirements (on the build machine):
#   - Docker Engine with internet access
#   - tar
#
# Usage:
#   bash scripts/docker/build-full-offline-bundle.sh
#
# The output file is: eve-full-offline-bundle.tar.gz

APP_IMAGE="${APP_IMAGE:-ghcr.io/aibedini/eve:latest}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16-alpine}"
CADDY_IMAGE="${CADDY_IMAGE:-caddy:2-alpine}"
REDIS_IMAGE="${REDIS_IMAGE:-redis:7-alpine}"
OUT_DIR="${OUT_DIR:-eve-full-offline-bundle}"
OUT_FILE="${OUT_FILE:-eve-full-offline-bundle.tar.gz}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERR: '$1' is required on the build machine." >&2
        exit 1
    fi
}

need_cmd docker
need_cmd tar

rm -rf "$OUT_DIR" "$OUT_FILE"
mkdir -p "$OUT_DIR/docker" "$OUT_DIR/docker-debs"

# ---- Step 1: Download Docker .deb packages inside a clean Ubuntu 22.04 container ----
# Running in a container ensures we capture ALL dependency packages that a
# fresh Ubuntu 22.04 install does not already have.
echo "-- Downloading Docker .deb packages via Ubuntu 22.04 container"
docker run --rm \
    -v "$(pwd)/$OUT_DIR/docker-debs:/output" \
    ubuntu:22.04 bash -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq ca-certificates curl gnupg

        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg

        echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu jammy stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -qq

        # Download all packages + transitive deps without installing
        apt-get install -y --download-only \
            docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin 2>&1

        cp /var/cache/apt/archives/*.deb /output/
        echo "Packages downloaded:"
        ls /output/*.deb
    '

echo "-- Building Eve image: $APP_IMAGE"
docker build -t "$APP_IMAGE" .

echo "-- Pulling runtime images"
docker pull "$POSTGRES_IMAGE"
docker pull "$CADDY_IMAGE"
docker pull "$REDIS_IMAGE"

echo "-- Saving Docker images to tar"
docker save -o "$OUT_DIR/docker-images.tar" \
    "$APP_IMAGE" \
    "$POSTGRES_IMAGE" \
    "$CADDY_IMAGE" \
    "$REDIS_IMAGE"

# ---- Step 2: Copy config files and installer ----
cp docker-compose.yml        "$OUT_DIR/docker-compose.yml"
cp .env.docker.example       "$OUT_DIR/.env.example"
cp docker/Caddyfile          "$OUT_DIR/docker/Caddyfile"
cp scripts/docker/install-full-offline-bundle.sh "$OUT_DIR/install.sh"
cp scripts/docker/eve        "$OUT_DIR/eve"
chmod +x "$OUT_DIR/install.sh" "$OUT_DIR/eve"

cat > "$OUT_DIR/README.txt" <<EOF
Eve X-UI Manager — Full Offline Bundle
Ubuntu 22.04 (Jammy) amd64

This archive contains everything a bare Ubuntu 22.04 server needs:
  - Docker Engine + Docker Compose plugin (.deb packages in docker-debs/)
  - Eve app, PostgreSQL 16, Redis 7, Caddy 2 (Docker images in docker-images.tar)
  - Configuration templates and an interactive installer

Quick start:
  mkdir -p /opt/eve-docker
  tar -xzf eve-full-offline-bundle.tar.gz -C /opt/eve-docker
  cd /opt/eve-docker
  sudo bash install.sh

After install, use the management CLI:
  sudo eve

No internet access is needed on the target server.

Included Docker images:
  - ${APP_IMAGE}
  - ${POSTGRES_IMAGE}
  - ${CADDY_IMAGE}
  - ${REDIS_IMAGE}
EOF

# ---- Step 3: Create archive ----
echo "-- Creating archive: $OUT_FILE"
tar -czf "$OUT_FILE" -C "$OUT_DIR" .

BUNDLE_SIZE="$(du -sh "$OUT_FILE" | awk '{print $1}')"
echo
echo "OK: $OUT_FILE is ready (${BUNDLE_SIZE})."
echo
echo "Upload it to GitHub Releases, then on the target Ubuntu 22.04 server run:"
echo "  mkdir -p /opt/eve-docker"
echo "  tar -xzf eve-full-offline-bundle.tar.gz -C /opt/eve-docker"
echo "  cd /opt/eve-docker && sudo bash install.sh"
