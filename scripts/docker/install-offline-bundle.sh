#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ "${EUID}" -ne 0 ]; then
    echo "ERR: run as root: sudo bash install.sh" >&2
    exit 1
fi

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERR: '$1' is required on this server." >&2
        echo "Install Docker Engine + Docker Compose plugin first, then rerun this installer." >&2
        exit 1
    fi
}

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
    if [ -n "$ip" ]; then
        echo "$ip"
    else
        echo "127.0.0.1"
    fi
}

prompt_default() {
    local var_name="$1"
    local prompt="$2"
    local default_value="${3:-}"
    local value

    if [ -n "$default_value" ]; then
        read -r -p "$prompt [$default_value]: " value
        value="${value:-$default_value}"
    else
        read -r -p "$prompt: " value
    fi

    printf -v "$var_name" '%s' "$value"
}

read_env_if_exists() {
    if [ -f .env ]; then
        set -a
        # shellcheck disable=SC1091
        . ./.env
        set +a
    fi
}

set_env_kv() {
    local key="$1"
    local value="$2"
    local tmp

    touch .env
    chmod 600 .env || true

    if grep -qE "^${key}=" .env; then
        tmp="$(mktemp)"
        awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k{$0=k"="v} {print}' .env > "$tmp"
        mv "$tmp" .env
    else
        echo "${key}=${value}" >> .env
    fi
}

need_cmd docker

if ! docker compose version >/dev/null 2>&1; then
    echo "ERR: Docker Compose plugin is required." >&2
    exit 1
fi

install_eve_cli() {
    local src="./eve"
    local dst="/usr/local/bin/eve"
    local cfg="/etc/eve-docker.conf"

    if [ -f "$src" ]; then
        cp "$src" "$dst"
        chmod +x "$dst"
        cat > "$cfg" <<EOF
INSTALL_DIR=$(pwd)
COMPOSE_FILE=$(pwd)/docker-compose.yml
ENV_FILE=$(pwd)/.env
EOF
        chmod 644 "$cfg"
        echo "OK: installed eve CLI to $dst"
    else
        echo "WARN: eve CLI not found next to install.sh (skipping)" >&2
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
            # HTTP-only, no redirects, no ACME.
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

configure_env() {
    local default_ssl_mode

    read_env_if_exists

    echo "-- Configure Eve"
    prompt_default DOMAIN "Domain or IP for this server (example: panel.example.com)" "${DOMAIN:-}"

    default_ssl_mode="${SSL_MODE:-http}"
    if echo "$DOMAIN" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
        default_ssl_mode="http"
    fi

    prompt_default SSL_MODE "SSL mode (http|internal|letsencrypt)" "$default_ssl_mode"
    if echo "$DOMAIN" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' && [ "$SSL_MODE" = "letsencrypt" ]; then
        echo "WARN: IP addresses cannot use Let's Encrypt. Switching SSL_MODE to http."
        SSL_MODE="http"
    fi

    prompt_default LETSENCRYPT_EMAIL "Let's Encrypt email (optional)" "${LETSENCRYPT_EMAIL:-admin@${DOMAIN}}"
    prompt_default POSTGRES_PASSWORD "PostgreSQL password" "${POSTGRES_PASSWORD:-$(random_secret)}"
    prompt_default INITIAL_ADMIN_USERNAME "Initial admin username" "${INITIAL_ADMIN_USERNAME:-admin}"
    prompt_default INITIAL_ADMIN_PASSWORD "Initial admin password" "${INITIAL_ADMIN_PASSWORD:-$(random_secret)}"

    set_env_kv "DOMAIN" "$DOMAIN"
    set_env_kv "SSL_MODE" "$SSL_MODE"
    set_env_kv "LETSENCRYPT_EMAIL" "$LETSENCRYPT_EMAIL"
    set_env_kv "EVE_IMAGE" "${EVE_IMAGE:-ghcr.io/aibedini/eve:latest}"
    set_env_kv "POSTGRES_PASSWORD" "$POSTGRES_PASSWORD"
    set_env_kv "INITIAL_ADMIN_USERNAME" "$INITIAL_ADMIN_USERNAME"
    set_env_kv "INITIAL_ADMIN_PASSWORD" "$INITIAL_ADMIN_PASSWORD"
    set_env_kv "GUNICORN_WORKERS" "${GUNICORN_WORKERS:-3}"
    set_env_kv "GUNICORN_THREADS" "${GUNICORN_THREADS:-4}"
    set_env_kv "GUNICORN_TIMEOUT" "${GUNICORN_TIMEOUT:-120}"
    set_env_kv "SESSION_COOKIE_SECURE" "${SESSION_COOKIE_SECURE:-false}"

    write_caddyfile "$SSL_MODE"
}

create_default_env() {
    DOMAIN="${DOMAIN:-$(detect_default_domain)}"
    SSL_MODE="${SSL_MODE:-http}"
    LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-admin@${DOMAIN}}"
    POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(random_secret)}"
    INITIAL_ADMIN_USERNAME="${INITIAL_ADMIN_USERNAME:-admin}"
    INITIAL_ADMIN_PASSWORD="${INITIAL_ADMIN_PASSWORD:-$(random_secret)}"
    EVE_IMAGE="${EVE_IMAGE:-ghcr.io/aibedini/eve:latest}"
    GUNICORN_WORKERS="${GUNICORN_WORKERS:-3}"
    GUNICORN_THREADS="${GUNICORN_THREADS:-4}"
    GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
    SESSION_COOKIE_SECURE="${SESSION_COOKIE_SECURE:-false}"

    echo "-- Creating default .env"
    set_env_kv "DOMAIN" "$DOMAIN"
    set_env_kv "SSL_MODE" "$SSL_MODE"
    set_env_kv "LETSENCRYPT_EMAIL" "$LETSENCRYPT_EMAIL"
    set_env_kv "EVE_IMAGE" "$EVE_IMAGE"
    set_env_kv "POSTGRES_PASSWORD" "$POSTGRES_PASSWORD"
    set_env_kv "INITIAL_ADMIN_USERNAME" "$INITIAL_ADMIN_USERNAME"
    set_env_kv "INITIAL_ADMIN_PASSWORD" "$INITIAL_ADMIN_PASSWORD"
    set_env_kv "GUNICORN_WORKERS" "$GUNICORN_WORKERS"
    set_env_kv "GUNICORN_THREADS" "$GUNICORN_THREADS"
    set_env_kv "GUNICORN_TIMEOUT" "$GUNICORN_TIMEOUT"
    set_env_kv "SESSION_COOKIE_SECURE" "$SESSION_COOKIE_SECURE"

    write_caddyfile "$SSL_MODE"
}

