"""Background schedulers, watchdogs and thread bootstrap (extracted from app.py).

Data-fetcher loop, snapshot reader, DB/Telegram backup scheduler, health
watchdog, usage-snapshot rollup worker (+ its legacy-table data migration),
pulse scheduler, and the role-aware ``ensure_background_threads_started``.
"""
import concurrent.futures
import json
import os
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import and_, inspect, or_, text

from panel.adapters.xui import persist_detected_panel_type
from panel.core.redis_client import (
    GLOBAL_REFRESH_LOCK,
    GLOBAL_SERVER_DATA,
    load_snapshot_from_redis,
    publish_snapshot_to_redis,
    redis_enabled,
)
from panel.extensions import db
from panel.jobs.messaging import (
    _notification_bot_for_reseller,
    sms_bot_worker,
    sms_status_worker,
    telegram_announcement_worker,
    telegram_depletion_worker,
    whatsapp_bot_worker,
)
from panel.jobs.refresh import (
    _backoff_get,
    _backoff_record_failure,
    _backoff_record_success,
    _backoff_should_skip,
    _check_server_reachable,
    _recompute_global_stats_from_server_statuses,
    _set_snap_progress,
    refresh_queue_worker,
)
from panel.models import (
    Admin,
    HealthLog,
    PulseRun,
    PulseTemplate,
    RenewalEvent,
    Server,
    SystemMigration,
    SystemSetting,
    UsageCounterState,
    UsageDaily,
    UsageHourly,
    get_pulse_settings,
)
from panel.services.backup import (
    TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES,
    TELEGRAM_BACKUP_MAX_INTERVAL_MINUTES,
    _create_database_backup_file,
    _get_system_setting_value,
    _parse_int,
    _parse_iso_datetime,
    _run_telegram_backup,
    _set_system_setting_value,
)

BACKGROUND_THREADS_STARTED = False

def _run_snapshot_with_progress():
    """Background thread: fetch servers one-by-one with progress, then snapshot."""
    from app import _get_panel_ui_lang, app, fetch_worker, get_server_password, process_inbounds  # deferred: app-level helper, avoids circular import
    global _SNAPSHOT_PROGRESS
    is_fa = False
    try:
        with app.app_context():
            is_fa = _get_panel_ui_lang() == 'fa'
    except Exception:
        pass

    def _msg(en, fa):
        return fa if is_fa else en

    try:
        with app.app_context():
            servers = Server.query.filter_by(enabled=True).all()
            total = len(servers)
            _set_snap_progress({
                'step': 0, 'total': total,
                'message': _msg(f'Fetching {total} server(s)…', f'در حال دریافت {total} سرور…'),
                'message_fa': f'در حال دریافت {total} سرور…',
                'fetched_fresh': False,
            })

            cache_was_empty = not bool(GLOBAL_SERVER_DATA.get('inbounds'))

            admin_user = Admin.query.filter(
                or_(Admin.is_superadmin == True, Admin.role == 'superadmin')
            ).first()
            if not admin_user:
                admin_user = SimpleNamespace(role='superadmin', id=0, is_superadmin=True)

            for i, srv in enumerate(servers, 1):
                _set_snap_progress({
                    'step': i,
                    'current_server': srv.name,
                    'message': _msg(
                        f'Fetching server {i}/{total}: {srv.name}',
                        f'دریافت سرور {i} از {total}: {srv.name}',
                    ),
                })
                try:
                    srv_dict = {
                        'id': srv.id, 'name': srv.name, 'host': srv.host,
                        'username': srv.username, 'password': get_server_password(srv),
                        'api_token': srv.api_token,  # v3 Bearer auth (else cookie login → 403)
                        'panel_type': srv.panel_type, 'sub_port': srv.sub_port,
                        'sub_path': srv.sub_path, 'json_path': srv.json_path,
                    }
                    srv_id, inbounds, online_index, status_payload, status_error, error, detected_type = fetch_worker(srv_dict)
                    if not error:
                        if not isinstance(inbounds, list):
                            inbounds = []
                        processed, stats = process_inbounds(inbounds, srv, admin_user, '*', {}, online_index=online_index)
                        existing = GLOBAL_SERVER_DATA.get('inbounds') or []
                        without = [ib for ib in existing if int(ib.get('server_id', -1)) != int(srv.id)]
                        GLOBAL_SERVER_DATA['inbounds'] = without + list(processed or [])
                        GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()
                        publish_snapshot_to_redis([srv.id])
                except Exception:
                    pass  # keep going for other servers

            inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
            if not inbounds:
                _set_snap_progress({
                    'status': 'error',
                    'error': _msg(
                        'Fetched all servers but got no inbounds. Check that servers are online and enabled.',
                        'همه سرورها بررسی شدند اما اینباندی یافت نشد. مطمئن شوید سرورها آنلاین و فعال هستند.',
                    ),
                })
                return

            _set_snap_progress({
                'step': total,
                'message': _msg('Taking usage snapshot…', 'در حال ثبت اسنپ‌شات مصرف…'),
            })
            _take_usage_snapshots()

            inbound_count = len(inbounds)
            _set_snap_progress({
                'status': 'done',
                'inbound_count': inbound_count,
                'fetched_fresh': cache_was_empty,
                'message': _msg(
                    f'Done! Snapshot recorded for {inbound_count} inbound(s).{" (cache was empty — fetched fresh data first)" if cache_was_empty else ""}',
                    f'انجام شد! اسنپ‌شات برای {inbound_count} اینباند ثبت شد.{" (کش خالی بود — ابتدا داده‌ها دریافت شدند)" if cache_was_empty else ""}',
                ),
                'error': None,
            })
    except Exception as exc:
        _set_snap_progress({
            'status': 'error',
            'error': str(exc),
        })











































def inject_version():
    from app import APP_VERSION  # deferred: app-level helper, avoids circular import
    maintenance_notice = None
    try:
        status = get_usage_migration_status()
        if status.get('required'):
            maintenance_notice = status
    except Exception:
        # Requests must remain available while an older schema is bootstrapping.
        db.session.rollback()
    return dict(app_version=APP_VERSION, maintenance_notice=maintenance_notice)






def background_data_fetcher():
    """
    این تابع در پس‌زمینه اجرا می‌شود و هر ۳۰ ثانیه اطلاعات را در RAM بروز می‌کند.
    Fetches from panels, processes, and (if Redis is on) publishes the snapshot.
    """
    from app import app  # deferred: app-level helper, avoids circular import
    ensure_background_threads_started()
    # Warm the dedicated process from the previous Redis snapshot so failed or
    # backoff-skipped panels retain their last good server block.
    try:
        load_snapshot_from_redis(force=True)
    except Exception:
        pass
    while True:
        with app.app_context():
            # Avoid overlapping with a manual refresh job.
            if GLOBAL_REFRESH_LOCK.acquire(blocking=False):
                try:
                    fetch_and_update_global_data(force=False)
                finally:
                    try:
                        GLOBAL_REFRESH_LOCK.release()
                    except Exception:
                        pass
        time.sleep(30)


def snapshot_reader_worker():
    """Runs in workers that DON'T fetch (Redis mode). Pulls the shared snapshot
    from Redis into local memory so requests are served fast & in-process.
    Only decompresses when the snapshot version actually changed."""
    # Prime immediately so the worker has data without waiting a full interval.
    try:
        load_snapshot_from_redis(force=True)
    except Exception:
        pass
    while True:
        try:
            load_snapshot_from_redis()
        except Exception:
            pass
        time.sleep(10)


