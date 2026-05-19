#!/usr/bin/env bash
# fix_permissions.sh — Check and fix Eve file-manager directory permissions.
# Run as root (or with sudo) on the production server.
#
#   bash fix_permissions.sh            # auto-detect service user
#   bash fix_permissions.sh www-data   # pass user explicitly

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; }
err()  { echo -e "${RED}  ✗ $*${NC}"; }
info() { echo -e "${CYAN}  → $*${NC}"; }

echo ""
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}   Eve File-Manager Permission Check & Fix        ${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo ""

# ── 1. Find the Eve installation directory ─────────────────────────────────
SERVICE_USER="${1:-}"
POSSIBLE_DIRS=(
    "/opt/eve"
    "/opt/eve-xui-manager"
    "$HOME/eve"
    "$HOME/eve-xui-manager"
    "/var/www/eve"
    "/root/eve"
    "/root/eve-xui-manager"
)

EVE_DIR=""
for d in "${POSSIBLE_DIRS[@]}"; do
    if [ -f "$d/app.py" ]; then
        EVE_DIR="$d"
        break
    fi
done

# Try systemd unit file for accurate path
if [ -z "$EVE_DIR" ]; then
    UNIT_FILE=$(systemctl show -p FragmentPath eve 2>/dev/null | cut -d= -f2 || true)
    if [ -n "$UNIT_FILE" ] && [ -f "$UNIT_FILE" ]; then
        WORK_DIR=$(grep -oP 'WorkingDirectory=\K.*' "$UNIT_FILE" || true)
        if [ -n "$WORK_DIR" ] && [ -f "$WORK_DIR/app.py" ]; then
            EVE_DIR="$WORK_DIR"
        fi
    fi
fi

if [ -z "$EVE_DIR" ]; then
    err "Cannot locate Eve installation (app.py not found)."
    echo "  Pass the path manually: EVE_DIR=/your/path bash fix_permissions.sh"
    echo "  Or run with: EVE_DIR=/opt/eve bash fix_permissions.sh"
    if [ -n "${EVE_DIR_OVERRIDE:-}" ]; then
        EVE_DIR="$EVE_DIR_OVERRIDE"
    else
        exit 1
    fi
fi
ok "Eve directory: $EVE_DIR"

# ── 2. Determine static/app-files directory ────────────────────────────────
STATIC_DIR="$EVE_DIR/static"
APP_FILES_DIR="$STATIC_DIR/app-files"
ok "Target directory: $APP_FILES_DIR"

# ── 3. Detect service user ─────────────────────────────────────────────────
if [ -z "$SERVICE_USER" ]; then
    # Try systemd unit
    UNIT_FILE=$(systemctl show -p FragmentPath eve 2>/dev/null | cut -d= -f2 || true)
    if [ -n "$UNIT_FILE" ] && [ -f "$UNIT_FILE" ]; then
        SERVICE_USER=$(grep -oP 'User=\K.*' "$UNIT_FILE" | head -1 || true)
    fi
fi

if [ -z "$SERVICE_USER" ]; then
    # Fall back to owner of app.py
    SERVICE_USER=$(stat -c '%U' "$EVE_DIR/app.py" 2>/dev/null || echo "")
fi

if [ -z "$SERVICE_USER" ]; then
    warn "Could not detect service user. Using current user: $(whoami)"
    SERVICE_USER="$(whoami)"
fi
ok "Service user: $SERVICE_USER"

# ── 4. Create directories ──────────────────────────────────────────────────
echo ""
info "Checking directories..."

for dir in "$STATIC_DIR" "$APP_FILES_DIR"; do
    if [ ! -d "$dir" ]; then
        info "Creating: $dir"
        mkdir -p "$dir"
        ok "Created: $dir"
    else
        ok "Exists: $dir"
    fi
done

# ── 5. Fix ownership ───────────────────────────────────────────────────────
echo ""
info "Fixing ownership (chown $SERVICE_USER)..."
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_FILES_DIR" 2>/dev/null \
    || chown -R "$SERVICE_USER" "$APP_FILES_DIR"
ok "Ownership set to $SERVICE_USER"

# ── 6. Fix permissions ─────────────────────────────────────────────────────
info "Fixing permissions..."
chmod 755 "$APP_FILES_DIR"
# Make any existing files readable
if ls "$APP_FILES_DIR"/* >/dev/null 2>&1; then
    chmod 644 "$APP_FILES_DIR"/*
fi
ok "Permissions set (755 dir, 644 files)"

# ── 7. Verify write access ─────────────────────────────────────────────────
echo ""
info "Write test..."
TEST_FILE="$APP_FILES_DIR/.write_test_$$"
if touch "$TEST_FILE" 2>/dev/null; then
    rm -f "$TEST_FILE"
    ok "Write test passed"
else
    err "Write test FAILED — directory is still not writable"
    echo "  Manual fix: chown $SERVICE_USER '$APP_FILES_DIR' && chmod 755 '$APP_FILES_DIR'"
    exit 1
fi

# ── 8. Check Nginx client_max_body_size ────────────────────────────────────
echo ""
info "Checking Nginx upload limit..."
NGINX_MAX=$(grep -r "client_max_body_size" /etc/nginx/ 2>/dev/null | head -3 || true)
if [ -z "$NGINX_MAX" ]; then
    warn "client_max_body_size not set in Nginx — default is 1 MB. Large files will fail!"
    echo ""
    echo "  Fix: Edit /etc/nginx/sites-available/eve and add inside the server block:"
    echo "    client_max_body_size 512m;"
    echo "  Then reload: nginx -t && systemctl reload nginx"
else
    echo "  Found: $NGINX_MAX"
    if echo "$NGINX_MAX" | grep -qiE "512m|256m|1g|2g"; then
        ok "Nginx limit looks sufficient (≥256m)"
    else
        warn "Nginx limit may be too low for large installer files"
        echo "  Recommend: client_max_body_size 512m;"
    fi
fi

# ── 9. Optionally restart the Eve service ─────────────────────────────────
echo ""
if systemctl is-active eve >/dev/null 2>&1; then
    info "Restarting Eve service..."
    systemctl restart eve
    ok "Eve service restarted"
else
    warn "Eve service not running as 'eve'. Restart it manually if needed."
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}   All checks complete. Upload should work now.   ${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo "  Diagnostic URL (run in browser while logged in as superadmin):"
echo "  https://YOUR_DOMAIN/api/app-files/health"
echo ""
