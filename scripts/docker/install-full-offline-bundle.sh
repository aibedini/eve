#!/usr/bin/env bash
set -euo pipefail

# Full offline installer for Ubuntu 22.04 (Jammy) amd64.
# Installs Docker from bundled .deb packages, loads Docker images,
# configures Eve, and starts the stack — all without internet access.
#
# Usage:
#   sudo bash install.sh

cd "$(dirname "$0")"

if [ "${EUID}" -ne 0 ]; then
    echo "ERR: run as root: sudo bash install.sh" >&2
    exit 1
fi

# ---- Helpers ----
random_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 24
    else
        date +%s%N | sha256sum | awk '{print $1}'
    fi
}

detect_default_domain() {
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    [ -n "$ip" ] && echo "$ip" || echo "127.0.0.1"
}

prompt_default() {
    local var_name="$1" prompt="$2" default_value="${3:-}" value
    if [ -n "$default_value" ]; then
        read -r -p "$prompt [$default_value]: " value
        value="${value:-$default_value}"
    else
        read -r -p "$prompt: " value
    fi
    printf -v "$var_name" '%s' "$value"
}

read_env_if_exists() {
    [ -f .env ] || return 0
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
}

set_env_kv() {
    local key="$1" value="$2" tmp
    touch .env
    chmod 600 .env || true
    if grep -qE "^${key}=" .env; then
        tmp="$(mktemp)"
        awk -v k="$key" -v v="$value" \
            'BEGIN{FS=OFS="="} $1==k{$0=k"="v} {print}' .env > "$tmp"
        mv "$tmp" .env
    else
        echo "${key}=${value}" >> .env
    fi
}

write_caddyfile() {
    local ssl_mode="$1"
    mkdir -p docker
    case "$ssl_mode" in
        letsencrypt)
            cat > docker/Caddyfile <<'EOF'
{
    email {$LETSENCRYPT_EMAIL}
}

{$DOMAIN} {
    encode gzip zstd
    reverse_proxy app:5000
}
EOF
            ;;
        internal)
            cat > docker/Caddyfile <<'EOF'
{
    email {$LETSENCRYPT_EMAIL}
}

{$DOMAIN} {
    tls internal
    encode gzip zstd
    reverse_proxy app:5000
}
EOF
            ;;
        http)
            cat > docker/Caddyfile <<'EOF'
{
    auto_https off
}

http://{$DOMAIN} {
    encode gzip zstd
    reverse_proxy app:5000
}
EOF
            ;;
        *)
            echo "ERR: invalid SSL_MODE: $ssl_mode (use: letsencrypt | internal | http)" >&2
            exit 1
            ;;
    esac
}

# ---- Install Docker from bundled .deb packages ----
install_docker_offline() {
    local deb_dir="./docker-debs"

    if [ ! -d "$deb_dir" ] || [ -z "$(ls -A "$deb_dir"/*.deb 2>/dev/null)" ]; then
        echo "ERR: docker-debs/ directory is missing or empty." >&2
        echo "     Make sure you extracted the full offline bundle." >&2
        exit 1
    fi

    echo "-- Installing Docker from bundled packages (no internet needed)"

    # Install in dependency order; --force-depends allows circular ordering.
    # dpkg --configure -a resolves any deferred configuration afterward.
    for pkg in containerd.io docker-ce-cli docker-ce docker-buildx-plugin docker-compose-plugin; do
        deb_file="$(ls "$deb_dir"/${pkg}_*.deb 2>/dev/null | head -1 || true)"
        if [ -n "$deb_file" ]; then
            dpkg -i --force-depends "$deb_file" 2>&1 || true
        fi
    done

    dpkg --configure -a 2>&1 || true

    systemctl enable docker  2>/dev/null || true
    systemctl start  docker  2>/dev/null || true

    if ! docker --version >/dev/null 2>&1; then
        echo "ERR: Docker installation failed. Check 'dpkg -l | grep docker'." >&2
        exit 1
    fi

    echo "OK: $(docker --version)"

    if ! docker compose version >/dev/null 2>&1; then
        echo "ERR: Docker Compose plugin not available after install." >&2
        exit 1
    fi

    echo "OK: $(docker compose version)"
}