def fetch_and_update_global_data(force: bool = False, server_ids=None, progress_callback=None):
    """یک بار داده‌ها را از سرورها واکشی و در RAM به‌روزرسانی می‌کند."""
    from app import _utc_iso_now, app, fetch_worker, get_server_password, process_inbounds  # deferred: app-level helper, avoids circular import
    try:
        GLOBAL_SERVER_DATA['is_updating'] = True

        servers_q = Server.query.filter_by(enabled=True).filter(
            (Server.hidden == False) | (Server.hidden == None))
        if server_ids:
            try:
                ids = [int(x) for x in (server_ids or [])]
                servers_q = servers_q.filter(Server.id.in_(ids))
            except Exception:
                pass

        servers = servers_q.all()

        now_ts = time.time()
        skipped_ids = set()
        if not force:
            for s in servers:
                try:
                    if _backoff_should_skip(int(s.id), now_ts):
                        skipped_ids.add(int(s.id))
                except Exception:
                    continue

        server_dicts = [{
            'id': s.id, 'name': s.name, 'host': s.host,
            'username': s.username, 'password': get_server_password(s),
            # Pass the (encrypted) v3 API token through so the concurrent
            # fetch_worker authenticates v3 panels with the Bearer token.
            # Without it server_is_v3() is False → cookie login → 403 on v3.
            'api_token': s.api_token,
            'panel_type': s.panel_type, 'sub_port': s.sub_port,
            'sub_path': s.sub_path, 'json_path': s.json_path
        } for s in servers if int(s.id) not in skipped_ids]

        if progress_callback:
            try:
                progress_callback('started', {
                    'total': len(servers),
                    'servers': [{'id': int(s.id), 'name': s.name, 'state': (
                        'skipped' if int(s.id) in skipped_ids else 'pending')}
                        for s in servers],
                })
            except Exception:
                app.logger.exception('Failed to initialize refresh progress')

        # Release the database read lock before starting long-running network I/O
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        # ── Seed working maps from the current cache so partial updates MERGE ──
        # (a warm refresh keeps every server's data on screen and replaces it
        #  server-by-server; a cold start fills in progressively from empty).
        existing_inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
        existing_by_server = defaultdict(list)
        for inbound in existing_inbounds:
            try:
                sid = int(inbound.get('server_id', -1))
                if sid > 0:
                    existing_by_server[sid].append(inbound)
            except Exception:
                continue

        existing_statuses = GLOBAL_SERVER_DATA.get('servers_status') or []
        status_map = {}
        for st in existing_statuses:
            try:
                if isinstance(st, dict) and 'server_id' in st:
                    status_map[int(st.get('server_id'))] = st
            except Exception:
                continue

        admin_user = Admin.query.filter(or_(Admin.is_superadmin == True, Admin.role == 'superadmin')).first()
        if not admin_user:
            admin_user = SimpleNamespace(role='superadmin', id=0, is_superadmin=True)

        now_iso = _utc_iso_now()
        new_by_server = dict(existing_by_server)
        servers_by_id = {int(s.id): s for s in servers}
        server_order = [int(s.id) for s in servers]

        def _commit_snapshot():
            """Publish the current (possibly partial) state to GLOBAL_SERVER_DATA
            so the dashboard renders servers as they finish instead of blocking on
            the slowest panel. Cheap relative to the network fetches it follows."""
            flat = []
            for _sid in server_order:
                flat.extend(new_by_server.get(_sid, []))
            statuses = [status_map.get(_sid) or {"server_id": _sid, "success": False, "error": "No data"}
                        for _sid in server_order]
            GLOBAL_SERVER_DATA['inbounds'] = flat
            GLOBAL_SERVER_DATA['stats'] = _recompute_global_stats_from_server_statuses(statuses)
            GLOBAL_SERVER_DATA['servers_status'] = statuses
            GLOBAL_SERVER_DATA['last_update'] = _utc_iso_now()

        def _apply_result(sid, res):
            _, inbounds, online_index, status_payload, status_error, error, detected_type = res
            if error:
                _backoff_record_failure(sid, error)
                st = status_map.get(sid) or {"server_id": sid}
                # Keep cached stats if present to avoid UI dropping counts.
                if isinstance(st.get('stats'), dict) and st.get('stats'):
                    st['success'] = True
                else:
                    st['success'] = False
                st['error'] = error
                st['reachable'] = False
                st['reachable_error'] = error
                st['reachable_checked_at'] = now_iso
                st['panel_status_error'] = (status_error or error)
                st['panel_status_checked_at'] = now_iso
                if status_payload:
                    st['xui_version'] = status_payload.get('xui_version')
                    st['xray_version'] = status_payload.get('xray_version')
                    st['xray_state'] = status_payload.get('xray_state')
                    st['xray_core'] = status_payload.get('xray_core')
                    st['online_count'] = status_payload.get('online_count')
                status_map[sid] = st
                return  # keep existing inbounds block (if any)

            _backoff_record_success(sid)
            srv = servers_by_id.get(sid)
            if srv is not None and persist_detected_panel_type(srv, detected_type):
                app.logger.info("Detected panel type for server %s as %s", sid, detected_type)
            if not isinstance(inbounds, list):
                inbounds = []
            processed, stats = process_inbounds(inbounds, srv, admin_user, '*', {}, online_index=online_index)
            new_by_server[sid] = list(processed or [])

            st = status_map.get(sid) or {"server_id": sid}
            status_payload = status_payload or {}
            st.update({
                "server_id": sid,
                "success": True,
                "stats": stats,
                "panel_type": getattr(srv, 'panel_type', None),
                "reachable": True,
                "reachable_error": None,
                "reachable_checked_at": now_iso,
                "error": None,
                "xui_version": status_payload.get('xui_version'),
                "xray_version": status_payload.get('xray_version'),
                "xray_state": status_payload.get('xray_state'),
                "xray_core": status_payload.get('xray_core'),
                "online_count": status_payload.get('online_count'),
                "panel_status_error": status_error if status_error else None,
                "panel_status_checked_at": now_iso
            })
            status_map[sid] = st

        # Backoff-skipped servers: mark unreachable up-front (keep their cached inbounds).
        for sid in skipped_ids:
            info = _backoff_get(sid)
            st = status_map.get(sid) or {"server_id": sid}
            st['reachable'] = False
            st['reachable_error'] = (info.get('last_error') or 'Backoff')
            st['reachable_checked_at'] = now_iso
            st['backoff_until'] = int(info.get('next_allowed_at', 0) or 0)
            status_map[sid] = st

        # Publish the seed/skip state immediately so a cold load shows structure fast.
        _commit_snapshot()

        # Fetch concurrently and commit after EACH server completes → the grid
        # fills in progressively instead of waiting for the whole fan-out.
        pending_ids = {int(s['id']) for s in server_dicts}
        last_publish = 0.0
        dirty_server_ids = set()
        if server_dicts:
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_id = {executor.submit(fetch_worker, s): int(s['id']) for s in server_dicts}
                for future in concurrent.futures.as_completed(future_to_id):
                    sid = future_to_id[future]
                    pending_ids.discard(sid)
                    try:
                        res = future.result()
                        if not (isinstance(res, tuple) and len(res) >= 7):
                            res = (sid, None, None, None, None, "Timeout", 'auto')
                    except Exception as e:
                        res = (sid, None, None, None, None, str(e) or "Timeout", 'auto')
                    try:
                        _apply_result(sid, res)
                    except Exception:
                        app.logger.exception("Failed to apply fetch result for server %s", sid)
                    _commit_snapshot()
                    if progress_callback:
                        try:
                            srv = servers_by_id.get(sid)
                            progress_callback('server_done', {
                                'id': sid,
                                'name': getattr(srv, 'name', None) or f'Server {sid}',
                                'state': 'error' if res[5] else 'success',
                                'error': res[5] or None,
                            })
                        except Exception:
                            app.logger.exception('Failed to store refresh progress for server %s', sid)
                    dirty_server_ids.add(sid)
                    nowt = time.time()
                    if nowt - last_publish >= 1.0:
                        publish_snapshot_to_redis(dirty_server_ids)
                        dirty_server_ids.clear()
                        last_publish = nowt

        # Defensive: any server that never produced a result → timeout entry.
        for sid in list(pending_ids):
            _apply_result(sid, (sid, None, None, None, None, "Timeout", 'auto'))

        # Final authoritative commit + publish to the other workers (no-op w/o Redis).
        _commit_snapshot()
        publish_snapshot_to_redis(dirty_server_ids)

    except Exception as e:
        app.logger.error("Background fetch error: %s", e)
    finally:
        GLOBAL_SERVER_DATA['is_updating'] = False

