#!/bin/bash
#
# BNQO agent installer — eve control plane
#
# Installs the bnqo-agent static binary, writes /etc/bnqo/agent.toml and a
# hardened systemd unit, and starts the service. The binary enrolls itself on
# first run via POST /api/bnqo/agent/enroll using the one-time enroll token
# placed in the config (see docs/bnqo/EVE_API_CONTRACT.md §2.1).
#
# Usage:
#   sudo BNQO_EVE_URL=https://panel.example.com BNQO_ENROLL_TOKEN=<token> bash install.sh
#   sudo UNINSTALL=1 bash install.sh
#
# Optional env:
#   BNQO_AGENT_NAME  (default: $(hostname -s))
#   BNQO_ROLE        (iran|outside|relay, default: outside)
#   BNQO_PORT        (UDP probe/reflector port, default: 44818)
#
# Idempotent: re-running upgrades the binary and restarts the service.

set -euo pipefail

# ------------------------- Styling -------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

print_header()  { echo; echo -e "${CYAN}${BOLD}  ── $1 ──${NC}"; echo; }
print_success() { echo -e "  ${GREEN}✓${NC} $1"; }
print_error()   { echo -e "  ${RED}✗${NC} $1" >&2; }
print_warning() { echo -e "  ${YELLOW}⚠${NC} $1"; }

BIN_PATH="/usr/local/bin/bnqo-agent"
UNIT_PATH="/etc/systemd/system/bnqo-agent.service"
CONFIG_DIR="/etc/bnqo"
CONFIG_PATH="${CONFIG_DIR}/agent.toml"
STATE_DIR="/var/lib/bnqo"
AGENT_USER="bnqo"

require_root() {
    if [ "${EUID}" -ne 0 ]; then
        print_error "Run this installer as root or with sudo"
        exit 1
    fi
}

# ------------------------- Uninstall -------------------------
if [ "${UNINSTALL:-0}" = "1" ]; then
    require_root
    print_header "BNQO Agent — Uninstall"
    systemctl disable --now bnqo-agent 2>/dev/null || true
    rm -f "$UNIT_PATH"
    systemctl daemon-reload || true
    rm -f "$BIN_PATH"
    rm -rf "$CONFIG_DIR" "$STATE_DIR"
    if id "$AGENT_USER" >/dev/null 2>&1; then
        userdel "$AGENT_USER" 2>/dev/null || print_warning "Could not remove user '${AGENT_USER}' (remove manually)."
    fi
    print_success "BNQO agent removed (service, binary, config, state, user)."
    exit 0
fi

# ------------------------- Inputs -------------------------
BNQO_EVE_URL="${BNQO_EVE_URL:-}"
BNQO_ENROLL_TOKEN="${BNQO_ENROLL_TOKEN:-}"
BNQO_AGENT_NAME="${BNQO_AGENT_NAME:-$(hostname -s 2>/dev/null || hostname)}"
BNQO_ROLE="${BNQO_ROLE:-outside}"
BNQO_PORT="${BNQO_PORT:-44818}"

# Normalize: strip trailing slashes from the panel origin.
BNQO_EVE_URL="${BNQO_EVE_URL%/}"

require_root

if [ -z "$BNQO_EVE_URL" ]; then
    print_error "BNQO_EVE_URL is required (e.g. https://panel.example.com)."
    exit 1
fi
if [ -z "$BNQO_ENROLL_TOKEN" ]; then
    print_error "BNQO_ENROLL_TOKEN is required. Generate one in the eve web UI (/pulse/links → New enroll token)."
    exit 1
fi
case "$BNQO_ROLE" in
    iran|outside|relay) ;;
    *) print_error "BNQO_ROLE must be one of: iran, outside, relay (got '${BNQO_ROLE}')."; exit 1 ;;
esac
if ! [[ "$BNQO_PORT" =~ ^[0-9]+$ ]] || [ "$BNQO_PORT" -lt 1 ] || [ "$BNQO_PORT" -gt 65535 ]; then
    print_error "BNQO_PORT must be a valid UDP port (got '${BNQO_PORT}')."
    exit 1
fi

print_header "BNQO Agent — Install"
echo -e "  Panel  : ${BNQO_EVE_URL}"
echo -e "  Name   : ${BNQO_AGENT_NAME}"
echo -e "  Role   : ${BNQO_ROLE}"
echo -e "  Port   : ${BNQO_PORT} (UDP)"
echo

# ------------------------- Dependencies -------------------------
if ! command -v curl >/dev/null 2>&1; then
    print_warning "curl missing — installing via apt..."
    apt-get update -qq && apt-get install -y curl >/dev/null
