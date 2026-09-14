#!/bin/bash
cd /opt/eve-xui-manager || exit 1
set -a
. /opt/eve-xui-manager/.env
set +a
PSQL="psql $DATABASE_URL -tAc"
echo "=========== 1. DOCTOR: telemetry_pipeline ==========="
/opt/eve-xui-manager/venv/bin/python - <<'PY'
import json, os, sys
sys.path.insert(0, '/opt/eve-xui-manager')
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
from app import app
from panel.services import depletion_pipeline
with app.app_context():
    print(json.dumps(depletion_pipeline.status(), indent=2, default=str))
PY
echo "=========== 2. DB INTEGRITY AUDIT (read-only, PII-free) ==========="
echo "duplicate_event_id=$($PSQL "select count(*) from (select event_id from service_notification_events group by event_id having count(*)>1) d")"
echo "duplicate_logical_transition=$($PSQL "select count(*) from (select service_key,state,state_version from service_notification_events group by 1,2,3 having count(*)>1) d")"
echo "sent_and_superseded=$($PSQL "select count(*) from service_notification_events where status='superseded' and sent_at is not null")"
echo "sent_below_current_generation=$($PSQL "select count(*) from service_notification_events e join service_lifecycle_states s on s.service_key=e.service_key where e.status='sent' and e.lifecycle_generation < s.generation")"
echo "open_event_with_old_generation=$($PSQL "select count(*) from service_notification_events e join service_lifecycle_states s on s.service_key=e.service_key where e.status in ('pending','retry','sending') and e.lifecycle_generation < s.generation")"
echo "expired_lease=$($PSQL "select count(*) from service_notification_events where status='sending' and claimed_at < now() - interval '15 minutes'")"
echo "retry_past_max_attempts=$($PSQL "select count(*) from service_notification_events where status in ('pending','retry') and attempt_count >= 7")"
echo "orphan_event_without_ledger=$($PSQL "select count(*) from service_notification_events e left join service_observed_states o on o.service_key=e.service_key where o.id is null")"
echo "ledger_key_missing_uuid_segment=$($PSQL "select count(*) from service_observed_states where client_uuid is not null and service_key <> 'eve:' || server_id || ':' || client_uuid")"
echo "phone_like_identity_segment=$($PSQL "select count(*) from service_notification_events where service_key ~ '^eve:[0-9]+:(0?9[0-9]{9}|\\+98[0-9]{10})$'")"
echo "ledger_rows_without_uuid=$($PSQL "select count(*) from service_observed_states where client_uuid is null")"
echo "notification_rows_total=$($PSQL "select count(*) from service_notification_events")"
echo "notification_by_status=$($PSQL "select string_agg(status || ':' || c, ',') from (select status, count(*) c from service_notification_events group by status) t")"
echo "ledger_services=$($PSQL "select count(*) from service_observed_states")"
echo "ledger_servers=$($PSQL "select count(distinct server_id) from service_observed_states")"
echo "=========== 3. PERFORMANCE / PLANS ==========="
echo "-- claim query plan (the 5s worker tick):"
psql "$DATABASE_URL" -c "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) SELECT service_notification_events.id FROM service_notification_events WHERE status IN ('pending','retry') AND attempt_count < 7 AND (next_attempt_at IS NULL OR next_attempt_at <= now()) ORDER BY next_attempt_at, id LIMIT 10 FOR UPDATE SKIP LOCKED" | sed 's/^/   /'
echo "-- table sizes / counts:"
psql "$DATABASE_URL" -c "select relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) as size from pg_stat_user_tables where relname in ('service_observed_states','service_notification_events') order by relname" | sed 's/^/   /'
echo "-- index usage on the new tables:"
psql "$DATABASE_URL" -c "select relname, indexrelname, idx_scan, idx_tup_read from pg_stat_user_indexes where relname like 'service_%' order by relname, indexrelname" | sed 's/^/   /'
echo AUDIT_DONE