def run_scheduler():
    from app import _cleanup_old_backups, _get_app_tzinfo, _parse_bool, app  # deferred: app-level helper, avoids circular import
    while True:
        with app.app_context():
            try:
                freq_setting = db.session.get(SystemSetting, 'backup_frequency')
                if freq_setting and freq_setting.value != 'disabled':
                    last_backup = db.session.get(SystemSetting, 'last_auto_backup')
                    
                    should_backup = False
                    now = datetime.now()
                    
                    if not last_backup:
                        should_backup = True
                    else:
                        last_time = datetime.fromisoformat(last_backup.value)
                        if freq_setting.value == 'daily' and (now - last_time) > timedelta(days=1):
                            should_backup = True
                        elif freq_setting.value == 'weekly' and (now - last_time) > timedelta(weeks=1):
                            should_backup = True
                        elif freq_setting.value == 'monthly' and (now - last_time) > timedelta(days=30):
                            should_backup = True
                            
                    if should_backup:
                        try:
                            filename = _create_database_backup_file('auto')
                        except Exception as e:
                            app.logger.error("Auto backup failed: %s", e)
                            filename = None

                        if filename:
                            # Update last backup time
                            if not last_backup:
                                last_backup = SystemSetting(key='last_auto_backup', value=now.isoformat())
                                db.session.add(last_backup)
                            else:
                                last_backup.value = now.isoformat()
                            db.session.commit()
                            app.logger.info("Auto backup created: %s", filename)

                # Backup retention cleanup (delete files older than N days)
                try:
                    if _parse_bool(_get_system_setting_value('backup_retention_enabled', 'false')):
                        rdays = _parse_int(_get_system_setting_value('backup_retention_days', '14'), 14, min_value=1, max_value=3650)
                        last_clean = _parse_iso_datetime(_get_system_setting_value('backup_last_cleanup', ''))
                        # run at most once every 6 hours
                        if (not last_clean) or (datetime.utcnow() - last_clean) >= timedelta(hours=6):
                            res = _cleanup_old_backups(rdays)
                            _set_system_setting_value('backup_last_cleanup', datetime.utcnow().isoformat())
                            db.session.commit()
                            if res.get('deleted'):
                                app.logger.info("[Backup retention] Deleted %s old backup(s), freed %s bytes", res['deleted'], res['freed_bytes'])
                except Exception as _ce:
                    app.logger.error("Backup retention error: %s", _ce)

                # Telegram backups
                tg_enabled = _parse_bool(_get_system_setting_value('telegram_backup_enabled', 'false'))
                if tg_enabled:
                    # Normalize any parsed datetime to naive-UTC for safe arithmetic
                    def _naive_utc(dt):
                        if not dt:
                            return None
                        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt

                    schedule_mode = (_get_system_setting_value('telegram_backup_schedule_mode', 'interval') or 'interval').strip().lower()
                    now_utc = datetime.utcnow()
                    last_run_dt = _naive_utc(_parse_iso_datetime(_get_system_setting_value('telegram_backup_last_run', '')))
                    last_attempt_dt = _naive_utc(_parse_iso_datetime(_get_system_setting_value('telegram_backup_last_attempt', '')))
                    # Retry throttle: never re-attempt more than once per 15 min on failure
                    attempt_ok = (not last_attempt_dt) or ((now_utc - last_attempt_dt) >= timedelta(minutes=15))
                    should_run = False

                    if schedule_mode == 'daily':
                        # Fire once per day at a fixed local time (server timezone).
                        daily_time = (_get_system_setting_value('telegram_backup_daily_time', '00:00') or '00:00').strip()
                        try:
                            th, tm = (int(x) for x in daily_time.split(':'))
                        except Exception:
                            th, tm = 0, 0
                        try:
                            tz_local = _get_app_tzinfo()
                        except Exception:
                            tz_local = timezone(timedelta(hours=3, minutes=30))
                        now_local = datetime.now(tz_local)
                        target_today = now_local.replace(hour=th, minute=tm, second=0, microsecond=0)
                        last_local = None
                        if last_run_dt:
                            last_local = last_run_dt.replace(tzinfo=timezone.utc).astimezone(tz_local)
                        already_done_today = bool(last_local and last_local.date() == now_local.date()
                                                  and last_local >= target_today)
                        if now_local >= target_today and not already_done_today and attempt_ok:
                            should_run = True
                    else:
                        tg_interval = _parse_int(
                            _get_system_setting_value('telegram_backup_interval_minutes', str(TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES)),
                            TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES,
                            min_value=1,
                            max_value=TELEGRAM_BACKUP_MAX_INTERVAL_MINUTES
                        )
                        if not last_run_dt:
                            should_run = attempt_ok
                        elif (now_utc - last_run_dt) >= timedelta(minutes=tg_interval) and attempt_ok:
                            should_run = True

                    if should_run:
                        # Record attempt time first so a failure doesn't trigger a
                        # per-minute retry storm (next try is throttled to +15 min).
                        try:
                            _set_system_setting_value('telegram_backup_last_attempt', datetime.utcnow().isoformat())
                            db.session.commit()
                        except Exception:
                            pass
                        result = _run_telegram_backup(trigger='scheduled')
                        if not result.get('success'):
                            app.logger.warning("Telegram backup failed: %s", result.get('error'))
                            
            except Exception as e:
                app.logger.error("Scheduler error: %s", e)
            
        time.sleep(60) # Check every minute

def update_session_lifetime():
    from app import app  # deferred: app-level helper, avoids circular import
    with app.app_context():
        try:
            # Check if table exists first to avoid error on fresh install
            inspector = inspect(db.engine)
            if 'system_settings' in inspector.get_table_names():
                setting = db.session.get(SystemSetting, 'session_timeout_hours')
                if setting:
                    hours = int(setting.value)
                    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=hours)
                    app.logger.info("Session lifetime updated to %s hours", hours)
        except Exception as e:
            app.logger.error("Error updating session lifetime: %s", e)


# ---------------------------------------------------------------------------
# Health Watchdog – background self-healing loop
# ---------------------------------------------------------------------------
HEALTH_CHECK_INTERVAL = 60  # seconds between checks

# Critical static files that must exist
_CRITICAL_STATIC_FILES = [
    'style.css',
    'tailwind.generated.css',
    'jquery-3.6.0.min.js',
    'jalalidatepicker.min.css',
    'jalalidatepicker.min.js',
]

def _health_check_db():
    """Verify DB connectivity. Auto-heal by recycling the connection pool."""
    from app import _add_health_log  # deferred: app-level helper, avoids circular import
    try:
        db.session.execute(text('SELECT 1'))
        db.session.rollback()
        return True, None
    except Exception as exc:
        error_msg = str(exc)
        # Auto-heal: dispose the pool so new connections are created
        try:
            db.session.rollback()
            db.engine.dispose()
            _add_health_log('warning', 'db', 'Database connection lost – pool recycled',
                            action_taken='Disposed connection pool and recycled',
                            details={'error': error_msg}, resolved=True)
            return False, error_msg
        except Exception as heal_exc:
            _add_health_log('critical', 'db', f'Database unreachable and auto-heal failed: {error_msg}',
                            action_taken=f'Heal attempt failed: {heal_exc}',
                            details={'error': error_msg, 'heal_error': str(heal_exc)})
            return False, error_msg


