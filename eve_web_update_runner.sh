#!/usr/bin/env bash

# Durable, non-interactive updater launched by eve-web-update.service.
# State stays outside APP_DIR so browser polling survives a Gunicorn restart.
set -uo pipefail

APP_DIR="/opt/eve-xui-manager"
APP_USER="evemgr"
SERVICE_NAME="eve-manager"
STATE_DIR="/var/lib/eve-manager/web-update"
STATUS_FILE="$STATE_DIR/status.json"
LOG_FILE="$STATE_DIR/update.log"
BACKUP_FILE="$STATE_DIR/backup-path"
ROLLBACK_FILE="$STATE_DIR/rolled-back"
SYSTEM_BACKUP="$STATE_DIR/system-files.tar.gz"
LOCK_FILE="/run/lock/eve-web-update.lock"

mkdir -p "$STATE_DIR"
chown root:"$APP_USER" "$STATE_DIR"
chmod 750 "$STATE_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 75

: > "$LOG_FILE"
chown root:"$APP_USER" "$LOG_FILE"
chmod 640 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
current_version="$(sed -n 's/^APP_VERSION = "\([^"]*\)"/\1/p' "$APP_DIR/app.py" 2>/dev/null | head -1)"

write_status() {
    local state="$1" message="$2" finished_at="${3:-}" version="${4:-$current_version}"
    python3 - "$STATUS_FILE" "$state" "$message" "$started_at" "$finished_at" "$version" <<'PY'
import json, os, sys, tempfile
path, state, message, started_at, finished_at, version = sys.argv[1:]
payload = {
    'state': state, 'message': message,
    'started_at': started_at or None, 'finished_at': finished_at or None,
    'version': version or None,
}
fd, tmp = tempfile.mkstemp(prefix='.status-', dir=os.path.dirname(path), text=True)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
    chown root:"$APP_USER" "$STATUS_FILE" 2>/dev/null || true
}

health_check() {
    local attempt
    for attempt in $(seq 1 60); do
        if curl --fail --silent --max-time 3 http://127.0.0.1:5000/healthz \
                | grep -q '"success"[[:space:]]*:[[:space:]]*true'; then
            return 0
        fi
        sleep 2
    done
    return 1
}

restore_previous_version() {
    local backup=""
    [ -r "$BACKUP_FILE" ] && backup="$(tr -d '\r\n' < "$BACKUP_FILE")"
    case "$backup" in
        /opt/eve-xui-manager.bak.[0-9]*) ;;
        *) echo "No validated application backup is available for rollback."; return 1 ;;
    esac
    [ -d "$backup" ] || { echo "Application backup does not exist: $backup"; return 1; }

    echo "Update failed; restoring $backup ..."
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    rsync -a --delete \
        --exclude='.env' --exclude='instance/' --exclude='venv/' \
        "$backup/" "$APP_DIR/"
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
    [ ! -s "$SYSTEM_BACKUP" ] || tar -xzf "$SYSTEM_BACKUP" -C / 2>/dev/null || true
    systemctl daemon-reload
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
    systemctl restart "$SERVICE_NAME"
    for unit in background telegram-egress telegram-bot; do
        systemctl restart "${SERVICE_NAME}-${unit}.service" 2>/dev/null || true
    done
    touch "$ROLLBACK_FILE"
    health_check
}

rm -f "$BACKUP_FILE" "$ROLLBACK_FILE" "$SYSTEM_BACKUP"
write_status "running" "Preparing a recoverable update"
echo "Eve browser update started at $started_at (current version: ${current_version:-unknown})"

# Preserve the known-good service/proxy definitions as well as application files.
system_files=()
for path in \
    /etc/systemd/system/eve-manager.service \
    /etc/systemd/system/eve-manager-background.service \
    /etc/systemd/system/eve-manager-telegram-egress.service \
    /etc/systemd/system/eve-manager-telegram-bot.service \
    /etc/nginx/sites-available/eve-manager \
    /usr/local/bin/eve; do
    [ -e "$path" ] && system_files+=("$path")
done
if [ "${#system_files[@]}" -gt 0 ]; then
    tar -czf "$SYSTEM_BACKUP" "${system_files[@]}" 2>/dev/null || true
fi

set +e
env \
    EVE_AUTO_ROLLBACK=true \
    EVE_UPDATE_BACKUP_FILE="$BACKUP_FILE" \
    EVE_UPDATE_ROLLED_BACK_FILE="$ROLLBACK_FILE" \
    DEBIAN_FRONTEND=noninteractive \
    NEEDRESTART_MODE=a \
    APT_LISTCHANGES_FRONTEND=none \
    UCF_FORCE_CONFFOLD=1 \
    PYTHONUNBUFFERED=1 \
    bash "$APP_DIR/setup.sh" --browser-update
update_rc=$?
set -e

if [ "$update_rc" -eq 0 ]; then
    echo "Update command completed; checking panel health ..."
    if health_check; then
        new_version="$(sed -n 's/^APP_VERSION = "\([^"]*\)"/\1/p' "$APP_DIR/app.py" 2>/dev/null | head -1)"
        finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        write_status "succeeded" "Panel updated and health check passed" "$finished_at" "$new_version"
        echo "Panel update completed successfully (version: ${new_version:-unknown})."
        exit 0
    fi
    echo "Updater completed, but the panel did not become healthy."
else
    echo "Update command failed with exit code $update_rc."
fi

# Restore again even when the installer already handled a migration failure:
# this pass uses --delete and restores the known-good service/proxy snapshots,
# guaranteeing that newly introduced files cannot survive a failed update.
rollback_ok=1
restore_previous_version && rollback_ok=0

finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
restored_version="$(sed -n 's/^APP_VERSION = "\([^"]*\)"/\1/p' "$APP_DIR/app.py" 2>/dev/null | head -1)"
if [ "$rollback_ok" -eq 0 ]; then
    write_status "rolled_back" "Update failed; previous healthy version was restored" "$finished_at" "$restored_version"
    echo "Rollback completed; the previous panel version is healthy."
else
    write_status "failed" "Update and automatic rollback both failed; SSH recovery is required" "$finished_at" "$restored_version"
    echo "Automatic rollback could not restore a healthy panel. Check system logs over SSH."
fi
exit 1