# ---- Configure .env ----
configure_env() {
    read_env_if_exists

    echo "-- Configure Eve"
    prompt_default DOMAIN "Domain or IP for this server (example: panel.example.com)" \
        "${DOMAIN:-$(detect_default_domain)}"

    local default_ssl="http"
    if echo "$DOMAIN" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
        default_ssl="http"
    fi

    prompt_default SSL_MODE "SSL mode (http|internal|letsencrypt)" "${SSL_MODE:-$default_ssl}"

    if echo "$DOMAIN" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' && [ "$SSL_MODE" = "letsencrypt" ]; then
        echo "WARN: IP address detected — switching SSL_MODE to http."
        SSL_MODE="http"
    fi

    prompt_default LETSENCRYPT_EMAIL \
        "Let's Encrypt email (optional)" \
        "${LETSENCRYPT_EMAIL:-admin@${DOMAIN}}"

    prompt_default POSTGRES_PASSWORD \
        "PostgreSQL password" \
        "${POSTGRES_PASSWORD:-$(random_secret)}"

    prompt_default INITIAL_ADMIN_USERNAME \
        "Initial admin username" \
        "${INITIAL_ADMIN_USERNAME:-admin}"

    prompt_default INITIAL_ADMIN_PASSWORD \
        "Initial admin password" \
        "${INITIAL_ADMIN_PASSWORD:-$(random_secret)}"

    set_env_kv "DOMAIN"                 "$DOMAIN"
    set_env_kv "SSL_MODE"               "$SSL_MODE"
    set_env_kv "LETSENCRYPT_EMAIL"      "$LETSENCRYPT_EMAIL"
    set_env_kv "EVE_IMAGE"              "${EVE_IMAGE:-ghcr.io/aibedini/eve:latest}"
    set_env_kv "POSTGRES_PASSWORD"      "$POSTGRES_PASSWORD"
    set_env_kv "INITIAL_ADMIN_USERNAME" "$INITIAL_ADMIN_USERNAME"
    set_env_kv "INITIAL_ADMIN_PASSWORD" "$INITIAL_ADMIN_PASSWORD"
    set_env_kv "GUNICORN_WORKERS"       "${GUNICORN_WORKERS:-3}"
    set_env_kv "GUNICORN_THREADS"       "${GUNICORN_THREADS:-4}"
    set_env_kv "GUNICORN_TIMEOUT"       "${GUNICORN_TIMEOUT:-120}"
    set_env_kv "SESSION_COOKIE_SECURE"  "${SESSION_COOKIE_SECURE:-false}"

    write_caddyfile "$SSL_MODE"
}

# ---- Install eve CLI ----
install_eve_cli() {
    local src="./eve" dst="/usr/local/bin/eve" cfg="/etc/eve-docker.conf"
    [ -f "$src" ] || { echo "WARN: eve CLI not found (skipping)"; return 0; }
    cp "$src" "$dst"
    chmod +x "$dst"
    cat > "$cfg" <<EOF
INSTALL_DIR=$(pwd)
COMPOSE_FILE=$(pwd)/docker-compose.yml
ENV_FILE=$(pwd)/.env
EOF
    chmod 644 "$cfg"
    echo "OK: eve CLI installed at $dst"
}

smoke_test() {
    echo "-- Health check"
    if docker compose exec -T app curl -fsS http://127.0.0.1:5000/healthz >/dev/null 2>&1; then
        echo "OK: app is healthy"
    else
        echo "WARN: app not ready yet — check: docker compose logs -f app"
    fi

    echo
    echo "Open:"
    local ssl domain
    ssl="$(grep -E '^SSL_MODE=' .env | tail -1 | cut -d= -f2- || true)"
    domain="$(grep -E '^DOMAIN=' .env | tail -1 | cut -d= -f2- || true)"
    case "${ssl:-http}" in
        http)     echo "  http://${domain}" ;;
        internal) echo "  https://${domain}  (browser warning is expected)" ;;
        *)        echo "  https://${domain}" ;;
    esac

    echo
    echo "Initial login:"
    echo "  Username: $(grep -E '^INITIAL_ADMIN_USERNAME=' .env | tail -1 | cut -d= -f2-)"
    echo "  Password: $(grep -E '^INITIAL_ADMIN_PASSWORD=' .env | tail -1 | cut -d= -f2-)"
    echo
    echo "Note: these credentials are only created when the database has no admin yet."
}

# ---- Main ----
[ -f docker-images.tar ] || {
    echo "ERR: docker-images.tar not found. Are you in the extracted bundle directory?" >&2
    exit 1
}

# Install Docker if not present
if ! command -v docker >/dev/null 2>&1; then
    install_docker_offline
else
    echo "-- Docker already installed: $(docker --version)"
    if [ -d "./docker-debs" ]; then
        echo "   (docker-debs/ present but skipped — Docker is already available)"
    fi
fi

# Verify Docker Compose plugin
if ! docker compose version >/dev/null 2>&1; then
    echo "ERR: Docker Compose plugin is missing. Re-run without Docker installed to trigger offline install." >&2
    exit 1
fi

echo "-- Loading Docker images (offline)"
docker load -i docker-images.tar

if [ ! -f .env ]; then
    configure_env
else
    echo "-- Existing .env found; keeping it"
    read -r -p "Reconfigure domain/SSL now? [y/N]: " reconf
    if [ "${reconf:-N}" = "y" ] || [ "${reconf:-N}" = "Y" ]; then
        configure_env
    else
        ssl_existing="$(grep -E '^SSL_MODE=' .env | tail -1 | cut -d= -f2- || true)"
        [ -n "$ssl_existing" ] && write_caddyfile "$ssl_existing"
    fi
fi

echo "-- Starting Eve"
docker compose up -d

install_eve_cli

echo
echo "OK: Eve is starting."
echo "Status : docker compose ps"
echo "Logs   : docker compose logs -f app"
smoke_test