def _project_root():
    """Repo/app root (panel/jobs/ -> panel/ -> root)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _health_check_static_files():
    """Ensure critical static files exist on disk."""
    from app import _add_health_log  # deferred: app-level helper, avoids circular import
    static_dir = os.path.join(_project_root(), 'static')
    missing = []
    for fname in _CRITICAL_STATIC_FILES:
        fpath = os.path.join(static_dir, fname)
        if not os.path.isfile(fpath):
            missing.append(fname)
    if missing:
        _add_health_log('error', 'static',
                        f'Missing critical static files: {", ".join(missing)}',
                        details={'missing': missing})
        return False, missing
    return True, None


def _health_check_disk():
    """Warn if disk usage is above 90%."""
    from app import _add_health_log  # deferred: app-level helper, avoids circular import
    try:
        usage = shutil.disk_usage(_project_root())
        pct = (usage.used / usage.total) * 100
        if pct > 95:
            _add_health_log('critical', 'disk',
                            f'Disk nearly full: {pct:.1f}% used',
                            details={'used_pct': round(pct, 1),
                                     'free_gb': round(usage.free / (1024**3), 2)})
            return False, pct
        elif pct > 90:
            _add_health_log('warning', 'disk',
                            f'Disk usage high: {pct:.1f}% used',
                            details={'used_pct': round(pct, 1),
                                     'free_gb': round(usage.free / (1024**3), 2)})
            return False, pct
        return True, round(pct, 1)
    except Exception as exc:
        return True, str(exc)  # non-fatal


def _health_check_servers():
    """Check reachability of enabled servers, log any that are down."""
    from app import _add_health_log  # deferred: app-level helper, avoids circular import
    try:
        servers = Server.query.filter_by(enabled=True).all()
    except Exception:
        return True, None  # if we can't query, the DB check will catch it
    down_servers = []
    for srv in servers:
        ok, err = _check_server_reachable(srv, timeout_sec=3.0)
        if not ok:
            down_servers.append({'id': srv.id, 'name': srv.name or srv.host, 'error': err})
    if down_servers:
        names = ', '.join(s['name'] for s in down_servers)
        _add_health_log('warning', 'server',
                        f'{len(down_servers)} server(s) unreachable: {names}',
                        details={'servers': down_servers})
        return False, down_servers
    return True, None


def _run_single_health_cycle():
    """Execute one full health-check cycle. Returns summary dict."""
    results = {}
    results['db'] = _health_check_db()
    results['static'] = _health_check_static_files()
    results['disk'] = _health_check_disk()
    results['servers'] = _health_check_servers()
    return results


def health_watchdog():
    """Long-running watchdog daemon – runs health checks every HEALTH_CHECK_INTERVAL seconds."""
    from app import _add_health_log, app  # deferred: app-level helper, avoids circular import
    with app.app_context():
        _add_health_log('info', 'general', 'Health watchdog started',
                        details={'interval_seconds': HEALTH_CHECK_INTERVAL})
        while True:
            try:
                time.sleep(HEALTH_CHECK_INTERVAL)
                _run_single_health_cycle()
                # Prune old logs – keep last 500
                try:
                    count = HealthLog.query.count()
                    if count > 500:
                        cutoff = (HealthLog.query
                                  .order_by(HealthLog.id.desc())
                                  .offset(500)
                                  .first())
                        if cutoff:
                            HealthLog.query.filter(HealthLog.id <= cutoff.id).delete()
                            db.session.commit()
                except Exception:
                    db.session.rollback()
            except Exception as exc:
                app.logger.error("[HealthWatchdog] Error in cycle: %s", exc)
                try:
                    time.sleep(30)
                except Exception:
                    pass


_USAGE_HOURLY_RETENTION_HOURS = 48
_USAGE_DAILY_RETENTION_DAYS = 365
_USAGE_LEGACY_MIGRATION_KEY = 'usage_rollup_migration_v1'
_USAGE_LEGACY_MIGRATION_ID = 'usage-snapshots-to-rollups-v1'
_USAGE_TEHRAN_OFFSET = timedelta(hours=3, minutes=30)


def _usage_tehran_date(value: datetime):
    return (value + _USAGE_TEHRAN_OFFSET).date()


def _seconds_until_next_usage_hour(value=None):
    local = (value or datetime.utcnow()) + _USAGE_TEHRAN_OFFSET
    next_hour = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(1, int((next_hour - local).total_seconds()))


def _coerce_usage_datetime(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=None)


def _usage_account_points(inbounds):
    """Collapse v3's repeated account across inbounds to one canonical counter."""
    points = {}
    for inbound in inbounds or []:
        try:
            server_id = int(inbound.get('server_id'))
        except (TypeError, ValueError):
            continue
        tag = (inbound.get('remark') or inbound.get('tag') or '').strip() or f"inbound-{inbound.get('id', '')}"
        for client in inbound.get('clients') or []:
            sub_id = str(client.get('subId') or client.get('id') or '').strip()
            if not sub_id:
                continue
            up = max(0, int(client.get('up') or 0))
            down = max(0, int(client.get('down') or 0))
            total = up + down
            try:
                limit = int(client.get('totalGB') or 0) or None
            except (TypeError, ValueError):
                limit = None
            if limit and limit > 9_223_372_036_854_775_807:
                limit = None
            point = {
                'server_id': server_id, 'sub_id': sub_id, 'inbound_tag': tag,
                'upload_bytes': up, 'download_bytes': down, 'total_bytes': total,
                'remaining_bytes': max(limit - total, 0) if limit else None,
                'volume_limit_bytes': limit, 'client': client,
            }
            key = (server_id, sub_id)
            previous = points.get(key)
            if previous is None or (total, tag) > (previous['total_bytes'], previous['inbound_tag']):
                points[key] = point
    return points


def _usage_delta(current, previous):
    current = max(0, int(current or 0))
    previous = max(0, int(previous or 0))
    return current - previous if current >= previous else current


def _renewal_from_counter_reset(point, now):
    client = point.get('client') or {}
    try:
        expiry_ts = int(client.get('expiryTimestamp') or client.get('expiryTime') or 0)
    except (TypeError, ValueError):
        expiry_ts = 0
    days = None
    unlimited_time = False
    if expiry_ts > 0:
        days = max((datetime.utcfromtimestamp(expiry_ts / 1000) - now).days, 0)
    elif expiry_ts < 0:
        days = max(int(round(abs(expiry_ts) / 86400000.0)), 0)
    else:
        unlimited_time = True
    return RenewalEvent(
        server_id=point['server_id'], sub_id=point['sub_id'], renewed_at=now,
        volume_bytes=point['volume_limit_bytes'], days=days,
        is_unlimited_volume=(point['volume_limit_bytes'] is None),
        is_unlimited_time=unlimited_time,
    )


def _collect_usage_rollups():
    """Update bounded hourly/daily aggregates without appending raw samples."""
    from app import app  # deferred: avoids circular import
    inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    if not inbounds:
        app.logger.info('[UsageRollup] cache empty; skipping collection')
        return False
    now = datetime.utcnow()
    bucket_at = now.replace(minute=0, second=0, microsecond=0)
    usage_date = _usage_tehran_date(now)
    points = _usage_account_points(inbounds)
    if not points:
        return False

    if db.engine.dialect.name == 'postgresql':
        locked = db.session.execute(text('SELECT pg_try_advisory_xact_lock(73519041)')).scalar()
        if not locked:
            db.session.rollback()
            return False

    states = {(r.server_id, r.sub_id): r for r in UsageCounterState.query.all()}
    hourly = {(r.server_id, r.sub_id): r for r in UsageHourly.query.filter_by(bucket_at=bucket_at).all()}
    daily = {(r.server_id, r.sub_id): r for r in UsageDaily.query.filter_by(usage_date=usage_date).all()}
    renewal_count = 0

    for key, point in points.items():
        state = states.get(key)
        if state is None:
            state = UsageCounterState(
                server_id=point['server_id'], sub_id=point['sub_id'],
                inbound_tag=point['inbound_tag'], upload_bytes=point['upload_bytes'],
                download_bytes=point['download_bytes'], total_bytes=point['total_bytes'],
                remaining_bytes=point['remaining_bytes'], volume_limit_bytes=point['volume_limit_bytes'],
                observed_at=now,
            )
            db.session.add(state)
            states[key] = state
            delta_up = delta_down = 0
            opening_up, opening_down = point['upload_bytes'], point['download_bytes']
        else:
            delta_up = _usage_delta(point['upload_bytes'], state.upload_bytes)
            delta_down = _usage_delta(point['download_bytes'], state.download_bytes)
            opening_up, opening_down = state.upload_bytes, state.download_bytes
            if point['total_bytes'] < int(state.total_bytes or 0) and int(state.total_bytes or 0) > 0:
                db.session.add(_renewal_from_counter_reset(point, now))
                renewal_count += 1

        hour = hourly.get(key)
        if hour is None:
            hour = UsageHourly(
                server_id=point['server_id'], sub_id=point['sub_id'], inbound_tag=point['inbound_tag'],
                bucket_at=bucket_at, upload_bytes=delta_up, download_bytes=delta_down,
                remaining_bytes=point['remaining_bytes'], volume_limit_bytes=point['volume_limit_bytes'],
                sample_count=1, updated_at=now,
            )
            db.session.add(hour)
            hourly[key] = hour
        else:
            hour.upload_bytes += delta_up
            hour.download_bytes += delta_down
            hour.inbound_tag = point['inbound_tag']
            hour.remaining_bytes = point['remaining_bytes']
            hour.volume_limit_bytes = point['volume_limit_bytes']
            hour.sample_count += 1
            hour.updated_at = now

        day = daily.get(key)
        if day is None:
            day = UsageDaily(
                server_id=point['server_id'], sub_id=point['sub_id'], inbound_tag=point['inbound_tag'],
                usage_date=usage_date, upload_bytes=delta_up, download_bytes=delta_down,
                opening_upload_bytes=opening_up, opening_download_bytes=opening_down,
                closing_upload_bytes=point['upload_bytes'], closing_download_bytes=point['download_bytes'],
                remaining_bytes=point['remaining_bytes'], volume_limit_bytes=point['volume_limit_bytes'],
                sample_count=1, first_observed_at=now, last_observed_at=now,
            )
            db.session.add(day)
            daily[key] = day
        else:
            day.upload_bytes += delta_up
            day.download_bytes += delta_down
            day.inbound_tag = point['inbound_tag']
            day.closing_upload_bytes = point['upload_bytes']
            day.closing_download_bytes = point['download_bytes']
            day.remaining_bytes = point['remaining_bytes']
            day.volume_limit_bytes = point['volume_limit_bytes']
            day.sample_count += 1
            day.last_observed_at = now

        state.inbound_tag = point['inbound_tag']
        state.upload_bytes = point['upload_bytes']
        state.download_bytes = point['download_bytes']
        state.total_bytes = point['total_bytes']
        state.remaining_bytes = point['remaining_bytes']
        state.volume_limit_bytes = point['volume_limit_bytes']
        state.observed_at = now

    UsageHourly.query.filter(UsageHourly.bucket_at < now - timedelta(hours=_USAGE_HOURLY_RETENTION_HOURS)).delete(synchronize_session=False)
    UsageDaily.query.filter(UsageDaily.usage_date < usage_date - timedelta(days=_USAGE_DAILY_RETENTION_DAYS)).delete(synchronize_session=False)
    UsageCounterState.query.filter(UsageCounterState.observed_at < now - timedelta(days=30)).delete(synchronize_session=False)
    db.session.commit()
    app.logger.info('[UsageRollup] updated accounts=%s renewals=%s', len(points), renewal_count)
    return True