fi
command -v curl >/dev/null 2>&1 || { print_error "curl is required but could not be installed."; exit 1; }

# mtr is used for on-demand diagnostics (RUN_MTR jobs). Best-effort only.
if ! command -v mtr >/dev/null 2>&1; then
    print_warning "mtr missing — installing via apt (best-effort)..."
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y mtr-tiny >/dev/null 2>&1 \
        || apt-get install -y mtr >/dev/null 2>&1 \
        || print_warning "Could not install mtr — MTR diagnostics will be unavailable (continuing)."
fi

# ------------------------- User & directories -------------------------
if ! id "$AGENT_USER" >/dev/null 2>&1; then
    NOLOGIN_SHELL="/usr/sbin/nologin"
    [ -x "$NOLOGIN_SHELL" ] || NOLOGIN_SHELL="/sbin/nologin"
    useradd --system --no-create-home --shell "$NOLOGIN_SHELL" "$AGENT_USER"
    print_success "Created system user '${AGENT_USER}'"
fi
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0750 "$STATE_DIR"
install -d -o root -g "$AGENT_USER" -m 0750 "$CONFIG_DIR"

# ------------------------- Binary -------------------------
AGENT_URL="${BNQO_EVE_URL}/static/app-files/bnqo/bnqo-agent"
print_warning "Downloading agent binary: ${AGENT_URL}"
TMP_BIN="$(mktemp /tmp/bnqo-agent-XXXXXX)"
if ! curl -fsSL --connect-timeout 15 --max-time 300 "$AGENT_URL" -o "$TMP_BIN"; then
    rm -f "$TMP_BIN"
    print_error "Download failed (HTTP error from ${AGENT_URL})."
    print_error "The agent binary is served by the eve panel; if it 404s, the build step"
    print_error "has not run yet — see docs/bnqo/EVE_INTEGRATION.md, then retry."
    exit 1
fi
install -m 0755 -o root -g root "$TMP_BIN" "$BIN_PATH"
rm -f "$TMP_BIN"
# Belt-and-braces: the systemd unit also grants CAP_NET_RAW via AmbientCapabilities.
if command -v setcap >/dev/null 2>&1; then
    setcap cap_net_raw+ep "$BIN_PATH" 2>/dev/null \
        || print_warning "setcap failed (filesystem may not support xattrs) — relying on systemd AmbientCapabilities."
fi
print_success "Installed ${BIN_PATH}"

# ------------------------- Config -------------------------
# The agent reads this file on startup and enrolls itself with enroll_token on
# first run (one-time token; consumed by the panel on success).
cat > "$CONFIG_PATH" <<EOF
# BNQO agent configuration — generated by install.sh
eve_url = "${BNQO_EVE_URL}"
name = "${BNQO_AGENT_NAME}"
role = "${BNQO_ROLE}"
port = ${BNQO_PORT}
state_dir = "${STATE_DIR}"
enroll_token = "${BNQO_ENROLL_TOKEN}"
EOF
chown root:"$AGENT_USER" "$CONFIG_PATH"
chmod 0640 "$CONFIG_PATH"
print_success "Wrote ${CONFIG_PATH} (root:${AGENT_USER}, 0640)"

# ------------------------- systemd unit -------------------------
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=BNQO Probe Agent (eve network link monitor)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=${AGENT_USER}
Group=${AGENT_USER}
ExecStart=${BIN_PATH} --config ${CONFIG_PATH}
Restart=always
RestartSec=5

# --- Privilege floor (docs/bnqo/SECURITY_CONTROLS.md §3.1) ---
NoNewPrivileges=true
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW

# --- Filesystem ---
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
StateDirectory=bnqo
ConfigurationDirectory=bnqo

# --- Kernel / namespace surface ---
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
SystemCallArchitectures=native

# --- Resources ---
LimitNOFILE=65536

# --- Environment ---
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now bnqo-agent
print_success "Service bnqo-agent enabled and started"

# ------------------------- Enrollment wait -------------------------
# The binary enrolls on first run using the token in agent.toml; give it up to
# 15s to come up and enroll before reporting.
enrolled=0
for _ in $(seq 1 15); do
    if systemctl is-active --quiet bnqo-agent; then
        enrolled=1
        break
    fi
    sleep 1
done
echo
if [ "$enrolled" = "1" ]; then
    print_success "bnqo-agent is running. Enrollment happens automatically on first run."
else
    print_warning "Service is not active yet after 15s — check the logs below."
fi
echo -e "  Status : systemctl status bnqo-agent"
echo -e "  Logs   : journalctl -u bnqo-agent -f"
echo -e "  Panel  : ${BNQO_EVE_URL}/pulse/links"
