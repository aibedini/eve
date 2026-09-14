#!/bin/bash
set -uo pipefail
cd /opt/eve-xui-manager || exit 1
git fetch --tags --prune origin >/dev/null 2>&1
git pull --ff-only origin main 2>&1 | tail -1
echo "== version: $(git rev-parse --short HEAD) $(grep -m1 APP_VERSION app.py)"
chown -R evemgr:evemgr /opt/eve-xui-manager
sudo -u evemgr bash -c 'set -a; . /opt/eve-xui-manager/.env; set +a; unset EVE_SKIP_IMPORT_MIGRATIONS; /opt/eve-xui-manager/venv/bin/python -m panel.migrate' 2>&1 | tail -2
systemctl restart eve-manager eve-manager-background
set -a
. /opt/eve-xui-manager/.env
set +a
sleep 45
echo "== units: $(systemctl is-active eve-manager) $(systemctl is-active eve-manager-background) telegram-bot=$(systemctl is-active eve-manager-telegram-bot) egress=$(systemctl is-active eve-manager-telegram-egress)"
echo "== http: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/login)"
echo "== alembic head: $(psql "$DATABASE_URL" -tAc 'select version_num from alembic_version')"
echo "== alembic version files with down_revision chain head check:"
psql "$DATABASE_URL" -tAc "select count(*) from service_observed_states" | sed 's/^/   ledger rows: /'
echo "== errors since restart: $(journalctl -u eve-manager-background --since '-60 seconds' --no-pager | grep -icE 'traceback|error')"
echo DEPLOY274_DONE