def _take_usage_snapshots():
    return _collect_usage_rollups()


def _legacy_usage_table_name():
    try:
        tables = set(inspect(db.engine).get_table_names())
        if 'usage_snapshots' in tables:
            return 'usage_snapshots'
        if 'usage_snapshots_legacy' in tables:
            return 'usage_snapshots_legacy'
    except Exception:
        pass
    return None


def _legacy_usage_table_exists():
    return bool(_legacy_usage_table_name())


def _usage_migration_record(create=True):
    record = SystemMigration.query.filter_by(migration_id=_USAGE_LEGACY_MIGRATION_ID).first()
    if record is None and create:
        record = SystemMigration(migration_id=_USAGE_LEGACY_MIGRATION_ID, status='pending', phase='preflight')
        db.session.add(record)
        db.session.commit()
    return record


def get_usage_migration_status():
    """Return a JSON-safe status for the maintenance CLI and diagnostics."""
    table_name = _legacy_usage_table_name()
    record = _usage_migration_record(create=False)
    return {
        'migrationId': _USAGE_LEGACY_MIGRATION_ID,
        'required': bool(table_name),
        'sourceTable': table_name,
        'status': record.status if record else ('pending' if table_name else 'complete'),
        'phase': record.phase if record else None,
        'processedRows': int(record.processed_rows or 0) if record else 0,
        'totalRows': int(record.total_rows) if record and record.total_rows is not None else None,
        'lastError': record.last_error if record else None,
        'startedAt': record.started_at.isoformat() if record and record.started_at else None,
        'updatedAt': record.updated_at.isoformat() if record and record.updated_at else None,
        'finishedAt': record.finished_at.isoformat() if record and record.finished_at else None,
    }


def _migrate_legacy_usage_snapshots_v248():
    """Stream raw legacy rows into rollups, then remove the obsolete table."""
    from app import app  # deferred: avoids circular import
    if not _legacy_usage_table_exists():
        _set_system_setting_value(_USAGE_LEGACY_MIGRATION_KEY, 'complete')
        db.session.commit()
        return
    app.logger.info('[UsageRollup] migrating legacy usage_snapshots...')
    _set_system_setting_value(_USAGE_LEGACY_MIGRATION_KEY, 'running')
    UsageHourly.query.delete(synchronize_session=False)
    UsageDaily.query.delete(synchronize_session=False)
    UsageCounterState.query.delete(synchronize_session=False)
    db.session.commit()

    sql = text('''
        SELECT server_id, sub_id, inbound_tag, recorded_at,
               upload_bytes, download_bytes, total_bytes,
               remaining_bytes, volume_limit_bytes
        FROM usage_snapshots
        ORDER BY server_id, sub_id, recorded_at, total_bytes DESC, id DESC
    ''')
    now = datetime.utcnow()
    hourly_cutoff = now - timedelta(hours=_USAGE_HOURLY_RETENTION_HOURS)
    daily_batch, hourly_batch, state_batch = [], [], []
    current_key = current_ts = None
    prev_up = prev_down = None
    day_acc = hour_acc = last_point = None
    processed = 0

    def flush_batches(force=False):
        wrote = False
        for batch in (daily_batch, hourly_batch, state_batch):
            if batch and (force or len(batch) >= 1000):
                db.session.bulk_save_objects(batch)
                batch.clear()
                wrote = True
        if wrote:
            db.session.commit()

    def finish_hour():
        nonlocal hour_acc
        if hour_acc:
            hourly_batch.append(UsageHourly(**hour_acc))
            hour_acc = None

    def finish_day():
        nonlocal day_acc
        if day_acc:
            daily_batch.append(UsageDaily(**day_acc))
            day_acc = None

    def finish_account():
        if current_key is None:
            return
        finish_hour()
        finish_day()
        if last_point:
            state_batch.append(UsageCounterState(
                server_id=current_key[0], sub_id=current_key[1], inbound_tag=last_point['tag'],
                upload_bytes=last_point['up'], download_bytes=last_point['down'],
                total_bytes=last_point['up'] + last_point['down'],
                remaining_bytes=last_point['remaining'], volume_limit_bytes=last_point['limit'],
                observed_at=last_point['ts'],
            ))
        flush_batches()

    with db.engine.connect().execution_options(stream_results=True) as conn:
        result = conn.execute(sql)
        while True:
            rows = result.fetchmany(5000)
            if not rows:
                break
            processed += len(rows)
            if processed % 1_000_000 < len(rows):
                app.logger.info('[UsageRollup] migrated %s legacy rows', f'{processed:,}')
            for row in rows:
                key = (int(row[0]), str(row[1]))
                ts = _coerce_usage_datetime(row[3])
                if key != current_key:
                    finish_account()
                    current_key, current_ts = key, None
                    prev_up = prev_down = None
                    day_acc = hour_acc = last_point = None
                if ts == current_ts:
                    continue
                current_ts = ts
                up, down = int(row[4] or 0), int(row[5] or 0)
                delta_up = _usage_delta(up, prev_up) if prev_up is not None else 0
                delta_down = _usage_delta(down, prev_down) if prev_down is not None else 0
                day = _usage_tehran_date(ts)
                hour = ts.replace(minute=0, second=0, microsecond=0)
                tag = row[2]

                if day_acc is None or day_acc['usage_date'] != day:
                    finish_day()
                    day_acc = dict(
                        server_id=key[0], sub_id=key[1], inbound_tag=tag, usage_date=day,
                        upload_bytes=0, download_bytes=0,
                        opening_upload_bytes=(prev_up if prev_up is not None else up),
                        opening_download_bytes=(prev_down if prev_down is not None else down),
                        closing_upload_bytes=up, closing_download_bytes=down,
                        remaining_bytes=row[7], volume_limit_bytes=row[8], sample_count=0,
                        first_observed_at=ts, last_observed_at=ts,
                    )
                day_acc['upload_bytes'] += delta_up
                day_acc['download_bytes'] += delta_down
                day_acc['closing_upload_bytes'] = up
                day_acc['closing_download_bytes'] = down
                day_acc['remaining_bytes'] = row[7]
                day_acc['volume_limit_bytes'] = row[8]
                day_acc['inbound_tag'] = tag
                day_acc['sample_count'] += 1
                day_acc['last_observed_at'] = ts

                if ts >= hourly_cutoff:
                    if hour_acc is None or hour_acc['bucket_at'] != hour:
                        finish_hour()
                        hour_acc = dict(
                            server_id=key[0], sub_id=key[1], inbound_tag=tag, bucket_at=hour,
                            upload_bytes=0, download_bytes=0, remaining_bytes=row[7],
                            volume_limit_bytes=row[8], sample_count=0, updated_at=ts,
                        )
                    hour_acc['upload_bytes'] += delta_up
                    hour_acc['download_bytes'] += delta_down
                    hour_acc['remaining_bytes'] = row[7]
                    hour_acc['volume_limit_bytes'] = row[8]
                    hour_acc['inbound_tag'] = tag
                    hour_acc['sample_count'] += 1
                    hour_acc['updated_at'] = ts

                prev_up, prev_down = up, down
                last_point = {'tag': tag, 'up': up, 'down': down, 'remaining': row[7], 'limit': row[8], 'ts': ts}
    finish_account()
    flush_batches(force=True)
    with db.engine.begin() as conn:
        conn.execute(text('DROP TABLE usage_snapshots'))
    _set_system_setting_value(_USAGE_LEGACY_MIGRATION_KEY, 'complete')
    db.session.commit()
    app.logger.info('[UsageRollup] legacy migration complete; raw table dropped')


