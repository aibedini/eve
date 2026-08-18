"""Refresh / bulk-job / cached-client pipeline (extracted from app.py).

Background data-fetch machinery: refresh-job lifecycle, bulk client jobs,
telegram-backup job runner, write-through cached-client helpers.
"""
import copy
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import requests
from sqlalchemy import func, or_

from panel.adapters.xui import (
    _push_full_inbound,
    _reconcile_client_inbounds,
    extract_base_and_webpath,
    fetch_inbounds,
    fetch_onlines,
    fetch_server_status,
    get_xui_session,
    persist_detected_panel_type,
    server_is_v3,
    v3_reset_client,
    v3_update_client,
)
from panel.core.redis_client import (
    GLOBAL_REFRESH_LOCK,
    GLOBAL_SERVER_DATA,
    REDIS_REFRESH_JOB_PREFIX,
    REDIS_REFRESH_JOB_TTL,
    REDIS_REFRESH_PROCESSING_KEY,
    REDIS_REFRESH_QUEUE_KEY,
    REDIS_REFRESH_SCOPE_PREFIX,
    get_redis,
    publish_snapshot_to_redis,
)
from panel.extensions import db
from panel.models import Admin, ClientOwnership, Server
from panel.services.backup import _run_telegram_backup
from panel.services.subscription import find_client

REFRESH_JOBS = {}  # job_id -> job dict
REFRESH_JOBS_LOCK = threading.Lock()
REFRESH_MAX_JOBS = 50

# Automated SMS state-scan progress (in-memory; per-process). A single job at a
# time — the worker (or a manual "run now") populates it so the UI can show how
# many users will be messaged, how many are done, and what's left.
SMS_SCAN_JOB = {'state': 'idle'}
SMS_SCAN_JOB_LOCK = threading.Lock()
# Mirror the scan job + cancel signal to Redis so every gunicorn worker sees the
# same live progress and a Stop from any worker reaches the worker running the scan.
SMS_SCAN_REDIS_KEY = 'eve:sms_scan_job'
SMS_SCAN_CANCEL_REDIS_KEY = 'eve:sms_scan_cancel'
SMS_SCAN_REDIS_TTL = 900

# Bulk job tracking. Persist to a shared file so progress polling works across
# gunicorn workers while the background worker updates the job.
BULK_JOBS_FILE = os.path.join(tempfile.gettempdir(), 'eve_bulk_jobs.json')
BULK_JOBS = {}  # job_id -> job dict (status/progress only — client list kept in BULK_JOBS_CLIENTS)
BULK_JOBS_CLIENTS = {}  # job_id -> client list (in-memory only; NOT persisted to avoid huge files)
BULK_JOBS_LOCK = threading.Lock()
BULK_MAX_JOBS = 50
BULK_SAVE_EVERY = 25   # write progress to disk every N clients (not every single one)

# Manual snapshot progress tracking
# Written to a shared file so all gunicorn workers see the same state.
_SNAPSHOT_PROGRESS_FILE = '/tmp/eve_snapshot_progress.json'
_SNAPSHOT_PROGRESS = {
    'status': 'idle',   # idle | running | done | error
    'step': 0,
    'total': 0,
    'current_server': '',
    'message': '',
    'message_fa': '',
    'inbound_count': 0,
    'fetched_fresh': False,
    'error': None,
}

def _set_snap_progress(updates):
    """Update _SNAPSHOT_PROGRESS and persist to shared file for cross-worker visibility."""
    global _SNAPSHOT_PROGRESS
    _SNAPSHOT_PROGRESS.update(updates)
    try:
        import json as _json
        with open(_SNAPSHOT_PROGRESS_FILE, 'w') as _f:
            _json.dump(_SNAPSHOT_PROGRESS, _f)
    except Exception:
        pass

def _read_snap_progress():
    """Read progress from shared file; fall back to in-memory dict."""
    try:
        import json as _json
        with open(_SNAPSHOT_PROGRESS_FILE) as _f:
            return _json.load(_f)
    except Exception:
        return _SNAPSHOT_PROGRESS

# Telegram backup job tracking. Persist to a shared file so all gunicorn
# workers can report status for jobs started by another worker.
TELEGRAM_BACKUP_JOBS_FILE = os.path.join(tempfile.gettempdir(), 'eve_telegram_backup_jobs.json')
TELEGRAM_BACKUP_JOBS = {}  # job_id -> job dict
TELEGRAM_BACKUP_JOBS_LOCK = threading.Lock()
TELEGRAM_BACKUP_MAX_JOBS = 20

MAX_FILE_SIZE = 10 * 1024 * 1024        # 10 MB  — general file uploads




REFRESH_BACKOFF = {}  # server_id -> {fail_count:int, next_allowed_at:float, last_error:str, last_failed_at:float}
REFRESH_MAX_BACKOFF_SEC = 300

def _summarize_job(job):
    if not isinstance(job, dict):
        return None
    # keep payload small
    keys = (
        'id', 'state', 'mode', 'server_id', 'force',
        'created_at', 'started_at', 'finished_at',
        'progress', 'error'
    )
    return {k: job.get(k) for k in keys if k in job}


def _summarize_bulk_job(job):
    if not isinstance(job, dict):
        return None
    keys = (
        'id', 'state', 'action',
        'created_at', 'started_at', 'finished_at',
        'progress', 'error', 'report_rows', 'report_rules'
    )
    return {k: job.get(k) for k in keys if k in job}


def _summarize_telegram_backup_job(job):
    if not isinstance(job, dict):
        return None
    keys = (
        'id', 'state', 'trigger',
        'created_at', 'started_at', 'finished_at',
        'stage', 'progress', 'error',
        'success_count', 'total', 'results'
    )
    return {k: job.get(k) for k in keys if k in job}


def _prune_telegram_backup_jobs_locked():
    if len(TELEGRAM_BACKUP_JOBS) <= TELEGRAM_BACKUP_MAX_JOBS:
        return
    jobs_sorted = sorted(TELEGRAM_BACKUP_JOBS.items(), key=lambda kv: kv[1].get('created_at_ts', 0))
    to_delete = max(0, len(TELEGRAM_BACKUP_JOBS) - TELEGRAM_BACKUP_MAX_JOBS)
    deleted = 0
    for job_id, job in jobs_sorted:
        if deleted >= to_delete:
            break
        if job.get('state') in ('done', 'error'):
            TELEGRAM_BACKUP_JOBS.pop(job_id, None)
            deleted += 1