smoke_test() {
    echo "-- Checking Eve health"
    if docker compose exec -T app curl -fsS http://127.0.0.1:5000/healthz >/dev/null 2>&1; then
        echo "OK: app healthcheck passed"
    else
        echo "WARN: app healthcheck is not ready yet. Check logs: docker compose logs -f app" >&2
    fi

    echo
    echo "Open:"
    if grep -qE '^SSL_MODE=http$' .env 2>/dev/null; then
        echo "  http://$(grep -E '^DOMAIN=' .env | tail -n 1 | cut -d= -f2-)"
    elif grep -qE '^SSL_MODE=internal$' .env 2>/dev/null; then
        echo "  https://$(grep -E '^DOMAIN=' .env | tail -n 1 | cut -d= -f2-)  (browser warning is expected)"
    else
        echo "  https://$(grep -E '^DOMAIN=' .env | tail -n 1 | cut -d= -f2-)"
    fi

    echo
    echo "Initial login:"
    echo "  Username: $(grep -E '^INITIAL_ADMIN_USERNAME=' .env | tail -n 1 | cut -d= -f2-)"
    echo "  Password: $(grep -E '^INITIAL_ADMIN_PASSWORD=' .env | tail -n 1 | cut -d= -f2-)"
    echo
    echo "Note: these credentials are created only when the database has no admin yet."
}

if [ ! -f docker-images.tar ]; then
    echo "ERR: docker-images.tar not found next to install.sh" >&2
    exit 1
fi

echo "-- Loading Docker images"
docker load -i docker-images.tar

if [ ! -f .env ]; then
    create_default_env
else
    echo "-- Existing .env found; keeping it"
    read -r -p "Configure domain/SSL now? [y/N]: " configure_now
    if [ "$configure_now" = "y" ] || [ "$configure_now" = "Y" ]; then
        configure_env
    else
        SSL_MODE_EXISTING="$(grep -E '^SSL_MODE=' .env | tail -n 1 | cut -d= -f2- || true)"
        if [ -n "$SSL_MODE_EXISTING" ]; then
            write_caddyfile "$SSL_MODE_EXISTING"
        fi
    fi
fi

echo "-- Starting Eve"
docker compose up -d

install_eve_cli

echo
echo "OK: Eve is starting."
echo "Status: docker compose ps"
echo "Logs:   docker compose logs -f app"
smoke_test