def _migrate_legacy_usage_snapshots(finalize=True, batch_accounts=10):
    """Convert raw snapshots in resumable batches, validate, then reclaim them."""
    from app import app  # deferred: avoids circular import
    source_table = _legacy_usage_table_name()
    if not source_table:
        _set_system_setting_value(_USAGE_LEGACY_MIGRATION_KEY, 'complete')
        record = _usage_migration_record(create=False)
        if record and record.status != 'complete':
            record.status = 'complete'
            record.phase = 'cleanup_complete'
            record.finished_at = datetime.utcnow()
        db.session.commit()
        return get_usage_migration_status()

    lock_fd = None
    try:
        import fcntl
        lock_fd = open('/tmp/eve-usage-migration.lock', 'a+')
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            app.logger.info('[UsageRollup] migration already running in another process')
            lock_fd.close()
            return get_usage_migration_status()
    except (ImportError, OSError):
        if lock_fd:
            lock_fd.close()
        lock_fd = None

    record = _usage_migration_record()
    try:
        try:
            cursor = json.loads(record.cursor_json or '{}')
        except Exception:
            cursor = {}
        cursor_server = cursor.get('server_id')
        cursor_sub = cursor.get('sub_id')

        if not cursor and not int(record.processed_rows or 0):
            # A pre-ledger 2.4.8 attempt may have left partial targets behind.
            UsageHourly.query.delete(synchronize_session=False)
            UsageDaily.query.delete(synchronize_session=False)
            UsageCounterState.query.delete(synchronize_session=False)
            record.total_rows = int(db.session.execute(
                text(f'SELECT COUNT(*) FROM {source_table}')
            ).scalar() or 0)
            db.session.commit()

        record.status = 'running'
        record.phase = 'backfill'
        record.started_at = record.started_at or datetime.utcnow()
        record.last_error = None
        _set_system_setting_value(_USAGE_LEGACY_MIGRATION_KEY, 'running')
        db.session.commit()
        app.logger.info('[UsageRollup] migrating %s; resume=%s', source_table, cursor or "start")

        hourly_cutoff = datetime.utcnow() - timedelta(hours=_USAGE_HOURLY_RETENTION_HOURS)
        daily_cutoff = _usage_tehran_date(datetime.utcnow()) - timedelta(days=_USAGE_DAILY_RETENTION_DAYS)
        while True:
            params = {'limit': max(1, min(int(batch_accounts), 1000))}
            where = ''
            if cursor_server is not None:
                where = ('WHERE (server_id > :cursor_server OR '
                         '(server_id = :cursor_server AND sub_id > :cursor_sub))')
                params.update(cursor_server=cursor_server, cursor_sub=cursor_sub)
            keys = db.session.execute(text(f'''
                SELECT server_id, sub_id FROM {source_table} {where}
                GROUP BY server_id, sub_id
                ORDER BY server_id, sub_id
                LIMIT :limit
            '''), params).fetchall()
            if not keys:
                break

            last_server, last_sub = int(keys[-1][0]), str(keys[-1][1])
            range_params = {'last_server': last_server, 'last_sub': last_sub}
            lower = ''
            if cursor_server is not None:
                lower = ('AND (server_id > :cursor_server OR '
                         '(server_id = :cursor_server AND sub_id > :cursor_sub))')
                range_params.update(cursor_server=cursor_server, cursor_sub=cursor_sub)
            rows = db.session.execute(text(f'''
                SELECT server_id, sub_id, inbound_tag, recorded_at,
                       upload_bytes, download_bytes, total_bytes,
                       remaining_bytes, volume_limit_bytes
                FROM {source_table}
                WHERE (server_id < :last_server OR
                       (server_id = :last_server AND sub_id <= :last_sub))
                  {lower}
                ORDER BY server_id, sub_id, recorded_at, total_bytes DESC, id DESC
            '''), range_params).fetchall()

            for model in (UsageHourly, UsageDaily, UsageCounterState):
                conditions = [and_(model.server_id == int(k[0]), model.sub_id == str(k[1])) for k in keys]
                model.query.filter(or_(*conditions)).delete(synchronize_session=False)

            daily_batch, hourly_batch, state_batch = [], [], []
            current_key = current_ts = None
            prev_up = prev_down = None
            day_acc = hour_acc = last_point = None

            def finish_hour():
                nonlocal hour_acc
                if hour_acc:
                    hourly_batch.append(UsageHourly(**hour_acc))
                    hour_acc = None

            def finish_day():
                nonlocal day_acc
                if day_acc:
                    daily_batch.append(UsageDaily(**day_acc))
                    day_acc = None

            def finish_account():
                if current_key is None:
                    return
                finish_hour()
                finish_day()
                if last_point:
                    state_batch.append(UsageCounterState(
                        server_id=current_key[0], sub_id=current_key[1], inbound_tag=last_point['tag'],
                        upload_bytes=last_point['up'], download_bytes=last_point['down'],
                        total_bytes=last_point['up'] + last_point['down'],
                        remaining_bytes=last_point['remaining'], volume_limit_bytes=last_point['limit'],
                        observed_at=last_point['ts'],
                    ))

            for row in rows:
                key = (int(row[0]), str(row[1]))
                ts = _coerce_usage_datetime(row[3])
                if key != current_key:
                    finish_account()
                    current_key, current_ts = key, None
                    prev_up = prev_down = None
                    day_acc = hour_acc = last_point = None
                if ts == current_ts:
                    continue
                current_ts = ts
                up, down = int(row[4] or 0), int(row[5] or 0)
                delta_up = _usage_delta(up, prev_up) if prev_up is not None else 0
                delta_down = _usage_delta(down, prev_down) if prev_down is not None else 0
                day = _usage_tehran_date(ts)
                hour = ts.replace(minute=0, second=0, microsecond=0)
                tag = row[2]

                if day >= daily_cutoff:
                    if day_acc is None or day_acc['usage_date'] != day:
                        finish_day()
                        day_acc = dict(
                            server_id=key[0], sub_id=key[1], inbound_tag=tag, usage_date=day,
                            upload_bytes=0, download_bytes=0,
                            opening_upload_bytes=(prev_up if prev_up is not None else up),
                            opening_download_bytes=(prev_down if prev_down is not None else down),
                            closing_upload_bytes=up, closing_download_bytes=down,
                            remaining_bytes=row[7], volume_limit_bytes=row[8], sample_count=0,
                            first_observed_at=ts, last_observed_at=ts,
                        )
                    day_acc['upload_bytes'] += delta_up
                    day_acc['download_bytes'] += delta_down
                    day_acc['closing_upload_bytes'] = up
                    day_acc['closing_download_bytes'] = down
                    day_acc['remaining_bytes'] = row[7]
                    day_acc['volume_limit_bytes'] = row[8]
                    day_acc['inbound_tag'] = tag
                    day_acc['sample_count'] += 1
                    day_acc['last_observed_at'] = ts

                if ts >= hourly_cutoff:
                    if hour_acc is None or hour_acc['bucket_at'] != hour:
                        finish_hour()
                        hour_acc = dict(
                            server_id=key[0], sub_id=key[1], inbound_tag=tag, bucket_at=hour,
                            upload_bytes=0, download_bytes=0, remaining_bytes=row[7],
                            volume_limit_bytes=row[8], sample_count=0, updated_at=ts,
                        )
                    hour_acc['upload_bytes'] += delta_up
                    hour_acc['download_bytes'] += delta_down
                    hour_acc['remaining_bytes'] = row[7]
                    hour_acc['volume_limit_bytes'] = row[8]
                    hour_acc['inbound_tag'] = tag
                    hour_acc['sample_count'] += 1
                    hour_acc['updated_at'] = ts

                prev_up, prev_down = up, down
                last_point = {'tag': tag, 'up': up, 'down': down,
                              'remaining': row[7], 'limit': row[8], 'ts': ts}

            finish_account()
            db.session.bulk_save_objects(daily_batch)
            db.session.bulk_save_objects(hourly_batch)
            db.session.bulk_save_objects(state_batch)
            record.cursor_json = json.dumps({'server_id': last_server, 'sub_id': last_sub})
            record.processed_rows = int(record.processed_rows or 0) + len(rows)
            record.updated_at = datetime.utcnow()
            db.session.commit()
            cursor_server, cursor_sub = last_server, last_sub
            app.logger.info('[UsageRollup] progress %s/%s rows', f'{record.processed_rows:,}', f'{record.total_rows or 0:,}')

        record.phase = 'validation'
        db.session.commit()
        source_rows = int(db.session.execute(text(f'SELECT COUNT(*) FROM {source_table}')).scalar() or 0)
        source_accounts = int(db.session.execute(text(f'''
            SELECT COUNT(*) FROM (
                SELECT server_id, sub_id FROM {source_table} GROUP BY server_id, sub_id
            ) AS legacy_accounts
        ''')).scalar() or 0)
        target_accounts = int(UsageCounterState.query.count())
        if int(record.processed_rows or 0) != source_rows:
            raise RuntimeError(f'row validation failed: source={source_rows}, processed={record.processed_rows}')
        if target_accounts < source_accounts:
            raise RuntimeError(f'account validation failed: source={source_accounts}, target={target_accounts}')

        record.status = 'ready'
        record.phase = 'validated'
        record.details_json = json.dumps({'sourceRows': source_rows, 'sourceAccounts': source_accounts,
                                          'targetAccounts': target_accounts})
        db.session.commit()
        if finalize:
            with db.engine.begin() as conn:
                conn.execute(text(f'DROP TABLE {source_table}'))
            record.status = 'complete'
            record.phase = 'cleanup_complete'
            record.finished_at = datetime.utcnow()
            _set_system_setting_value(_USAGE_LEGACY_MIGRATION_KEY, 'complete')
            db.session.commit()
            app.logger.info('[UsageRollup] validation passed; legacy source dropped')
        return get_usage_migration_status()
    except Exception as exc:
        db.session.rollback()
        record = _usage_migration_record()
        record.status = 'failed'
        record.last_error = str(exc)[:4000]
        record.updated_at = datetime.utcnow()
        _set_system_setting_value(_USAGE_LEGACY_MIGRATION_KEY, 'failed')
        db.session.commit()
        raise
    finally:
        if lock_fd:
            try:
                import fcntl
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fd.close()