def _load_telegram_backup_jobs_locked():
    global TELEGRAM_BACKUP_JOBS
    try:
        with open(TELEGRAM_BACKUP_JOBS_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            TELEGRAM_BACKUP_JOBS = data
    except Exception:
        pass
    return TELEGRAM_BACKUP_JOBS


def _save_telegram_backup_jobs_locked():
    from app import app  # deferred: app-level helper, avoids circular import
    try:
        tmp_path = TELEGRAM_BACKUP_JOBS_FILE + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(TELEGRAM_BACKUP_JOBS, fh, ensure_ascii=False)
        os.replace(tmp_path, TELEGRAM_BACKUP_JOBS_FILE)
    except Exception as exc:
        try:
            app.logger.warning("Could not persist Telegram backup jobs: %s", exc)
        except Exception:
            pass


def _get_telegram_backup_job(job_id: str):
    with TELEGRAM_BACKUP_JOBS_LOCK:
        return copy.deepcopy(_load_telegram_backup_jobs_locked().get(job_id))


def _update_telegram_backup_job(job_id: str, **patch):
    with TELEGRAM_BACKUP_JOBS_LOCK:
        _load_telegram_backup_jobs_locked()
        job = TELEGRAM_BACKUP_JOBS.get(job_id)
        if not job:
            return
        for k, v in patch.items():
            job[k] = v
        TELEGRAM_BACKUP_JOBS[job_id] = job
        _save_telegram_backup_jobs_locked()


def _run_telegram_backup_job(job_id: str):
    from app import _utc_iso_now, app  # deferred: app-level helper, avoids circular import
    with TELEGRAM_BACKUP_JOBS_LOCK:
        _load_telegram_backup_jobs_locked()
        job = TELEGRAM_BACKUP_JOBS.get(job_id)
        if not job:
            return
        job['state'] = 'running'
        job['started_at'] = _utc_iso_now()
        job['stage'] = 'starting'
        TELEGRAM_BACKUP_JOBS[job_id] = job
        _save_telegram_backup_jobs_locked()

    def progress_cb(update: dict):
        if not isinstance(update, dict):
            return
        patch = {}
        if update.get('stage') is not None:
            patch['stage'] = update['stage']
        if update.get('progress') is not None:
            patch['progress'] = update['progress']
        if update.get('results') is not None:
            patch['results'] = update['results']
        if patch:
            _update_telegram_backup_job(job_id, **patch)

    try:
        with app.app_context():
            job_snapshot = _get_telegram_backup_job(job_id) or {}
            result = _run_telegram_backup(trigger=str(job_snapshot.get('trigger') or 'manual'), progress_cb=progress_cb)
    except Exception as exc:
        _update_telegram_backup_job(job_id, state='error', finished_at=_utc_iso_now(), error=str(exc), stage='error')
        return

    all_results = result.get('results') or []
    success_count = int(result.get('success_count') or 0)
    total_count = int(result.get('total') or 0)
    failures = [r for r in all_results if not r.get('success')]

    if result.get('success'):
        # Partial success: some servers failed — still mark done but include failure details
        partial_error = None
        if failures:
            partial_error = '; '.join(f"{r.get('server_name','?')}: {r.get('error','?')}" for r in failures)
        _update_telegram_backup_job(
            job_id,
            state='done',
            finished_at=_utc_iso_now(),
            stage='done',
            success_count=success_count,
            total=total_count,
            results=all_results,
            error=partial_error,
        )
    else:
        _update_telegram_backup_job(
            job_id,
            state='error',
            finished_at=_utc_iso_now(),
            stage='error',
            success_count=success_count,
            total=total_count,
            results=all_results,
            error=result.get('error') or 'Backup failed',
        )


def _prune_bulk_jobs_locked():
    _load_bulk_jobs_locked()
    if len(BULK_JOBS) <= BULK_MAX_JOBS:
        return
    jobs_sorted = sorted(BULK_JOBS.items(), key=lambda kv: kv[1].get('created_at_ts', 0))
    to_delete = max(0, len(BULK_JOBS) - BULK_MAX_JOBS)
    deleted = 0
    for job_id, job in jobs_sorted:
        if deleted >= to_delete:
            break
        if job.get('state') in ('done', 'error'):
            BULK_JOBS.pop(job_id, None)
            deleted += 1
    if deleted:
        _save_bulk_jobs_locked()


def _load_bulk_jobs_locked():
    """Merge the on-disk snapshot into the in-memory BULK_JOBS.

    With gunicorn --workers 3, a bulk job runs as a thread in ONE worker while
    polling requests are load-balanced across all workers. The old code did
    `BULK_JOBS = data` (full replace), which in a multi-worker race could drop
    a job a worker was actively tracking → "Job not found". We now MERGE:
    for each job, keep whichever copy has more progress, and never drop an
    in-memory job that the disk snapshot happens to lack.
    """
    global BULK_JOBS
    try:
        with open(BULK_JOBS_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return BULK_JOBS
    except Exception:
        return BULK_JOBS

    def _processed(job):
        try:
            return int((job.get('progress') or {}).get('processed', 0) or 0)
        except Exception:
            return 0

    merged = dict(BULK_JOBS)  # start from in-memory
    for jid, disk_job in data.items():
        mem_job = merged.get(jid)
        if mem_job is None:
            merged[jid] = disk_job
            continue
        # A finished state is authoritative; otherwise keep the further-along copy.
        mem_done = mem_job.get('state') in ('done', 'error')
        disk_done = disk_job.get('state') in ('done', 'error')
        if disk_done and not mem_done:
            merged[jid] = disk_job
        elif mem_done and not disk_done:
            pass  # keep mem
        elif _processed(disk_job) > _processed(mem_job):
            merged[jid] = disk_job
        # else keep mem (it's at least as fresh)
    BULK_JOBS = merged
    return BULK_JOBS


def _save_bulk_jobs_locked():
    """Persist BULK_JOBS to disk.  Clients are stored in BULK_JOBS_CLIENTS (memory-only)
    so the file stays small even with thousands of clients per job.

    Before writing, jobs that exist ONLY on disk (created by another gunicorn
    worker) are preserved so a save from this worker never clobbers another
    worker's concurrent job.
    """
    from app import app  # deferred: app-level helper, avoids circular import
    try:
        # Never write the client list to disk — it can be MB-sized per job.
        slim = {}
        for jid, j in BULK_JOBS.items():
            slim[jid] = {k: v for k, v in j.items() if k != 'clients'}

        # Preserve other workers' jobs that we don't have in memory.
        try:
            with open(BULK_JOBS_FILE, 'r', encoding='utf-8') as fh:
                disk = json.load(fh)
            if isinstance(disk, dict):
                for jid, dj in disk.items():
                    if jid not in slim:
                        slim[jid] = dj
        except Exception:
            pass

        tmp_path = BULK_JOBS_FILE + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(slim, fh, ensure_ascii=False)
        os.replace(tmp_path, BULK_JOBS_FILE)
    except Exception as exc:
        try:
            app.logger.warning("Could not persist bulk jobs: %s", exc)
        except Exception:
            pass


def _bulk_progress_update(job_id: str, *, processed_delta: int = 1,
                          success: int = 0, failed: int = 0, skipped: int = 0,
                          error_entry: dict | None = None,
                          report_row: dict | None = None,
                          force_save: bool = False) -> None:
    """Thread-safe progress update for _run_bulk_job.

    Writes to disk only every BULK_SAVE_EVERY clients (not on every single
    client) to prevent thousands of I/O operations for large bulk actions.
    Pass force_save=True for state transitions (start/done/error).
    """
    with BULK_JOBS_LOCK:
        j = BULK_JOBS.get(job_id)
        if j is None:
            return
        pr = j.get('progress') or {}
        pr['processed'] = int(pr.get('processed', 0) or 0) + processed_delta
        if success:
            pr['success']  = int(pr.get('success',  0) or 0) + success
        if failed:
            pr['failed']   = int(pr.get('failed',   0) or 0) + failed
        if skipped:
            pr['skipped']  = int(pr.get('skipped',  0) or 0) + skipped
        if error_entry:
            errs = j.get('errors') or []
            if len(errs) < 50:
                errs.append(error_entry)
            j['errors'] = errs
        if report_row:
            rows = j.get('report_rows') or []
            if len(rows) < 10000:
                rows.append(report_row)
            j['report_rows'] = rows
        j['progress'] = pr
        BULK_JOBS[job_id] = j
        # Throttle disk writes: only every BULK_SAVE_EVERY clients or on force
        if force_save or (int(pr.get('processed', 0)) % BULK_SAVE_EVERY == 0):
            _save_bulk_jobs_locked()


def _run_bulk_job(job_id: str):
    from app import CLIENT_RESET_FALLBACKS, CLIENT_UPDATE_FALLBACKS, _bytes_to_gb_float, _clear_message_cooldown, _delete_client_core, _fetch_client_snapshot, _has_client_access, _json_field, _normalize_volume_policy_rules, _post_client_update, _reset_client_traffic_core, _toggle_client_core, _utc_iso_now, _wt_patch_cache, app, build_panel_url, collect_endpoint_templates, ensure_reseller_allowed_for_assignment, format_remaining_days  # deferred: app-level helper, avoids circular import
    with BULK_JOBS_LOCK:
        _load_bulk_jobs_locked()
        job = BULK_JOBS.get(job_id)
        if not job:
            return
        job['state'] = 'running'
        job['started_at'] = _utc_iso_now()
        BULK_JOBS[job_id] = job
        _save_bulk_jobs_locked()

    try:
        with app.app_context():
            job = None
            with BULK_JOBS_LOCK:
                # Read status from memory first (avoid disk replacement race).
                # _load_bulk_jobs_locked is intentionally skipped here — the job
                # was just saved at the top of this function.
                job = BULK_JOBS.get(job_id) or {}
                # Client list is kept in the separate in-memory dict to avoid
                # bloating the persisted file with thousands of client entries.
                clients = BULK_JOBS_CLIENTS.get(job_id) or job.get('clients') or []

            action = job.get('action')
            data = job.get('data') or {}
            conditions = job.get('conditions') or {}
            user_id = job.get('user_id')

            user = db.session.get(Admin, user_id)
            if not user:
                raise RuntimeError('User not found')

            reseller_id = None
            if action == 'assign_owner':
                reseller_id = data.get('reseller_id')
                try:
                    reseller_id = int(reseller_id)
                except (TypeError, ValueError):
                    reseller_id = None
                if not reseller_id:
                    raise RuntimeError('reseller_id required')
                reseller = db.session.get(Admin, reseller_id)
                if not reseller or reseller.role != 'reseller':
                    raise RuntimeError('Invalid reseller')

            server_ids = []
            for item in clients:
                if isinstance(item, dict) and 'server_id' in item:
                    server_ids.append(item.get('server_id'))
            normalized_server_ids = []
            for sid in server_ids:
                try:
                    normalized_server_ids.append(int(sid))
                except (TypeError, ValueError):
                    continue
            normalized_server_ids = list({sid for sid in normalized_server_ids})
            servers_by_id = {}
            if normalized_server_ids:
                for s in Server.query.filter(Server.id.in_(normalized_server_ids)).all():
                    servers_by_id[s.id] = s

            def _normalize_bulk_conditions(raw: Any) -> dict:
                if not isinstance(raw, dict):
                    return {}
                enable_state = (raw.get('enable_state') or 'any').strip().lower()
                if enable_state not in ('any', 'enabled', 'disabled'):
                    enable_state = 'any'
                expiry_type = (raw.get('expiry_type') or 'any').strip().lower()
                if expiry_type not in ('any', 'unlimited', 'start_after_use', 'expired', 'today', 'soon', 'normal'):
                    expiry_type = 'any'
                return {
                    'enable_state': enable_state,
                    'expiry_type': expiry_type,
                }

            normalized_conditions = _normalize_bulk_conditions(conditions)

            def _fetch_client_snapshot(_user: 'Admin', _server: 'Server', _inbound_id: int, _email: str):
                """Fetch a best-effort client dict with at least enable/expiryTime/totalGB/up/down."""
                target_client = None
                cached_client_row = None

                try:
                    cached_inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
                except Exception:
                    cached_inbounds = []

                for ib in cached_inbounds:
                    try:
                        if int(ib.get('server_id', -1)) != int(_server.id):
                            continue
                        if int(ib.get('id', -1)) != int(_inbound_id):
                            continue
                        for c in ib.get('clients', []):
                            if (c.get('email') or '') == _email:
                                cached_client_row = c
                                if isinstance(c, dict) and 'raw_client' in c and isinstance(c.get('raw_client'), dict):
                                    target_client = copy.deepcopy(c.get('raw_client'))
                                break
                    except Exception:
                        continue
                    if cached_client_row:
                        break

                session_obj, error = get_xui_session(_server)
                if error:
                    return None, error

                if not target_client:
                    inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, _server.host, _server.panel_type)
                    if fetch_err:
                        return None, fetch_err
                    persist_detected_panel_type(_server, detected_type)
                    target_client, _ = find_client(inbounds, _inbound_id, _email)
                    if not target_client:
                        return None, 'Client not found'

                # Merge missing caps/usage from cached row when available.
                if cached_client_row and isinstance(target_client, dict):
                    for k in ('enable', 'up', 'down', 'totalGB', 'expiryTime'):
                        try:
                            if target_client.get(k) in (None, '') and cached_client_row.get(k) not in (None, ''):
                                target_client[k] = cached_client_row.get(k)
                            elif k in ('up', 'down', 'totalGB', 'expiryTime'):
                                if int(target_client.get(k) or 0) == 0 and int(cached_client_row.get(k) or 0) != 0:
                                    target_client[k] = cached_client_row.get(k)
                        except Exception:
                            pass

                return target_client, None

            def _matches_conditions(client_obj: dict, cond: dict) -> bool:
                if not cond:
                    return True
                if not isinstance(client_obj, dict):
                    return False

                enable_state = (cond.get('enable_state') or 'any')
                if enable_state != 'any':
                    is_enabled = bool(client_obj.get('enable'))
                    if enable_state == 'enabled' and not is_enabled:
                        return False
                    if enable_state == 'disabled' and is_enabled:
                        return False

                expiry_type = (cond.get('expiry_type') or 'any')
                if expiry_type != 'any':
                    try:
                        exp_info = format_remaining_days(client_obj.get('expiryTime', 0))
                        cur_type = (exp_info.get('type') or '').strip().lower()
                    except Exception:
                        cur_type = ''
                    if cur_type != expiry_type:
                        return False

                return True

            def _wt_patch_cache(_server, _email, tc):
                """Write-through the cache after a successful bulk client update."""
                try:
                    patch_cached_client(
                        _server.id, _email,
                        client_uuid=str(tc.get('id')) if tc.get('id') else None,
                        total_gb_bytes=int(tc.get('totalGB') or 0),
                        expiry_ts=int(tc.get('expiryTime') or 0),
                        enable=tc.get('enable'))
                except Exception:
                    pass

            def _post_client_update(_server: 'Server', _inbound_id: int, _email: str, target_client: dict):
                session_obj, error = get_xui_session(_server)
                if error:
                    return False, error, 400

                if server_is_v3(_server):
                    ok, _vr, verr = v3_update_client(_server, session_obj, _email, target_client)
                    if ok:
                        _wt_patch_cache(_server, _email, target_client)
                        return True, None, 200
                    detail = verr or 'panel rejected update'
                    app.logger.warning("Bulk update client (v3) failed for %s: %s", _email, detail)
                    return False, detail, 502

                # Shadowsocks clients have no UUID 'id' — updateClient/:clientId won't work.
                if 'id' not in target_client:
                    _ibs, _fe, _ = fetch_inbounds(session_obj, _server.host, _server.panel_type)
                    _full_ib = None
                    if not _fe:
                        for _ib in (_ibs or []):
                            if _ib.get('id') == _inbound_id:
                                _full_ib = _ib
                                break
                    if _full_ib is None:
                        detail = 'shadowsocks: could not fetch full inbound for update'
                        app.logger.warning("Bulk update client failed for %s: %s", _email, detail)
                        return False, detail, 400
                    _full_settings = _json_field(_full_ib.get('settings'), {})
                    _full_settings['clients'] = [
                        target_client if c.get('email') == _email else c
                        for c in _full_settings.get('clients', [])
                    ]
                    _ok_push, _push_err = _push_full_inbound(_server, session_obj, _full_ib, _full_settings)
                    if _ok_push:
                        _wt_patch_cache(_server, _email, target_client)
                        return True, None, 200
                    detail = _push_err or 'shadowsocks inbound update failed'
                    app.logger.warning("Bulk update client failed for %s: %s", _email, detail)
                    return False, detail, 400

                client_id = target_client.get('id', target_client.get('password', _email))
                update_payload = {
                    'id': _inbound_id,
                    'settings': json.dumps({'clients': [target_client]})
                }

                replacements = {
                    'id': _inbound_id,
                    'inbound_id': _inbound_id,
                    'inboundId': _inbound_id,
                    'clientId': client_id,
                    'client_id': client_id,
                    'email': _email
                }

                templates = collect_endpoint_templates(_server.panel_type, 'client_update', CLIENT_UPDATE_FALLBACKS)
                errors = []
                for template in templates:
                    full_url = build_panel_url(_server.host, template, replacements)
                    if not full_url:
                        continue
                    try:
                        resp = session_obj.post(full_url, json=update_payload, verify=False, timeout=10)
                    except Exception as exc:
                        errors.append(f"{template}: {exc}")
                        continue
                    if resp.status_code == 200:
                        try:
                            resp_json = resp.json()
                            if isinstance(resp_json, dict) and resp_json.get('success') is False:
                                panel_msg = resp_json.get('msg') or resp_json.get('message') or 'success=false'
                                errors.append(f"{template}: {panel_msg}")
                                continue
                        except ValueError:
                            pass
                        _wt_patch_cache(_server, _email, target_client)
                        return True, None, 200
                    errors.append(f"{template}: HTTP {resp.status_code}")

                detail = '; '.join(errors) or 'no endpoint succeeded'
                app.logger.warning("Bulk update client failed for %s: %s", _email, detail)
                return False, detail, 400

            def _reset_client_traffic_core(_server: 'Server', _inbound_id: int, _email: str):
                session_obj, error = get_xui_session(_server)
                if error:
                    return False, error, 400

                if server_is_v3(_server):
                    ok, _vr, verr = v3_reset_client(_server, session_obj, _email)
                    if ok:
                        return True, None, 200
                    detail = verr or 'reset failed'
                    app.logger.warning("Bulk reset traffic (v3) failed for %s: %s", _email, detail)
                    return False, detail, 502

                replacements = {
                    'id': _inbound_id,
                    'inbound_id': _inbound_id,
                    'inboundId': _inbound_id,
                    'email': _email
                }
                templates = collect_endpoint_templates(_server.panel_type, 'client_reset_traffic', CLIENT_RESET_FALLBACKS)
                errors = []
                for template in templates:
                    full_url = build_panel_url(_server.host, template, replacements)
                    if not full_url:
                        continue
                    requires_path_email = (':email' in template) or ('{email}' in template)
                    payload = None if requires_path_email else {'email': _email}
                    try:
                        if payload is None:
                            resp = session_obj.post(full_url, verify=False, timeout=10)
                        else:
                            resp = session_obj.post(full_url, json=payload, verify=False, timeout=10)
                    except Exception as exc:
                        errors.append(f"{template}: {exc}")
                        continue
                    if resp.status_code == 200:
                        try:
                            resp_json = resp.json()
                            if isinstance(resp_json, dict) and resp_json.get('success') is False:
                                panel_msg = resp_json.get('msg') or resp_json.get('message') or 'success=false'
                                errors.append(f"{template}: {panel_msg}")
                                continue
                        except ValueError:
                            pass
                        return True, None, 200
                    errors.append(f"{template}: HTTP {resp.status_code}")

                detail = '; '.join(errors) or 'no endpoint succeeded'
                app.logger.warning("Bulk reset traffic failed for %s: %s", _email, detail)
                return False, detail, 400

            def _apply_client_limit_delta(_user: 'Admin', _server: 'Server', _inbound_id: int, _email: str,
                                          days_delta: int | None = None, volume_gb_delta: int | None = None):
                if _user.role == 'reseller':
                    if not _has_client_access(_user, _server.id, _email, inbound_id=_inbound_id):
                        return False, 'Access denied', 403

                # validate deltas
                if days_delta is not None:
                    try:
                        days_delta = int(days_delta)
                    except Exception:
                        return False, 'Invalid days value', 400
                    if days_delta <= 0:
                        return False, 'Days must be > 0', 400
                if volume_gb_delta is not None:
                    try:
                        volume_gb_delta = int(volume_gb_delta)
                    except Exception:
                        return False, 'Invalid volume value', 400
                    if volume_gb_delta <= 0:
                        return False, 'Volume must be > 0', 400

                target_client, err = _fetch_client_snapshot(_user, _server, _inbound_id, _email)
                if err:
                    return False, err, 400
                if not isinstance(target_client, dict):
                    return False, 'Client not found', 404

                # Calculate new expiry (if requested)
                if days_delta is not None:
                    current_expiry = target_client.get('expiryTime', 0)
                    try:
                        current_expiry_int = int(current_expiry or 0)
                    except (TypeError, ValueError):
                        current_expiry_int = 0

                    if current_expiry_int < 0:
                        # Not started yet: add to pending duration
                        new_expiry = current_expiry_int - (days_delta * 86400000)
                    elif current_expiry_int > 0:
                        current_date = datetime.fromtimestamp(current_expiry_int / 1000)
                        new_date = current_date + timedelta(days=days_delta)
                        new_expiry = int(new_date.timestamp() * 1000)
                    else:
                        new_date = datetime.now() + timedelta(days=days_delta)
                        new_expiry = int(new_date.timestamp() * 1000)
                    target_client['expiryTime'] = new_expiry

                # Calculate new cap (if requested)
                if volume_gb_delta is not None:
                    try:
                        current_total_bytes = int(target_client.get('totalGB') or 0)
                    except (TypeError, ValueError):
                        current_total_bytes = 0

                    # If current is unlimited (0), keep unlimited.
                    if current_total_bytes == 0:
                        new_total_bytes = 0
                    else:
                        new_total_bytes = current_total_bytes + (volume_gb_delta * 1024 * 1024 * 1024)
                    target_client['totalGB'] = new_total_bytes

                return _post_client_update(_server, _inbound_id, _email, target_client)

            def _normalize_volume_policy_rules(raw_rules: Any) -> list[dict]:
                if not isinstance(raw_rules, list):
                    return []
                rules = []
                for raw_rule in raw_rules:
                    if not isinstance(raw_rule, dict):
                        continue
                    try:
                        min_gb = float(raw_rule.get('min_remaining_gb'))
                        max_gb = float(raw_rule.get('max_remaining_gb'))
                        target_gb = float(raw_rule.get('target_gb'))
                    except (TypeError, ValueError):
                        continue
                    if min_gb < 0 or max_gb < 0 or target_gb < 0:
                        continue
                    if max_gb < min_gb:
                        min_gb, max_gb = max_gb, min_gb
                    mode = str(raw_rule.get('mode') or 'set_remaining').strip().lower()
                    if mode not in ('set_remaining', 'reset_and_set'):
                        mode = 'set_remaining'
                    rules.append({
                        'min_remaining_gb': min_gb,
                        'max_remaining_gb': max_gb,
                        'target_gb': target_gb,
                        'mode': mode,
                    })
                return rules

            def _bytes_to_gb_float(value: int | float | None) -> float:
                try:
                    return round(float(value or 0) / float(1024 ** 3), 3)
                except Exception:
                    return 0.0

            def _apply_client_volume_policy(_user: 'Admin', _server: 'Server', _inbound_id: int, _email: str, raw_rules: Any):
                report = {
                    'server_id': getattr(_server, 'id', None),
                    'server_name': getattr(_server, 'name', '') or '',
                    'inbound_id': _inbound_id,
                    'email': _email,
                    'status': 'failed',
                    'error': None,
                }
                if _user.role == 'reseller':
                    if not _has_client_access(_user, _server.id, _email, inbound_id=_inbound_id):
                        report['error'] = 'Access denied'
                        return False, 'Access denied', 403, report

                rules = _normalize_volume_policy_rules(raw_rules)
                if not rules:
                    report['error'] = 'No valid volume rules'
                    return False, 'No valid volume rules', 400, report

                target_client, err = _fetch_client_snapshot(_user, _server, _inbound_id, _email)
                if err:
                    report['error'] = err
                    return False, err, 400, report
                if not isinstance(target_client, dict):
                    report['error'] = 'Client not found'
                    return False, 'Client not found', 404, report

                try:
                    current_total_bytes = int(target_client.get('totalGB') or 0)
                except (TypeError, ValueError):
                    current_total_bytes = 0
                if current_total_bytes <= 0:
                    report.update({
                        'status': 'skipped',
                        'reason': 'unlimited_volume',
                        'before_total_gb': 0,
                        'before_remaining_gb': None,
                        'after_total_gb': 0,
                        'after_remaining_gb': None,
                    })
                    return True, 'Unlimited volume skipped', 204, report

                try:
                    used_up = int(target_client.get('up') or 0)
                except (TypeError, ValueError):
                    used_up = 0
                try:
                    used_down = int(target_client.get('down') or 0)
                except (TypeError, ValueError):
                    used_down = 0
                used_bytes = max(0, used_up + used_down)
                remaining_bytes = max(0, current_total_bytes - used_bytes)
                remaining_gb = remaining_bytes / float(1024 ** 3)
                report.update({
                    'before_total_gb': _bytes_to_gb_float(current_total_bytes),
                    'before_remaining_gb': round(remaining_gb, 3),
                    'used_gb': _bytes_to_gb_float(used_bytes),
                })

                matched_rule = None
                for rule in rules:
                    if rule['min_remaining_gb'] <= remaining_gb <= rule['max_remaining_gb']:
                        matched_rule = rule
                        break
                if not matched_rule:
                    report.update({
                        'status': 'skipped',
                        'reason': 'no_matching_rule',
                        'after_total_gb': report.get('before_total_gb'),
                        'after_remaining_gb': report.get('before_remaining_gb'),
                    })
                    return True, 'No matching volume rule', 204, report

                target_bytes = int(round(matched_rule['target_gb'] * (1024 ** 3)))
                report.update({
                    'matched_min_gb': matched_rule['min_remaining_gb'],
                    'matched_max_gb': matched_rule['max_remaining_gb'],
                    'target_gb': matched_rule['target_gb'],
                    'mode': matched_rule['mode'],
                })
                if matched_rule['mode'] == 'reset_and_set':
                    target_client['up'] = 0
                    target_client['down'] = 0
                    target_client['totalGB'] = target_bytes
                    ok, update_err, status = _post_client_update(_server, _inbound_id, _email, target_client)
                    if not ok:
                        report['error'] = update_err
                        return ok, update_err, status, report
                    reset_ok, reset_err, reset_status = _reset_client_traffic_core(_server, _inbound_id, _email)
                    if not reset_ok:
                        report['error'] = reset_err
                        return reset_ok, reset_err, reset_status, report
                    report.update({
                        'status': 'changed',
                        'after_total_gb': _bytes_to_gb_float(target_bytes),
                        'after_remaining_gb': _bytes_to_gb_float(target_bytes),
                    })
                    return True, None, 200, report

                new_total_bytes = used_bytes + target_bytes
                target_client['totalGB'] = new_total_bytes
                ok, update_err, status = _post_client_update(_server, _inbound_id, _email, target_client)
                if ok:
                    report.update({
                        'status': 'changed',
                        'after_total_gb': _bytes_to_gb_float(new_total_bytes),
                        'after_remaining_gb': _bytes_to_gb_float(target_bytes),
                    })
                else:
                    report['error'] = update_err
                return ok, update_err, status, report

            def _apply_client_volume_multiplier(_user: 'Admin', _server: 'Server', _inbound_id: int, _email: str, _data: dict):
                """Apply a numeric multiplier to a client's remaining volume.

                mode=set_remaining: new cap = used + (remaining × factor)  [no traffic reset]
                mode=reset_and_set: reset up/down to 0, new cap = remaining × factor
                """
                report = {
                    'server_id': getattr(_server, 'id', None),
                    'server_name': getattr(_server, 'name', '') or '',
                    'inbound_id': _inbound_id,
                    'email': _email,
                    'status': 'failed',
                    'error': None,
                }
                if _user.role == 'reseller':
                    if not _has_client_access(_user, _server.id, _email, inbound_id=_inbound_id):
                        report['error'] = 'Access denied'
                        return False, 'Access denied', 403, report

                try:
                    factor = float(_data.get('factor') or 0)
                except (TypeError, ValueError):
                    factor = 0
                if factor <= 0:
                    report['error'] = 'Invalid factor'
                    return False, 'Invalid factor', 400, report
                mode = str(_data.get('mode') or 'set_remaining').strip().lower()

                target_client, err = _fetch_client_snapshot(_user, _server, _inbound_id, _email)
                if err:
                    report['error'] = err
                    return False, err, 400, report
                if not isinstance(target_client, dict):
                    report['error'] = 'Client not found'
                    return False, 'Client not found', 404, report

                try:
                    current_total_bytes = int(target_client.get('totalGB') or 0)
                except (TypeError, ValueError):
                    current_total_bytes = 0
                if current_total_bytes <= 0:
                    report.update({'status': 'skipped', 'reason': 'unlimited_volume'})
                    return True, 'Unlimited volume skipped', 204, report

                try:
                    used_bytes = max(0, int(target_client.get('up') or 0) + int(target_client.get('down') or 0))
                except (TypeError, ValueError):
                    used_bytes = 0
                remaining_bytes = max(0, current_total_bytes - used_bytes)
                remaining_gb = remaining_bytes / float(1024 ** 3)

                # Optional skip range: if remaining falls within [skip_min_gb, skip_max_gb], do nothing.
                try:
                    skip_min = float(_data.get('skip_min_gb')) if _data.get('skip_min_gb') is not None else None
                except (TypeError, ValueError):
                    skip_min = None
                try:
                    skip_max = float(_data.get('skip_max_gb')) if _data.get('skip_max_gb') is not None else None
                except (TypeError, ValueError):
                    skip_max = None

                in_skip_range = (
                    (skip_min is not None or skip_max is not None) and
                    (skip_min is None or remaining_gb >= skip_min) and
                    (skip_max is None or remaining_gb <= skip_max)
                )
                if in_skip_range:
                    report.update({
                        'status': 'skipped',
                        'reason': 'in_skip_range',
                        'before_remaining_gb': round(remaining_gb, 3),
                    })
                    return True, 'Skipped (remaining in skip range)', 204, report

                new_remaining_bytes = int(round(remaining_bytes * factor))

                report.update({
                    'before_total_gb': _bytes_to_gb_float(current_total_bytes),
                    'before_remaining_gb': _bytes_to_gb_float(remaining_bytes),
                    'used_gb': _bytes_to_gb_float(used_bytes),
                    'factor': factor,
                    'mode': mode,
                })

                if mode == 'reset_and_set':
                    target_client['up'] = 0
                    target_client['down'] = 0
                    target_client['totalGB'] = new_remaining_bytes
                    new_total_bytes = new_remaining_bytes
                else:  # set_remaining
                    new_total_bytes = used_bytes + new_remaining_bytes
                    target_client['totalGB'] = new_total_bytes

                ok, update_err, _status = _post_client_update(_server, _inbound_id, _email, target_client)
                if ok:
                    report.update({
                        'status': 'changed',
                        'after_total_gb': _bytes_to_gb_float(new_total_bytes),
                        'after_remaining_gb': _bytes_to_gb_float(new_remaining_bytes),
                    })
                else:
                    report['error'] = update_err
                return ok, update_err, _status, report

            for item in clients:
                client_ref = item
                if not isinstance(item, dict):
                    _bulk_progress_update(job_id, failed=1)
                    continue

                try:
                    server_id = int(item.get('server_id'))
                    inbound_id = int(item.get('inbound_id'))
                    email = (item.get('email') or '').strip()
                    client_uuid = (item.get('client_uuid') or '').strip()
                except (TypeError, ValueError):
                    server_id = None
                    inbound_id = None
                    email = ''
                    client_uuid = ''

                if not server_id or inbound_id is None or not email:
                    _bulk_progress_update(job_id, failed=1,
                        error_entry={'client': client_ref, 'error': 'server_id, inbound_id and email are required'})
                    continue

                server = servers_by_id.get(server_id)
                if not server:
                    _bulk_progress_update(job_id, failed=1,
                        error_entry={'client': {'server_id': server_id, 'inbound_id': inbound_id, 'email': email},
                                     'error': 'Server not found'})
                    continue

                # Optional conditional targeting
                if normalized_conditions and (normalized_conditions.get('enable_state') != 'any' or normalized_conditions.get('expiry_type') != 'any'):
                    try:
                        snap, snap_err = _fetch_client_snapshot(user, server, inbound_id, email)
                        if snap_err:
                            raise RuntimeError(snap_err)
                        if not _matches_conditions(snap, normalized_conditions):
                            _bulk_progress_update(job_id, skipped=1)
                            continue
                    except Exception as exc:
                        _bulk_progress_update(job_id, failed=1,
                            error_entry={'client': {'server_id': server_id, 'inbound_id': inbound_id, 'email': email},
                                         'error': str(exc) or 'Condition check failed'})
                        continue

                ok = False
                err = None
                skipped = False
                report_row = None

                if action in ('enable', 'disable'):
                    ok, err, _status = _toggle_client_core(user, server, inbound_id, email, action == 'enable')
                elif action == 'delete':
                    ok, err, _status = _delete_client_core(user, server, inbound_id, email)
                elif action == 'add_days':
                    delta = data.get('days_delta')
                    ok, err, _status = _apply_client_limit_delta(user, server, inbound_id, email, days_delta=delta, volume_gb_delta=None)
                    if ok:
                        _clear_message_cooldown(email, server_id)
                elif action == 'add_volume':
                    delta = data.get('volume_gb_delta')
                    ok, err, _status = _apply_client_limit_delta(user, server, inbound_id, email, days_delta=None, volume_gb_delta=delta)
                elif action == 'volume_policy':
                    ok, err, _status, report_row = _apply_client_volume_policy(user, server, inbound_id, email, data.get('volume_rules'))
                    skipped = bool(ok and _status == 204)
                elif action == 'volume_multiplier':
                    ok, err, _status, report_row = _apply_client_volume_multiplier(user, server, inbound_id, email, data)
                    skipped = bool(ok and _status == 204)
                elif action == 'set_start_after_use':
                    snap, snap_err = _fetch_client_snapshot(user, server, inbound_id, email)
                    if snap_err:
                        ok, err, _status = False, snap_err, 400
                    elif not isinstance(snap, dict):
                        ok, err, _status = False, 'Client not found', 404
                    else:
                        try:
                            exp = int(snap.get('expiryTime') or 0)
                        except (TypeError, ValueError):
                            exp = 0
                        if exp <= 0:
                            # already start_after_use (negative) or unlimited (0) → skip
                            ok, err, _status = True, None, 204
                            skipped = True
                        else:
                            now_ms = int(time.time() * 1000)
                            remaining_ms = exp - now_ms
                            if remaining_ms <= 0:
                                # expired → skip
                                ok, err, _status = True, None, 204
                                skipped = True
                            else:
                                snap['expiryTime'] = -remaining_ms
                                ok, err, _status = _post_client_update(server, inbound_id, email, snap)
                elif action == 'set_inbounds':
                    _mode = (data.get('inbound_mode') or 'set').lower()
                    _tids = data.get('inbound_ids') or []
                    ok, err, _status, _info = _reconcile_client_inbounds(
                        user, server, email, client_uuid, _tids, _mode)
                    skipped = bool(ok and _status == 204)
                elif action == 'assign_owner':
                    email_l = (email or '').lower()
                    try:
                        key_filters = []
                        if client_uuid:
                            key_filters.append(ClientOwnership.client_uuid == client_uuid)
                        if email_l:
                            key_filters.append(func.lower(ClientOwnership.client_email) == email_l)
                        q = ClientOwnership.query.filter(ClientOwnership.server_id == server_id)
                        if key_filters:
                            q = q.filter(or_(*key_filters))
                        q.delete(synchronize_session=False)
                        db.session.flush()  # Ensure delete is sent to DB before insert

                        ownership = ClientOwnership(
                            reseller_id=reseller_id,
                            server_id=server_id,
                            inbound_id=inbound_id,
                            client_email=email,
                            client_uuid=client_uuid if client_uuid else None
                        )
                        db.session.add(ownership)

                        # Keep reseller "Allowed Servers" in sync with assignments
                        try:
                            ensure_reseller_allowed_for_assignment(reseller, server_id, inbound_id)
                        except Exception:
                            pass

                        db.session.commit()
                        ok = True
                    except Exception as exc:
                        db.session.rollback()
                        ok = False
                        err = str(exc)
                elif action == 'unassign_owner':
                    email_l = (email or '').lower()
                    try:
                        key_filters = []
                        if client_uuid:
                            key_filters.append(ClientOwnership.client_uuid == client_uuid)
                        if email_l:
                            key_filters.append(func.lower(ClientOwnership.client_email) == email_l)
                        q = ClientOwnership.query.filter(ClientOwnership.server_id == server_id)
                        if key_filters:
                            q = q.filter(or_(*key_filters))
                        q.delete(synchronize_session=False)
                        db.session.commit()
                        ok = True
                    except Exception as exc:
                        db.session.rollback()
                        ok = False
                        err = str(exc)
                else:
                    ok = False
                    err = 'Invalid action'

                _bulk_progress_update(
                    job_id,
                    success=1 if (ok and not skipped) else 0,
                    skipped=1 if skipped else 0,
                    failed=1 if (not ok and not skipped) else 0,
                    error_entry=(None if (ok or skipped) else
                                 {'client': {'server_id': server_id, 'inbound_id': inbound_id, 'email': email},
                                  'error': err or 'Failed'}),
                    report_row=(report_row if (action == 'volume_policy' and isinstance(report_row, dict)) else None),
                )

        with BULK_JOBS_LOCK:
            job = BULK_JOBS.get(job_id) or {}
            job['state'] = 'done'
            job['finished_at'] = _utc_iso_now()
            BULK_JOBS[job_id] = job
            _save_bulk_jobs_locked()
            _prune_bulk_jobs_locked()
    except Exception as e:
        with BULK_JOBS_LOCK:
            job = BULK_JOBS.get(job_id) or {}
            job['state'] = 'error'
            job['error'] = str(e)
            job['finished_at'] = _utc_iso_now()
            BULK_JOBS[job_id] = job
            _save_bulk_jobs_locked()
            _prune_bulk_jobs_locked()
    finally:
        # Free the in-memory client list once the job is finished
        BULK_JOBS_CLIENTS.pop(job_id, None)


def _prune_refresh_jobs_locked():
    if len(REFRESH_JOBS) <= REFRESH_MAX_JOBS:
        return
    # prune oldest finished jobs first
    jobs_sorted = sorted(REFRESH_JOBS.items(), key=lambda kv: kv[1].get('created_at_ts', 0))
    to_delete = max(0, len(REFRESH_JOBS) - REFRESH_MAX_JOBS)
    deleted = 0
    for job_id, job in jobs_sorted:
        if deleted >= to_delete:
            break
        if job.get('state') in ('done', 'error'):
            REFRESH_JOBS.pop(job_id, None)
            deleted += 1


def _refresh_job_redis_key(job_id: str) -> str:
    return f'{REDIS_REFRESH_JOB_PREFIX}{job_id}'


def _refresh_scope_redis_key(scope_key: str) -> str:
    return f'{REDIS_REFRESH_SCOPE_PREFIX}{scope_key}'


def _get_refresh_job(job_id: str):
    """Read refresh state from Redis when available, with local fallback."""
    from app import app  # deferred: app-level helper, avoids circular import
    client = get_redis()
    if client is not None:
        try:
            raw = client.get(_refresh_job_redis_key(job_id))
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                job = json.loads(raw)
                if isinstance(job, dict):
                    return job
        except Exception as exc:
            app.logger.warning('Failed to read refresh job %s from Redis: %s', job_id, exc)
    with REFRESH_JOBS_LOCK:
        job = REFRESH_JOBS.get(job_id)
        return copy.deepcopy(job) if job else None


def _store_refresh_job(job: dict):
    """Persist lightweight refresh progress for every web worker to poll."""
    from app import app  # deferred: app-level helper, avoids circular import
    if not isinstance(job, dict) or not job.get('id'):
        return
    job_copy = copy.deepcopy(job)
    with REFRESH_JOBS_LOCK:
        REFRESH_JOBS[job_copy['id']] = job_copy
        _prune_refresh_jobs_locked()
    client = get_redis()
    if client is not None:
        try:
            client.set(
                _refresh_job_redis_key(job_copy['id']),
                json.dumps(job_copy, ensure_ascii=False, default=str),
                ex=REDIS_REFRESH_JOB_TTL,
            )
        except Exception as exc:
            app.logger.warning('Failed to persist refresh job %s: %s', job_copy['id'], exc)


def _release_refresh_scope(job: dict):
    from app import app  # deferred: app-level helper, avoids circular import
    client = get_redis()
    if client is None or not isinstance(job, dict):
        return
    scope_key = job.get('scope_key')
    job_id = str(job.get('id') or '')
    if not scope_key or not job_id:
        return
    key = _refresh_scope_redis_key(scope_key)
    try:
        # Delete only our own claim; a newer job may already own this scope.
        current = client.get(key)
        current = current.decode('utf-8') if isinstance(current, bytes) else str(current or '')
        if current == job_id:
            client.delete(key)
    except Exception as exc:
        app.logger.warning('Failed to release refresh scope %s: %s', scope_key, exc)


def _backoff_get(server_id: int) -> dict:
    try:
        return REFRESH_BACKOFF.get(int(server_id)) or {}
    except Exception:
        return {}


def _backoff_should_skip(server_id: int, now_ts: float) -> bool:
    info = _backoff_get(server_id)
    return float(info.get('next_allowed_at', 0) or 0) > float(now_ts)


def _backoff_record_failure(server_id: int, error: str):
    try:
        sid = int(server_id)
    except Exception:
        return
    now = time.time()
    info = REFRESH_BACKOFF.get(sid) or {'fail_count': 0, 'next_allowed_at': 0, 'last_error': '', 'last_failed_at': 0}
    fail_count = int(info.get('fail_count', 0) or 0) + 1
    # exponential backoff: 5,10,20,40,80,160,300...
    delay = min(REFRESH_MAX_BACKOFF_SEC, (2 ** min(fail_count, 6)) * 5)
    info.update({
        'fail_count': fail_count,
        'next_allowed_at': now + delay,
        'last_error': (error or 'Error')[:400],
        'last_failed_at': now,
    })
    REFRESH_BACKOFF[sid] = info


def _backoff_record_success(server_id: int):
    try:
        sid = int(server_id)
    except Exception:
        return
    if sid in REFRESH_BACKOFF:
        REFRESH_BACKOFF[sid] = {'fail_count': 0, 'next_allowed_at': 0, 'last_error': '', 'last_failed_at': 0}


def _check_server_reachable(server: 'Server', timeout_sec: float = 2.0):
    try:
        base, webpath = extract_base_and_webpath(server.host)
        url = f"{base}{webpath}/login"
        resp = requests.get(url, timeout=timeout_sec, verify=False, allow_redirects=True)
        return (resp.status_code < 500), None
    except Exception as e:
        return False, str(e)


def _update_reachability_status(servers, force: bool = False):
    from app import _utc_iso_now  # deferred: app-level helper, avoids circular import
    now_iso = _utc_iso_now()
    now_ts = time.time()
    existing_statuses = GLOBAL_SERVER_DATA.get('servers_status') or []
    status_map = {}
    for st in existing_statuses:
        try:
            if isinstance(st, dict) and 'server_id' in st:
                status_map[int(st.get('server_id'))] = st
        except Exception:
            continue

    for srv in servers or []:
        try:
            sid = int(srv.id)
        except Exception:
            continue
        if not force and _backoff_should_skip(sid, now_ts):
            info = _backoff_get(sid)
            st = status_map.get(sid) or {'server_id': sid}
            st['reachable'] = False
            st['reachable_error'] = f"Backoff (until {int(info.get('next_allowed_at', 0) or 0)})"
            st['reachable_checked_at'] = now_iso
            status_map[sid] = st
            continue

        ok, err = _check_server_reachable(srv)
        st = status_map.get(sid) or {'server_id': sid}
        st['reachable'] = bool(ok)
        st['reachable_error'] = None if ok else (err or 'Unreachable')
        st['reachable_checked_at'] = now_iso
        status_map[sid] = st

        if ok:
            _backoff_record_success(sid)
        else:
            _backoff_record_failure(sid, st['reachable_error'])

    # write back preserving server order (if we can)
    ordered = []
    try:
        id_order = [int(s.id) for s in servers]
        for sid in id_order:
            if sid in status_map:
                ordered.append(status_map[sid])
        # include any extra statuses that were not part of the requested servers
        for sid, st in status_map.items():
            if sid not in set(id_order):
                ordered.append(st)
    except Exception:
        ordered = list(status_map.values())
    GLOBAL_SERVER_DATA['servers_status'] = ordered
    GLOBAL_SERVER_DATA['last_update'] = _utc_iso_now()


def _run_refresh_job(job_id: str):
    from app import _run_snapshot_with_progress, _utc_iso_now, app, fetch_and_update_global_data  # deferred: app-level helper, avoids circular import
    job = _get_refresh_job(job_id)
    if not job:
        return
    job['state'] = 'running'
    job['started_at'] = _utc_iso_now()
    _store_refresh_job(job)

    try:
        # Important: background threads must run inside app context for SQLAlchemy.
        with app.app_context():
            with GLOBAL_REFRESH_LOCK:
                GLOBAL_SERVER_DATA['is_updating'] = True
                try:
                    mode = (job.get('mode') or 'full').strip().lower()
                    server_id = job.get('server_id')
                    force = bool(job.get('force'))
                    changed_server_ids = []

                    if mode == 'usage_snapshot':
                        _run_snapshot_with_progress()
                    elif mode == 'status':
                        servers_q = Server.query.filter_by(enabled=True).filter(
                            (Server.hidden == False) | (Server.hidden == None))
                        if server_id:
                            servers_q = servers_q.filter(Server.id == int(server_id))
                        servers = servers_q.all()
                        _update_reachability_status(servers, force=force)
                    else:
                        if server_id:
                            try:
                                fetch_and_update_server_data(int(server_id))
                                _backoff_record_success(int(server_id))
                                changed_server_ids = [int(server_id)]
                            except Exception as e:
                                _backoff_record_failure(int(server_id), str(e))
                                raise
                        else:
                            fetch_and_update_global_data(force=force)
                    # Propagate manual-refresh results to other workers (Redis mode).
                    publish_snapshot_to_redis(changed_server_ids)
                finally:
                    GLOBAL_SERVER_DATA['is_updating'] = False

        job = _get_refresh_job(job_id) or job
        job['state'] = 'done'
        job['finished_at'] = _utc_iso_now()
        _store_refresh_job(job)
    except Exception as e:
        job = _get_refresh_job(job_id) or job
        job['state'] = 'error'
        job['error'] = str(e)
        job['finished_at'] = _utc_iso_now()
        _store_refresh_job(job)
    finally:
        _release_refresh_scope(job)


def enqueue_refresh_job(mode: str = 'full', server_id=None, force: bool = False):
    from app import _utc_iso_now, app  # deferred: app-level helper, avoids circular import
    mode_norm = (mode or 'full').strip().lower()
    if mode_norm not in ('full', 'status', 'usage_snapshot'):
        mode_norm = 'full'

    sid = None
    try:
        if server_id not in (None, '', 'null'):
            sid = int(server_id)
    except Exception:
        sid = None

    scope_key = f"{mode_norm}:{sid if sid is not None else 'all'}"
    client = get_redis()
    if client is not None:
        try:
            scope_redis_key = _refresh_scope_redis_key(scope_key)
            existing_id = client.get(scope_redis_key)
            if existing_id:
                existing_id = existing_id.decode('utf-8') if isinstance(existing_id, bytes) else str(existing_id)
                existing = _get_refresh_job(existing_id)
                if existing and existing.get('state') in ('queued', 'running'):
                    return existing
                client.delete(scope_redis_key)
        except Exception as exc:
            app.logger.warning('Failed to inspect refresh queue scope: %s', exc)

    job_id = secrets.token_hex(8)
    job = {
        'id': job_id,
        'scope_key': scope_key,
        'mode': mode_norm,
        'server_id': sid,
        'force': bool(force),
        'state': 'queued',
        'created_at': _utc_iso_now(),
        'created_at_ts': time.time(),
        'started_at': None,
        'finished_at': None,
        'progress': {},
        'error': None,
    }
    _store_refresh_job(job)

    if client is not None:
        try:
            claimed = client.set(
                _refresh_scope_redis_key(scope_key), job_id,
                nx=True, ex=REDIS_REFRESH_JOB_TTL,
            )
            if not claimed:
                existing_id = client.get(_refresh_scope_redis_key(scope_key))
                if existing_id:
                    existing_id = existing_id.decode('utf-8') if isinstance(existing_id, bytes) else str(existing_id)
                    existing = _get_refresh_job(existing_id)
                    if existing:
                        return existing
            client.rpush(REDIS_REFRESH_QUEUE_KEY, job_id)
            client.expire(REDIS_REFRESH_QUEUE_KEY, REDIS_REFRESH_JOB_TTL)
            return job
        except Exception as exc:
            app.logger.error('Failed to enqueue refresh in Redis; using local worker: %s', exc)

    t = threading.Thread(target=_run_refresh_job, args=(job_id,), daemon=True)
    t.start()
    return job


def refresh_queue_worker():
    """Execute refresh requests outside Gunicorn web workers."""
    from app import app  # deferred: app-level helper, avoids circular import
    client = get_redis()
    if client is None:
        app.logger.error('Refresh queue worker requires Redis; worker stopped.')
        return
    app.logger.info('[RefreshQueue] worker started (PID=%s)', os.getpid())
    try:
        # Recover jobs atomically reserved by a previous background process that
        # exited before acknowledging completion.
        abandoned = client.lrange(REDIS_REFRESH_PROCESSING_KEY, 0, -1)
        for raw_job_id in abandoned:
            job_id = raw_job_id.decode('utf-8') if isinstance(raw_job_id, bytes) else str(raw_job_id)
            job = _get_refresh_job(job_id)
            if job and job.get('state') in ('queued', 'running'):
                job['state'] = 'queued'
                job['started_at'] = None
                _store_refresh_job(job)
                client.lpush(REDIS_REFRESH_QUEUE_KEY, job_id)
        if abandoned:
            client.delete(REDIS_REFRESH_PROCESSING_KEY)
    except Exception as exc:
        app.logger.warning('Failed to recover refresh queue reservations: %s', exc)

    while True:
        try:
            raw_job_id = client.rpoplpush(REDIS_REFRESH_QUEUE_KEY, REDIS_REFRESH_PROCESSING_KEY)
            if not raw_job_id:
                time.sleep(0.5)
                continue
            job_id = raw_job_id.decode('utf-8') if isinstance(raw_job_id, bytes) else str(raw_job_id)
            try:
                _run_refresh_job(job_id)
            finally:
                client.lrem(REDIS_REFRESH_PROCESSING_KEY, 1, raw_job_id)
        except Exception as exc:
            app.logger.exception('Refresh queue worker failed: %s', exc)
            time.sleep(2)


def _recompute_global_stats_from_server_statuses(server_statuses):
    """Recompute aggregate stats from cached per-server stats."""
    from app import format_bytes  # deferred: app-level helper, avoids circular import
    total_stats = {
        "total_inbounds": 0,
        "active_inbounds": 0,
        "total_clients": 0,
        "online_clients": 0,
        "active_clients": 0,
        "inactive_clients": 0,
        "not_started_clients": 0,
        "unlimited_expiry_clients": 0,
        "unlimited_volume_clients": 0,
        "upload_raw": 0,
        "download_raw": 0,
        "remaining_raw": 0,
        "limited_clients": 0,
    }

    for status in server_statuses or []:
        if not isinstance(status, dict) or not status.get("success"):
            continue
        stats = status.get("stats")
        if not isinstance(stats, dict):
            continue
        for k in list(total_stats.keys()):
            v = stats.get(k, 0)
            if isinstance(v, int):
                total_stats[k] += v

    total_stats["total_upload"] = format_bytes(total_stats["upload_raw"])
    total_stats["total_download"] = format_bytes(total_stats["download_raw"])
    total_stats["total_traffic"] = format_bytes(total_stats["upload_raw"] + total_stats["download_raw"])
    total_stats["total_remaining"] = format_bytes(total_stats["remaining_raw"])
    return total_stats


def fetch_and_update_server_data(server_id: int):
    """Fetch a single server's inbounds and update GLOBAL_SERVER_DATA in-place."""
    from app import app, process_inbounds  # deferred: app-level helper, avoids circular import
    server = db.session.get(Server, int(server_id))
    if not server or not server.enabled:
        raise ValueError("Server not found or disabled")
    if server.hidden:
        raise ValueError("Server is hidden — skipping fetch")

    admin_user = Admin.query.filter(or_(Admin.is_superadmin == True, Admin.role == 'superadmin')).first()
    if not admin_user:
        admin_user = SimpleNamespace(role='superadmin', id=0, is_superadmin=True)

    session_obj, error = get_xui_session(server)
    if error:
        raise RuntimeError(error)

    inbounds, fetch_error, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
    if fetch_error:
        raise RuntimeError(fetch_error)

    online_index, _ = fetch_onlines(session_obj, server.host, server.panel_type)
    status_payload, status_error, _status_type = fetch_server_status(session_obj, server.host, server.panel_type)

    # Enrich status_payload with online_count from onlines endpoint
    if online_index:
        online_count = len(online_index.get('pairs', set())) + len(online_index.get('emails', set()))
        if status_payload is None:
            status_payload = {}
        if status_payload.get('online_count') is None and online_count > 0:
            status_payload['online_count'] = online_count

    if persist_detected_panel_type(server, detected_type):
        app.logger.info("Detected panel type for server %s as %s", server.id, detected_type)

    if not isinstance(inbounds, list):
        inbounds = []
    processed, stats = process_inbounds(inbounds, server, admin_user, '*', {}, online_index=online_index)

    # Update cache atomically under lock
    # - Replace only this server's inbounds
    # - Preserve the previous ordering position (do NOT move the server's block to the end)
    # - Update per-server status stats
    # - Recompute aggregate stats
    existing_inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    new_block = list(processed or [])

    # Find the first occurrence index of this server in the existing list (if any)
    first_idx = None
    for idx, item in enumerate(existing_inbounds):
        try:
            if int(item.get('server_id', -1)) == int(server.id):
                first_idx = idx
                break
        except Exception:
            continue

    without_server = []
    for item in existing_inbounds:
        try:
            if int(item.get('server_id', -1)) == int(server.id):
                continue
        except Exception:
            pass
        without_server.append(item)

    if first_idx is None:
        # Server didn't exist in cache before: append to end
        GLOBAL_SERVER_DATA['inbounds'] = without_server + new_block
    else:
        # Insert new block at the previous position
        insert_at = min(max(first_idx, 0), len(without_server))
        GLOBAL_SERVER_DATA['inbounds'] = without_server[:insert_at] + new_block + without_server[insert_at:]

    statuses = GLOBAL_SERVER_DATA.get('servers_status') or []
    updated = False
    for st in statuses:
        if isinstance(st, dict) and int(st.get('server_id', -1)) == int(server.id):
            status_payload = status_payload or {}
            st.update({
                "server_id": server.id,
                "success": True,
                "stats": stats,
                "panel_type": server.panel_type,
                "xui_version": status_payload.get('xui_version'),
                "xray_version": status_payload.get('xray_version'),
                "xray_state": status_payload.get('xray_state'),
                "xray_core": status_payload.get('xray_core'),
                "online_count": status_payload.get('online_count'),
                "panel_status_error": status_error if status_error else None,
                "panel_status_checked_at": datetime.utcnow().isoformat()
            })
            updated = True
            break
    if not updated:
        status_payload = status_payload or {}
        statuses.append({
            "server_id": server.id,
            "success": True,
            "stats": stats,
            "panel_type": server.panel_type,
            "xui_version": status_payload.get('xui_version'),
            "xray_version": status_payload.get('xray_version'),
            "xray_state": status_payload.get('xray_state'),
            "xray_core": status_payload.get('xray_core'),
            "online_count": status_payload.get('online_count'),
            "panel_status_error": status_error if status_error else None,
            "panel_status_checked_at": datetime.utcnow().isoformat()
        })
    GLOBAL_SERVER_DATA['servers_status'] = statuses

    GLOBAL_SERVER_DATA['stats'] = _recompute_global_stats_from_server_statuses(statuses)
    GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()


# ── Write-through cache ──────────────────────────────────────────────────────
# After any successful panel write we mutate GLOBAL_SERVER_DATA directly (and
# republish to Redis) so the dashboard reflects the change INSTANTLY without the
# slow per-server panel re-fetch. The background fetcher reconciles on its next
# cycle, so any small drift here is self-healing.

def _recompute_cached_client(cd, thresholds=None, lang=None):
    """Recompute a processed client's derived display fields from its raw_client.
    Mirrors process_inbounds() so a patched row matches a full fetch."""
    from app import _compute_client_service_state, _get_dashboard_status_thresholds, _get_panel_ui_lang, format_bytes_gb_tb, format_remaining_days  # deferred: app-level helper, avoids circular import
    raw = cd.get('raw_client') or {}
    if thresholds is None:
        thresholds = _get_dashboard_status_thresholds()
    if lang is None:
        lang = _get_panel_ui_lang()
    up = int(cd.get('up') or 0)
    down = int(cd.get('down') or 0)
    try:
        total_bytes = int(raw.get('totalGB') or 0)
    except (TypeError, ValueError):
        total_bytes = 0

    cd['totalGB'] = total_bytes
    cd['totalGB_formatted'] = format_bytes_gb_tb(total_bytes) if total_bytes > 0 else "Unlimited"

    if total_bytes > 0:
        remaining_bytes = max(total_bytes - (up + down), 0)
        rf = format_bytes_gb_tb(remaining_bytes)
        vs = ""
        if remaining_bytes <= 0:
            rf, vs = "Suspended", "suspended"
        elif remaining_bytes < int(float(thresholds.get('low_volume_gb', 1.0)) * (1024 ** 3)):
            rf, vs = f"{rf} Low", "low"
        cd['remaining_bytes'] = remaining_bytes
        cd['remaining_formatted'] = rf
        cd['volume_status'] = vs
    else:
        remaining_bytes = None
        cd['remaining_bytes'] = -1
        cd['remaining_formatted'] = "Unlimited"
        cd['volume_status'] = "expiry-start-after"

    expiry_raw = raw.get('expiryTime', 0)
    expiry_info = format_remaining_days(expiry_raw, lang=lang)
    cd['expiryTime'] = expiry_info['text']
    cd['expiryTimestamp'] = expiry_raw
    cd['expiryType'] = expiry_info['type']

    try:
        state = _compute_client_service_state(
            enabled=bool(raw.get('enable', True)),
            total_bytes=int(total_bytes or 0),
            remaining_bytes=(None if remaining_bytes is None else int(remaining_bytes)),
            expiry_ts=int(expiry_raw or 0),
            expiry_info=expiry_info,
            thresholds=thresholds,
            lang=lang,
        )
        cd['service_state'] = state.get('key', 'active')
        cd['service_state_label'] = state.get('label', '')
        cd['service_state_emoji'] = state.get('emoji', '')
        cd['service_state_tag'] = state.get('tag', 'ok')
    except Exception:
        pass

    cd['enable'] = bool(raw.get('enable', True))
    cd['comment'] = (raw.get('comment') or '').strip()
    cd['email'] = raw.get('email', cd.get('email'))
    cd['id'] = raw.get('id', cd.get('id'))


def _iter_cached_client_copies(server_id, email, client_uuid=None):
    """Yield (inbound, processed_client) for every cached copy of a client on a
    server (a v3 client appears once per assigned inbound)."""
    try:
        sid = int(server_id)
    except (TypeError, ValueError):
        return
    email_l = (email or '').strip().lower()
    uuid_l = (client_uuid or '').strip().lower()
    for ib in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(ib.get('server_id', -1)) != sid:
                continue
        except Exception:
            continue
        for cd in (ib.get('clients') or []):
            ce = (cd.get('email') or '').strip().lower()
            cu = (cd.get('id') or '').strip().lower()
            if (email_l and ce == email_l) or (uuid_l and cu == uuid_l):
                yield ib, cd


def patch_cached_client(server_id, email, *, client_uuid=None, new_email=None,
                        comment=None, total_gb_bytes=None, expiry_ts=None,
                        enable=None, up=None, down=None, publish=True):
    """Write-through: update every cached copy of a client after a panel write."""
    from app import _get_dashboard_status_thresholds, _get_panel_ui_lang, app, format_bytes  # deferred: app-level helper, avoids circular import
    changed = False
    try:
        with GLOBAL_REFRESH_LOCK:
            thresholds = _get_dashboard_status_thresholds()
            lang = _get_panel_ui_lang()
            for _ib, cd in _iter_cached_client_copies(server_id, email, client_uuid):
                raw = cd.get('raw_client')
                if not isinstance(raw, dict):
                    raw = {}
                    cd['raw_client'] = raw
                if comment is not None:
                    raw['comment'] = comment
                if total_gb_bytes is not None:
                    raw['totalGB'] = int(total_gb_bytes)
                if expiry_ts is not None:
                    raw['expiryTime'] = int(expiry_ts)
                if enable is not None:
                    raw['enable'] = bool(enable)
                if new_email is not None:
                    raw['email'] = new_email
                if up is not None:
                    cd['up'] = int(up)
                    cd['up_formatted'] = format_bytes(int(up))
                if down is not None:
                    cd['down'] = int(down)
                    cd['down_formatted'] = format_bytes(int(down))
                _recompute_cached_client(cd, thresholds, lang)
                changed = True
            if changed:
                GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()
    except Exception as exc:
        app.logger.debug("patch_cached_client failed: %s", exc)
        return False
    if changed and publish:
        try:
            publish_snapshot_to_redis([server_id])
        except Exception:
            pass
    return changed


def add_cached_client(server_id, inbound_ids, raw_client, *, publish=True):
    """Write-through: append a newly-created client to every target inbound.

    Client arrays are kept in panel creation order, so appending here makes the
    recent-user endpoints (which iterate them in reverse) immediately accurate.
    This also publishes the change to Redis for other gunicorn workers instead
    of relying on the browser's short-lived optimistic override.
    """
    from app import _get_dashboard_status_thresholds, _get_panel_ui_lang, app, format_bytes  # deferred: app-level helper, avoids circular import
    changed = False
    try:
        sid = int(server_id)
        target_ids = {int(iid) for iid in (inbound_ids or [])}
        if not target_ids or not isinstance(raw_client, dict):
            return False
        email_l = str(raw_client.get('email') or '').strip().lower()
        uuid_l = str(raw_client.get('id') or '').strip().lower()
        if not email_l and not uuid_l:
            return False

        with GLOBAL_REFRESH_LOCK:
            thresholds = _get_dashboard_status_thresholds()
            lang = _get_panel_ui_lang()
            for ib in (GLOBAL_SERVER_DATA.get('inbounds') or []):
                try:
                    if int(ib.get('server_id', -1)) != sid or int(ib.get('id', -1)) not in target_ids:
                        continue
                except (TypeError, ValueError):
                    continue

                clients = ib.setdefault('clients', [])
                duplicate = False
                for cd in clients:
                    ce = str(cd.get('email') or '').strip().lower()
                    cu = str(cd.get('id') or '').strip().lower()
                    if (email_l and ce == email_l) or (uuid_l and cu == uuid_l):
                        duplicate = True
                        break
                if duplicate:
                    continue

                raw = copy.deepcopy(raw_client)
                cached = {
                    'server_id': sid,
                    'inbound_id': int(ib.get('id')),
                    'email': raw.get('email'),
                    'id': raw.get('id'),
                    'up': 0,
                    'down': 0,
                    'up_formatted': format_bytes(0),
                    'down_formatted': format_bytes(0),
                    'raw_client': raw,
                }
                _recompute_cached_client(cached, thresholds, lang)
                clients.append(cached)
                ib['client_count'] = len(clients)
                if raw.get('enable', True):
                    ib['active_count'] = int(ib.get('active_count') or 0) + 1
                changed = True

            if changed:
                GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()
    except Exception as exc:
        app.logger.debug("add_cached_client failed: %s", exc)
        return False
    if changed and publish:
        try:
            publish_snapshot_to_redis([server_id])
        except Exception:
            pass
    return changed


def remove_cached_client(server_id, email, *, client_uuid=None, inbound_id=None, publish=True):
    """Write-through: drop a client from cache (all inbounds, or just one)."""
    from app import app  # deferred: app-level helper, avoids circular import
    removed = False
    try:
        with GLOBAL_REFRESH_LOCK:
            try:
                sid = int(server_id)
            except (TypeError, ValueError):
                return False
            email_l = (email or '').strip().lower()
            uuid_l = (client_uuid or '').strip().lower()
            for ib in (GLOBAL_SERVER_DATA.get('inbounds') or []):
                try:
                    if int(ib.get('server_id', -1)) != sid:
                        continue
                except Exception:
                    continue
                if inbound_id is not None:
                    try:
                        if int(ib.get('id', -1)) != int(inbound_id):
                            continue
                    except Exception:
                        continue
                clients = ib.get('clients') or []
                kept = []
                for cd in clients:
                    ce = (cd.get('email') or '').strip().lower()
                    cu = (cd.get('id') or '').strip().lower()
                    if (email_l and ce == email_l) or (uuid_l and cu == uuid_l):
                        removed = True
                        continue
                    kept.append(cd)
                if len(kept) != len(clients):
                    ib['clients'] = kept
            if removed:
                GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()
    except Exception as exc:
        app.logger.debug("remove_cached_client failed: %s", exc)
        return False
    if removed and publish:
        try:
            publish_snapshot_to_redis([server_id])
        except Exception:
            pass
    return removed


def clone_cached_client_into_inbound(server_id, inbound_id, email, client_uuid=None, publish=True):
    """Clone an existing cached processed client into another inbound block on the
    same server (used when a v3 client is newly assigned to an inbound)."""
    from app import app  # deferred: app-level helper, avoids circular import
    done = False
    try:
        with GLOBAL_REFRESH_LOCK:
            try:
                sid = int(server_id)
                iid = int(inbound_id)
            except (TypeError, ValueError):
                return False
            email_l = (email or '').strip().lower()
            uuid_l = (client_uuid or '').strip().lower()
            source = None
            target_ib = None
            for ib in (GLOBAL_SERVER_DATA.get('inbounds') or []):
                try:
                    if int(ib.get('server_id', -1)) != sid:
                        continue
                except Exception:
                    continue
                try:
                    if int(ib.get('id', -1)) == iid:
                        target_ib = ib
                except Exception:
                    pass
                if source is None:
                    for cd in (ib.get('clients') or []):
                        ce = (cd.get('email') or '').strip().lower()
                        cu = (cd.get('id') or '').strip().lower()
                        if (email_l and ce == email_l) or (uuid_l and cu == uuid_l):
                            source = cd
                            break
            if target_ib is None or source is None:
                return False
            tgt_email = (source.get('email') or '').strip().lower()
            for cd in (target_ib.get('clients') or []):
                if (cd.get('email') or '').strip().lower() == tgt_email:
                    return False  # already present
            clone = copy.deepcopy(source)
            clone['inbound_id'] = iid
            target_ib.setdefault('clients', []).append(clone)
            GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()
            done = True
    except Exception as exc:
        app.logger.debug("clone_cached_client_into_inbound failed: %s", exc)
        return False
    if done and publish:
        try:
            publish_snapshot_to_redis([server_id])
        except Exception:
            pass
    return done


# Guard to avoid starting background threads multiple times (important for gunicorn workers / dev reload)