def usage_snapshot_worker():
    """Singleton daemon maintaining compact hourly and daily usage rollups."""
    from app import _add_health_log, app  # deferred: app-level helper, avoids circular import
    app.logger.info('[UsageRollup] singleton worker started (PID=%s)', os.getpid())
    # Give the post-update systemd unit first chance to own the migration. Old
    # updater scripts do not install that unit, so this becomes their fallback.
    try:
        with app.app_context():
            maintenance_needed = _legacy_usage_table_exists()
    except Exception:
        maintenance_needed = False
    if maintenance_needed:
        time.sleep(15)
    while True:
        try:
            with app.app_context():
                if (_get_system_setting_value(_USAGE_LEGACY_MIGRATION_KEY, '') == 'complete'
                        and not _legacy_usage_table_exists()):
                    break
                _migrate_legacy_usage_snapshots()
                if not _legacy_usage_table_exists():
                    break
        except Exception as exc:
            app.logger.error('[UsageRollup] legacy migration failed: %s', exc)
            try:
                with app.app_context():
                    db.session.rollback()
            except Exception:
                pass
        # If systemd owns the migration lock, do not let live collection write
        # into the same rollup tables. Retry until maintenance finishes.
        time.sleep(30)

    waited = 0
    while waited < 600 and not GLOBAL_SERVER_DATA.get('inbounds'):
        time.sleep(30)
        waited += 30
    if not GLOBAL_SERVER_DATA.get('inbounds'):
        try:
            with app.app_context():
                with GLOBAL_REFRESH_LOCK:
                    fetch_and_update_global_data(force=True)
        except Exception as exc:
            app.logger.error('[UsageRollup] initial fetch failed: %s', exc)

    while True:
        try:
            with app.app_context():
                _collect_usage_rollups()
            # Align collection to Tehran hour boundaries. In particular, the
            # 00:00 sample closes the previous day with minimal attribution skew.
            time.sleep(_seconds_until_next_usage_hour())
        except Exception as exc:
            app.logger.error('[UsageRollup] worker error: %s', exc)
            try:
                with app.app_context():
                    db.session.rollback()
                    _add_health_log('warning', 'snapshot', 'Usage rollup failed', details={'error': str(exc)})
            except Exception:
                pass
            time.sleep(60)


# File handles kept open so fcntl locks are held for the process lifetime.
_SINGLETON_LOCK_FDS = {}

def _claim_singleton(name):
    """Try to claim exclusive ownership of a singleton background thread.
    Uses a non-blocking fcntl exclusive lock on a /tmp file so:
    - Only one gunicorn worker wins (returns True).
    - If that worker dies, the OS releases the lock automatically.
    - Other workers return False and skip starting the thread.
    Gracefully falls back to True on non-Unix systems (Windows dev).
    """
    try:
        from app import app  # deferred: avoids circular import
        import fcntl as _fcntl
        lock_path = f'/tmp/eve_{name}.lock'
        fh = open(lock_path, 'w')
        _fcntl.flock(fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        _SINGLETON_LOCK_FDS[name] = fh  # keep open — releasing closes the lock
        app.logger.info("[Singleton] PID %s owns %s", os.getpid(), name)
        return True
    except (IOError, OSError):
        # Another worker already holds the lock
        return False
    except ImportError:
        # fcntl unavailable (Windows) — allow all threads (dev mode)
        return True


# ---------------------------------------------------------------------------
# Eve Pulse scheduler – drains queued probe runs and fires scheduled probes.
# Runs inside the background process only (started by
# ensure_background_threads_started); tests call pulse_scheduler_tick()
# directly with DISABLE_BACKGROUND_THREADS set.
# ---------------------------------------------------------------------------
PULSE_WORKER_POLL_SECONDS = 30


def _pulse_maybe_alert(run):
    """Send the Telegram alert when a finished run crosses the thresholds."""
    from app import _pulse_send_telegram_alert  # deferred: keeps patch('app._pulse_send_telegram_alert') working
    settings = get_pulse_settings(create=False)
    summary = run.summary()
    down = int(summary.get('down') or 0)
    degraded = int(summary.get('degraded') or 0)
    alert_down = down > 0 and (settings.alert_on_down if settings else True)
    alert_degraded = degraded > 0 and bool(settings and settings.alert_on_degraded)
    if not (alert_down or alert_degraded):
        return

    offenders = [rec for rec in run.results if rec.verdict in ('down', 'degraded')]
    offenders.sort(key=lambda rec: (rec.verdict != 'down', -(rec.loss_pct or 0)))
    lines = [
        f'📡 Pulse alert — {run.server_name or "server"} (run #{run.id})',
        f"healthy: {int(summary.get('healthy') or 0)} | degraded: {degraded} | down: {down}",
    ]
    for rec in offenders[:5]:
        parts = [f'{rec.config_label or "?"}: {rec.verdict}']
        if rec.latency_avg_ms is not None:
            parts.append(f'{rec.latency_avg_ms:.0f}ms')
        if rec.loss_pct is not None:
            parts.append(f'loss {rec.loss_pct:.0f}%')
        if rec.error:
            parts.append(str(rec.error)[:80])
        lines.append('• ' + ' | '.join(parts))
    _pulse_send_telegram_alert(run, '\n'.join(lines))


def _pulse_send_telegram_alert(run, text):
    """Deliver a pulse alert to every enabled global admin via the central bot."""
    from app import _telegram_bot_api_client, app  # deferred: app-level helper, avoids circular import
    if not (text or '').strip():
        return
    bot = _notification_bot_for_reseller(None)
    if bot is None:
        return
    api = _telegram_bot_api_client(bot)
    for admin in Admin.query.filter_by(enabled=True).all():
        role = str(admin.role or '').lower()
        if not (admin.is_superadmin or role in ('admin', 'superadmin')):
            continue
        try:
            chat_id = int(str(admin.telegram_id or '').strip())
            if chat_id <= 0:
                continue
        except (TypeError, ValueError):
            continue
        try:
            api.send_message(chat_id, text)
        except Exception as exc:
            app.logger.warning('[pulse] alert to admin %s failed: %s', admin.id, exc)


def pulse_scheduler_tick(now=None):
    """One scheduler pass: enqueue due scheduled runs, then drain the queue.

    Called by pulse_scheduler_worker (background thread) and directly by tests.
    Web-triggered runs are queued as status='queued' PulseRun rows so the web
    worker never blocks on probing.
    """
    from app import _pulse_enqueue_targets, _pulse_maybe_alert, app  # deferred: app-level helper, avoids circular import
    import pulse_runner  # lazy: pulse_runner imports app
    now = now or datetime.utcnow()

    # Reusable templates are the explicit scheduler: every saved target is
    # enqueued in its stored order, with its exact config selection.
    for template in PulseTemplate.query.filter_by(schedule_enabled=True).all():
        interval = max(5, int(template.interval_minutes or 60))
        due = (template.last_run_at is None
               or (now - template.last_run_at) >= timedelta(minutes=interval))
        if not due or not template.targets():
            continue
        _pulse_enqueue_targets(
            template.targets(), profile=template.profile, vantage=template.vantage,
            sites=template.sites(), triggered_by='schedule', template_name=template.name,
            download_bytes=template.download_bytes or 10_000_000,
            upload_bytes=template.upload_bytes or 2_000_000)
        template.last_run_at = now
        db.session.commit()

    settings = get_pulse_settings(create=False)
    if settings and settings.enabled:
        interval = max(5, int(settings.interval_minutes or 60))
        due = (settings.last_run_at is None
               or (now - settings.last_run_at) >= timedelta(minutes=interval))
        if due:
            if settings.server_id:
                target = db.session.get(Server, settings.server_id)
                targets = [target] if target and target.enabled else []
            else:
                targets = Server.query.filter_by(enabled=True).all()
            for server in targets:
                db.session.add(PulseRun(
                    server_id=server.id,
                    server_name=server.name,
                    scope='inbound' if settings.inbound_id else 'server',
                    profile=settings.profile or 'quick',
                    vantage='local',
                    status='queued',
                    triggered_by='schedule',
                    params_json=json.dumps({
                        'inbound_id': settings.inbound_id,
                        'limit': settings.probe_limit or 10,
                        'sites': settings.sites(),
                    }, ensure_ascii=False),
                ))
            settings.last_run_at = now
            db.session.commit()

    # Drain the queue sequentially — one probe run at a time. Remote runs
    # (vantage 'agent:<name>') are claimed by their agents instead.
    while True:
        run = (PulseRun.query.filter_by(status='queued', vantage='local')
               .order_by(PulseRun.created_at.asc(), PulseRun.id.asc())
               .first())
        if run is None:
            break
        run.status = 'running'
        db.session.commit()
        try:
            pulse_runner.execute_queued_run(run)
        except Exception as exc:
            db.session.rollback()
            run.status = 'failed'
            run.error = str(exc)
            run.finished_at = datetime.utcnow()
            db.session.commit()
        if run.status == 'done' and run.triggered_by in ('web', 'schedule'):
            try:
                _pulse_maybe_alert(run)
            except Exception as exc:
                app.logger.warning('[pulse] telegram alert failed: %s', exc)


def pulse_scheduler_worker():
    """Long-running loop: pulse queue drain + scheduled probes."""
    from app import app  # deferred: app-level helper, avoids circular import
    while True:
        try:
            with app.app_context():
                pulse_scheduler_tick()
        except Exception as exc:
            app.logger.error('[pulse] scheduler tick failed: %s', exc)
        time.sleep(PULSE_WORKER_POLL_SECONDS)


def ensure_background_threads_started():
    """Start background threads once per process.

    Dedicated web processes only read Redis snapshots. The background process
    owns refresh, scheduling and automation threads. ``combined`` retains the
    singleton-lock behavior used by development and legacy installations.
    """
    from app import PROCESS_ROLE, app  # deferred: app-level helper, avoids circular import
    global BACKGROUND_THREADS_STARTED
    if BACKGROUND_THREADS_STARTED:
        return
    BACKGROUND_THREADS_STARTED = True

    # Dedicated Gunicorn web processes only deserialize the shared snapshot.
    # Panel fan-out, scheduled jobs and automations belong to the background
    # process so request-serving workers do not inherit their memory peaks.
    if PROCESS_ROLE == 'web':
        try:
            threading.Thread(target=snapshot_reader_worker, daemon=True).start()
            if redis_enabled():
                app.logger.info('[ProcessRole] web worker reads snapshots from Redis.')
            else:
                app.logger.info('[ProcessRole] Redis unavailable; snapshot reader will keep retrying.')
        except Exception as e:
            app.logger.error('Failed to start snapshot reader thread: %s', e)
        return

    # Singleton: only one worker runs the scheduler (auto-backup, etc.)
    if _claim_singleton('scheduler'):
        try:
            threading.Thread(target=run_scheduler, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start scheduler thread: %s", e)
    else:
        app.logger.info("[Singleton] scheduler already owned by another worker, skipping.")

    # Data fetching:
    #  - Redis ON  : ONE worker (singleton) fetches+processes+publishes; the
    #                other workers only read the shared snapshot from Redis.
    #                → panels hit once, processing done once (not per-worker).
    #  - Redis OFF : fall back to every worker fetching into its own RAM cache.
    if redis_enabled():
        if _claim_singleton('data_fetcher'):
            try:
                threading.Thread(target=background_data_fetcher, daemon=True).start()
                app.logger.info("[Redis] this worker is the data fetcher (singleton).")
            except Exception as e:
                app.logger.error("Failed to start data fetcher thread: %s", e)
            try:
                threading.Thread(target=refresh_queue_worker, daemon=True).start()
                app.logger.info("[Redis] refresh queue worker started.")
            except Exception as e:
                app.logger.error("Failed to start refresh queue worker: %s", e)
        else:
            if PROCESS_ROLE == 'combined':
                try:
                    threading.Thread(target=snapshot_reader_worker, daemon=True).start()
                    app.logger.info("[Redis] this worker reads the shared snapshot.")
                except Exception as e:
                    app.logger.error("Failed to start snapshot reader thread: %s", e)
    else:
        # Per-worker: every worker fetches server data into its own memory cache
        try:
            threading.Thread(target=background_data_fetcher, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start data fetcher thread: %s", e)

    # Singleton: only one worker runs health watchdog (DB logs, notifications)
    if _claim_singleton('health_watchdog'):
        try:
            threading.Thread(target=health_watchdog, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start health watchdog thread: %s", e)
    else:
        app.logger.info("[Singleton] health_watchdog already owned by another worker, skipping.")

    # Singleton: only one worker runs usage snapshots — no race conditions, no dedup needed
    if _claim_singleton('snapshot_worker'):
        try:
            threading.Thread(target=usage_snapshot_worker, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start usage snapshot thread: %s", e)
    else:
        app.logger.info("[Singleton] snapshot_worker already owned by another worker, skipping.")

    # Singleton: only one worker runs the WhatsApp near-depletion bot scanner
    if _claim_singleton('whatsapp_bot_worker'):
        try:
            threading.Thread(target=whatsapp_bot_worker, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start whatsapp bot thread: %s", e)
    else:
        app.logger.info("[Singleton] whatsapp_bot_worker already owned by another worker, skipping.")

    # Singleton: only one worker runs the SMS near-depletion bot scanner
    if _claim_singleton('sms_bot_worker'):
        try:
            threading.Thread(target=sms_bot_worker, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start sms bot thread: %s", e)
    else:
        app.logger.info("[Singleton] sms_bot_worker already owned by another worker, skipping.")

    # Singleton: only one worker runs the Telegram near-depletion bot scanner
    if _claim_singleton('telegram_depletion_worker'):
        try:
            threading.Thread(target=telegram_depletion_worker, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start telegram depletion thread: %s", e)
    else:
        app.logger.info("[Singleton] telegram_depletion_worker already owned by another worker, skipping.")

    # Singleton: durable targeted Telegram announcement queue.
    if _claim_singleton('telegram_announcement_worker'):
        try:
            threading.Thread(target=telegram_announcement_worker, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start telegram announcement thread: %s", e)
    else:
        app.logger.info("[Singleton] telegram_announcement_worker already owned by another worker, skipping.")

    # Singleton: reconcile queued GMweb tasks and persist their terminal status.
    if _claim_singleton('sms_status_worker'):
        try:
            threading.Thread(target=sms_status_worker, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start sms status thread: %s", e)
    else:
        app.logger.info("[Singleton] sms_status_worker already owned by another worker, skipping.")

    # Singleton: pulse health-check queue worker (web-triggered + scheduled probes).
    if _claim_singleton('pulse_scheduler'):
        try:
            threading.Thread(target=pulse_scheduler_worker, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start pulse scheduler thread: %s", e)
    else:
        app.logger.info("[Singleton] pulse_scheduler already owned by another worker, skipping.")

    # Singleton: BNQO link status/detection engine + retention rollup.
    if _claim_singleton('bnqo_scheduler'):
        try:
            from panel.jobs.bnqo import bnqo_scheduler_worker  # deferred: keeps module import light
            threading.Thread(target=bnqo_scheduler_worker, daemon=True).start()
        except Exception as e:
            app.logger.error("Failed to start bnqo scheduler thread: %s", e)
    else:
        app.logger.info("[Singleton] bnqo_scheduler already owned by another worker, skipping.")

if not os.environ.get('DISABLE_BACKGROUND_THREADS'):
    # Start threads on module import (works under gunicorn as well)
    ensure_background_threads_started()
