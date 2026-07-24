"""Backup services extracted from app.py.

Database backup/restore (SQLite + PostgreSQL), full-migration bundles, and
the Telegram backup pipeline (settings, proxies, upload).

Reverse dependencies on app.py (Flask ``app``, APP_VERSION, BACKUP_DIR,
RECEIPTS_DIR, _APP_FILES_DIR_NAME, _parse_bool, build_panel_url,
format_jalali) are imported lazily inside the functions that need them --
never at module level (see panel/models/_helpers.py).
"""

import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

import requests
from jdatetime import datetime as jdatetime_class
from werkzeug.utils import secure_filename

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from panel.adapters.xui import _safe_response_json, get_xui_session
from panel.extensions import db
from panel.models import Server, SystemSetting, TelegramEgressProfile
from telegram_diagnostics import redact_connection_error

TELEGRAM_BACKUP_TMP_DIR = None
TELEGRAM_BACKUP_LOCK = threading.Lock()


def init_backup_tmp_dir(flask_app):
    """Bind the telegram-backup temp dir to the Flask app's instance path.

    Called from app.py immediately after importing this module so the
    directory exists at import time, matching the previous inline behavior.
    """
    global TELEGRAM_BACKUP_TMP_DIR
    TELEGRAM_BACKUP_TMP_DIR = os.path.join(flask_app.instance_path, 'telegram_backup_tmp')
    os.makedirs(TELEGRAM_BACKUP_TMP_DIR, exist_ok=True)
    return TELEGRAM_BACKUP_TMP_DIR


def _telegram_backup_tmp_dir() -> str:
    if TELEGRAM_BACKUP_TMP_DIR is None:
        raise RuntimeError('TELEGRAM_BACKUP_TMP_DIR is not initialized; call init_backup_tmp_dir(app) first')
    return TELEGRAM_BACKUP_TMP_DIR


def _db_uri() -> str:
    from app import app
    return (app.config.get('SQLALCHEMY_DATABASE_URI') or '').strip()


def _is_sqlite_db() -> bool:
    return _db_uri().startswith('sqlite:')


def _is_postgres_db() -> bool:
    return _db_uri().startswith('postgresql:')


# Heavy analytics/log tables whose ROW DATA is excluded from "clean" backups.
# Schema is preserved; the data regenerates on its own after a restore.
_ANALYTICS_EXCLUDE_TABLES = ('usage_counter_state', 'usage_hourly', 'usage_daily', 'health_logs')


def _pg_dump_backup(dest_path: str, exclude_analytics: bool = True) -> None:
    """Create a PostgreSQL backup using pg_dump (custom format).

    Requires `pg_dump` to be available in PATH on the server.
    """
    pg_dump_bin = shutil.which('pg_dump')
    if not pg_dump_bin:
        raise RuntimeError("pg_dump not found in PATH. Install postgresql-client (pg_dump) on the server.")

    uri = _db_uri()
    parsed = urlparse(uri)

    env = os.environ.copy()
    if parsed.password:
        env['PGPASSWORD'] = parsed.password

    # Custom format is compact and best for pg_restore.
    cmd = [
        pg_dump_bin,
        '--format=custom',
        '--compress=9',
        '--no-owner',
        '--no-privileges',
    ]
    # Keep the schema for analytics tables but drop their (huge) row data so the
    # backup stays small and "clean". They regenerate automatically after restore.
    if exclude_analytics:
        for _t in _ANALYTICS_EXCLUDE_TABLES:
            cmd += ['--exclude-table-data', _t]
    cmd += ['--file', dest_path, '--dbname', uri]
    result = subprocess.run(cmd, env=env, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump exited with code {result.returncode}")


def _pg_restore_jobs() -> int:
    """Number of parallel pg_restore workers — half of CPUs, min 1, max 8."""
    cpus = os.cpu_count() or 1
    return max(1, min(cpus, 8))


def _pg_env_from_uri(uri: str) -> dict:
    parsed = urlparse(uri)
    env = os.environ.copy()
    if parsed.password:
        env['PGPASSWORD'] = parsed.password
    return env


def _pg_reset_public_schema(uri: str, env: dict) -> None:
    psql_bin = shutil.which('psql')
    if not psql_bin:
        raise RuntimeError("psql not found in PATH. Install postgresql-client (psql) on the server.")

    db.session.remove()
    db.engine.dispose()
    cmd = [
        psql_bin,
        '--dbname', uri,
        '--set', 'ON_ERROR_STOP=1',
        '--command', 'DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or 'Unknown error')[:1000].strip()
        raise RuntimeError(f"PostgreSQL schema reset failed (exit {result.returncode}): {err}")


def _pg_restore_backup(backup_path: str) -> None:
    """Restore a PostgreSQL database from a pg_dump file.

    Supports:
    - .dump  (pg_dump --format=custom)  → pg_restore --jobs=N (parallel)
    - .sql   (pg_dump --format=plain)   → psql
    Requires postgresql-client tools (pg_restore / psql) on the server.
    """
    uri = _db_uri()
    env = _pg_env_from_uri(uri)

    ext = os.path.splitext(backup_path)[1].lower()

    if ext == '.dump':
        bin_ = shutil.which('pg_restore')
        if not bin_:
            raise RuntimeError(
                "pg_restore not found in PATH. "
                "Install postgresql-client:  apt install postgresql-client"
            )
        jobs = _pg_restore_jobs()
        cmd = [
            bin_,
            '--no-owner', '--no-acl',
            f'--jobs={jobs}',
            '--dbname', uri,
            backup_path,
        ]
    elif ext == '.sql':
        bin_ = shutil.which('psql')
        if not bin_:
            raise RuntimeError(
                "psql not found in PATH. "
                "Install postgresql-client:  apt install postgresql-client"
            )
        cmd = [bin_, '--dbname', uri, '--file', backup_path]
    else:
        raise ValueError(f"Unsupported backup format for PostgreSQL restore: {ext!r}")

    _pg_reset_public_schema(uri, env)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or 'Unknown error')[:1000].strip()
        raise RuntimeError(f"Restore failed (exit {result.returncode}): {err}")


def _create_database_backup_file(prefix: str, exclude_analytics: bool = True) -> str:
    """Create a DB backup in BACKUP_DIR and return filename.

    exclude_analytics=True (default) drops regenerable usage rollups / health_logs
    row data so backups stay small; the schema is kept and data regenerates.
    """
    from app import BACKUP_DIR, app
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    if _is_sqlite_db():
        db_path = os.path.join(app.instance_path, 'servers.db')
        if not os.path.exists(db_path):
            raise FileNotFoundError('Database file not found')

        filename = f'{prefix}_{timestamp}.db'
        dest = os.path.join(BACKUP_DIR, filename)
        shutil.copy2(db_path, dest)
        # Clean copy: drop the heavy analytics rows and reclaim space (VACUUM)
        if exclude_analytics:
            try:
                con = sqlite3.connect(dest)
                for _t in _ANALYTICS_EXCLUDE_TABLES:
                    try:
                        con.execute(f'DELETE FROM {_t}')
                    except Exception:
                        pass
                con.commit()
                con.execute('VACUUM')
                con.commit()
                con.close()
            except Exception:
                pass
        return filename

    if _is_postgres_db():
        filename = f'{prefix}_{timestamp}.dump'
        dest = os.path.join(BACKUP_DIR, filename)
        _pg_dump_backup(dest, exclude_analytics=exclude_analytics)
        return filename

    raise RuntimeError('Unsupported database backend for backup')


# Directories that hold user-uploaded files which must travel WITH the database
# in a full migration (the DB only stores their URLs/paths, not the bytes).
def _migration_file_dirs() -> dict:
    """Map of archive-folder-name → absolute source dir for migration bundles."""
    from app import RECEIPTS_DIR, _APP_FILES_DIR_NAME, app
    static_folder = app.static_folder or ''
    return {
        'static_uploads':   os.path.join(static_folder, 'uploads'),       # receipts/images uploaded via editor
        'static_app_files': os.path.join(static_folder, _APP_FILES_DIR_NAME),  # app icons / screenshots / tutorial files
        'instance_receipts': RECEIPTS_DIR,                                # manual payment receipts
    }


def _create_full_migration_zip(prefix: str = 'migration') -> str:
    """Create a COMPLETE migration bundle (.zip) in BACKUP_DIR and return filename.

    Contains everything needed to move to another server:
      - database/<db>      the DB dump (.db for SQLite, .dump for PostgreSQL)
      - static_uploads/    uploaded images/receipts
      - static_app_files/  app icons, screenshots, tutorial files
      - instance_receipts/ manual payment receipts
      - manifest.json      metadata (db type, version, created at)
    """
    from app import APP_VERSION, BACKUP_DIR
    import zipfile
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # 1) Produce the DB dump first (reuses the existing logic)
    db_filename = _create_database_backup_file(f'{prefix}_db')
    db_path = os.path.join(BACKUP_DIR, db_filename)
    db_arcname = f"database/{db_filename}"

    zip_filename = f"{prefix}_full_{timestamp}.zip"
    zip_path = os.path.join(BACKUP_DIR, zip_filename)

    manifest = {
        'kind': 'eve_full_migration',
        'version': APP_VERSION,
        'created_at': datetime.now().isoformat(),
        'db_type': 'postgresql' if _is_postgres_db() else 'sqlite',
        'db_file': db_arcname,
        'included_dirs': [],
    }

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            # DB
            zf.write(db_path, db_arcname)
            # File directories
            for arc_root, src_dir in _migration_file_dirs().items():
                if not src_dir or not os.path.isdir(src_dir):
                    continue
                file_count = 0
                for root, _dirs, files in os.walk(src_dir):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        rel = os.path.relpath(fpath, src_dir)
                        zf.write(fpath, f"{arc_root}/{rel}")
                        file_count += 1
                if file_count:
                    manifest['included_dirs'].append({'dir': arc_root, 'files': file_count})
            # Manifest
            zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        # Remove the standalone DB dump (it's now inside the zip)
        try:
            os.remove(db_path)
        except Exception:
            pass

    return zip_filename


def _restore_full_migration_zip(zip_path: str, log=None):
    """Restore a full migration bundle: DB + uploaded file directories.

    `log` is an optional callable(str) for progress messages.
    Returns the db archive member path that was extracted+restored.
    """
    from app import BACKUP_DIR, app
    import zipfile, tempfile
    def _say(m):
        if log:
            try: log(m)
            except Exception: pass

    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = zf.namelist()
        # locate the DB file inside database/
        db_member = next((n for n in names if n.startswith('database/') and not n.endswith('/')), None)
        if not db_member:
            raise RuntimeError('Bundle has no database/ file')

        tmp_dir = tempfile.mkdtemp(prefix='eve-migrate-')
        try:
            # 1) Restore the database
            db_ext = os.path.splitext(db_member)[1].lower()
            extracted_db = zf.extract(db_member, tmp_dir)
            _say(f'Database file: {os.path.basename(db_member)}')

            if _is_sqlite_db():
                if db_ext != '.db':
                    raise RuntimeError(f'This server is SQLite but bundle DB is {db_ext}')
                db_path = os.path.join(app.instance_path, 'servers.db')
                if os.path.exists(db_path):
                    shutil.copy2(db_path, os.path.join(BACKUP_DIR, f'pre_restore_{datetime.now():%Y%m%d_%H%M%S}.db'))
                shutil.copy2(extracted_db, db_path)
                _say('✓ SQLite database restored')
            elif _is_postgres_db():
                if db_ext not in ('.dump', '.sql'):
                    raise RuntimeError(f'This server is PostgreSQL but bundle DB is {db_ext}')
                uri = _db_uri()
                env = _pg_env_from_uri(uri)
                _pg_reset_public_schema(uri, env)
                if db_ext == '.dump':
                    bin_ = shutil.which('pg_restore')
                    subprocess.run([bin_, '--no-owner', '--no-acl', f'--jobs={_pg_restore_jobs()}',
                                    '--dbname', uri, extracted_db], env=env, check=False,
                                   capture_output=True, text=True)
                else:
                    bin_ = shutil.which('psql')
                    subprocess.run([bin_, '--dbname', uri, '--file', extracted_db],
                                   env=env, check=False, capture_output=True, text=True)
                _say('✓ PostgreSQL database restored')
            else:
                raise RuntimeError('Unsupported database backend')

            # 2) Restore the uploaded file directories
            dir_map = _migration_file_dirs()
            for arc_root, dest_dir in dir_map.items():
                members = [n for n in names if n.startswith(arc_root + '/') and not n.endswith('/')]
                if not members:
                    continue
                os.makedirs(dest_dir, exist_ok=True)
                restored = 0
                for n in members:
                    rel = n[len(arc_root) + 1:]
                    target = os.path.join(dest_dir, rel)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(n) as src, open(target, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    restored += 1
                _say(f'✓ Restored {restored} file(s) → {arc_root}')
        finally:
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass


TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES = 60
TELEGRAM_BACKUP_MAX_INTERVAL_MINUTES = 1440


def _get_system_setting_value(key: str, default: str | None = None) -> str | None:
    setting = db.session.get(SystemSetting, key)
    return setting.value if setting else default


def _set_system_setting_value(key: str, value: str | int | bool | None):
    setting = db.session.get(SystemSetting, key)
    if not setting:
        setting = SystemSetting(key=key, value=str(value) if value is not None else '')
        db.session.add(setting)
    else:
        setting.value = str(value) if value is not None else ''
    return setting


def _parse_int(value, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        val = int(value)
    except Exception:
        val = default
    if min_value is not None and val < min_value:
        val = min_value
    if max_value is not None and val > max_value:
        val = max_value
    return val


def _parse_iso_datetime(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _normalize_proxy_url(raw: str | None) -> str:
    val = (raw or '').strip()
    if not val:
        return ''
    if '://' in val:
        return val
    return f"socks5h://{val}"


def _get_telegram_backup_settings() -> dict:
    from app import _parse_bool, format_jalali
    enabled = _parse_bool(_get_system_setting_value('telegram_backup_enabled', 'false'))
    send_panel_backup = _parse_bool(_get_system_setting_value('telegram_backup_send_panel_backup', 'false'))
    interval = _parse_int(
        _get_system_setting_value('telegram_backup_interval_minutes', str(TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES)),
        TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES,
        min_value=1,
        max_value=TELEGRAM_BACKUP_MAX_INTERVAL_MINUTES
    )
    use_proxy = _parse_bool(_get_system_setting_value('telegram_backup_use_proxy', 'false'))
    proxy_mode = (_get_system_setting_value('telegram_backup_proxy_mode', 'url') or 'url').strip().lower()
    if proxy_mode not in ('url', 'hostport'):
        proxy_mode = 'url'
    proxy_url = _normalize_proxy_url(_get_system_setting_value('telegram_backup_proxy_url', '') or '')
    proxy_host = (_get_system_setting_value('telegram_backup_proxy_host', '') or '').strip()
    proxy_port = _parse_int(_get_system_setting_value('telegram_backup_proxy_port', ''), 0, min_value=0, max_value=65535)
    proxy_username = (_get_system_setting_value('telegram_backup_proxy_username', '') or '').strip()
    proxy_password = (_get_system_setting_value('telegram_backup_proxy_password', '') or '').strip()
    route_source = (_get_system_setting_value('telegram_backup_route_source', '') or '').strip().lower()
    if route_source not in ('direct', 'manual_proxy', 'panel_account'):
        route_source = 'manual_proxy' if use_proxy else 'direct'
    egress_profile_id = _parse_int(
        _get_system_setting_value('telegram_backup_egress_profile_id', ''), 0, min_value=0,
    )
    managed_account = None
    if egress_profile_id:
        profile = db.session.get(TelegramEgressProfile, egress_profile_id)
        if profile:
            managed_account = {
                'profile_id': profile.id,
                'server_id': profile.server_id,
                'server_name': profile.server.name if profile.server else None,
                'inbound_id': profile.inbound_id,
                'client_id': profile.client_email_snapshot,
                'client_email': profile.client_email_snapshot,
                'runtime_status': profile.runtime_status,
                'health_status': profile.health_status,
            }
    last_run = _get_system_setting_value('telegram_backup_last_run', '') or ''
    last_dt = _parse_iso_datetime(last_run)
    schedule_mode = (_get_system_setting_value('telegram_backup_schedule_mode', 'interval') or 'interval').strip().lower()
    if schedule_mode not in ('interval', 'daily'):
        schedule_mode = 'interval'
    daily_time = (_get_system_setting_value('telegram_backup_daily_time', '00:00') or '00:00').strip()
    return {
        'enabled': enabled,
        'send_panel_backup': send_panel_backup,
        'schedule_mode': schedule_mode,
        'daily_time': daily_time,
        'interval_minutes': interval,
        'bot_token': _get_system_setting_value('telegram_backup_bot_token', '') or '',
        'chat_id': _get_system_setting_value('telegram_backup_chat_id', '') or '',
        'use_proxy': use_proxy,
        'route_source': route_source,
        'managed_account': managed_account,
        'proxy_mode': proxy_mode,
        'proxy_url': proxy_url,
        'proxy_host': proxy_host,
        'proxy_port': proxy_port,
        'proxy_username': proxy_username,
        'proxy_password': proxy_password,
        'last_run': last_run,
        'last_run_jalali': format_jalali(last_dt) if last_dt else ''
    }


def _inject_proxy_credentials(proxy_url: str, username: str, password: str) -> str:
    if not proxy_url:
        return proxy_url
    if not username and not password:
        return proxy_url

    try:
        parsed = urlparse(proxy_url)
    except Exception:
        return proxy_url

    if parsed.username or parsed.password:
        return proxy_url

    netloc = parsed.netloc or ''
    if '@' in netloc:
        return proxy_url

    user_part = quote(username or '', safe='')
    pass_part = quote(password or '', safe='')
    if pass_part:
        creds = f"{user_part}:{pass_part}"
    else:
        creds = user_part

    updated = parsed._replace(netloc=f"{creds}@{netloc}")
    return updated.geturl()


def _build_telegram_proxies(use_proxy: bool, proxy_mode: str, proxy_url: str, proxy_host: str, proxy_port: int,
                            proxy_username: str, proxy_password: str) -> dict | None:
    if not use_proxy:
        return None

    mode = (proxy_mode or 'url').strip().lower()
    if mode == 'hostport':
        if not proxy_host or not proxy_port:
            return None
        normalized = _normalize_proxy_url(f"{proxy_host}:{proxy_port}")
        # Always inject credentials in hostport mode
        normalized = _inject_proxy_credentials(normalized, proxy_username, proxy_password)
    else:
        normalized = _normalize_proxy_url(proxy_url)
        # Only inject credentials if URL doesn't already have them
        if normalized and '@' not in normalized:
            normalized = _inject_proxy_credentials(normalized, proxy_username, proxy_password)

    if not normalized:
        return None

    return {'http': normalized, 'https': normalized}


def _telegram_backup_route_proxies(settings: dict, *, wait_for_runtime=False):
    """Resolve the configured backup route without exposing its connection URI."""
    source = str(settings.get('route_source') or '').strip().lower()
    if source == 'panel_account':
        managed = settings.get('managed_account') or {}
        profile_id = _parse_int(managed.get('profile_id'), 0, min_value=0)
        profile = db.session.get(TelegramEgressProfile, profile_id) if profile_id else None
        if not profile or not profile.enabled:
            return None, 'The selected panel account route is missing or disabled.'
        if wait_for_runtime and profile.runtime_status == 'pending':
            deadline = time.monotonic() + 12
            while profile.runtime_status == 'pending' and time.monotonic() < deadline:
                time.sleep(0.25)
                db.session.expire_all()
                profile = db.session.get(TelegramEgressProfile, profile_id)
                if not profile:
                    break
        if not profile or profile.runtime_status != 'running':
            state = profile.runtime_status if profile else 'missing'
            detail = redact_connection_error(profile.last_error) if profile and profile.last_error else ''
            return None, detail or f'The selected panel account route is not ready ({state}).'
        proxy_url = f'socks5h://127.0.0.1:{int(profile.local_port)}'
        return {'http': proxy_url, 'https': proxy_url}, None
    if source == 'direct' or not bool(settings.get('use_proxy')):
        return None, None
    proxies = _build_telegram_proxies(
        True,
        settings.get('proxy_mode') or 'url',
        settings.get('proxy_url') or '',
        settings.get('proxy_host') or '',
        int(settings.get('proxy_port') or 0),
        settings.get('proxy_username') or '',
        settings.get('proxy_password') or '',
    )
    return proxies, None if proxies else 'Manual proxy settings are incomplete.'


def _check_proxy_reachable(proxies: dict | None, timeout_sec: float = 5) -> tuple[bool, str | None]:
    if not proxies:
        return True, None
    url = proxies.get('https') or proxies.get('http') or ''
    if not url:
        return True, None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            return True, None
        with socket.create_connection((host, port), timeout=timeout_sec):
            pass
        return True, None
    except OSError as exc:
        return False, f"Proxy unreachable ({host}:{port}): {exc}"
    except Exception as exc:
        return False, f"Proxy check failed: {exc}"


def _telegram_get_me(token: str, proxies: dict | None = None, timeout_sec: int = 10):
    url = f"https://api.telegram.org/bot{token}/getMe"
    return requests.get(url, proxies=proxies, timeout=timeout_sec)


TELEGRAM_UPLOAD_CONNECT_TIMEOUT_SECONDS = 30
TELEGRAM_UPLOAD_READ_TIMEOUT_SECONDS = 600
TELEGRAM_UPLOAD_RETRIES = 3


def _telegram_send_document(token: str, chat_id: str, file_path: str, caption: str | None, proxies: dict | None = None):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data = {'chat_id': chat_id}
    if caption:
        data['caption'] = caption
    timeout = (TELEGRAM_UPLOAD_CONNECT_TIMEOUT_SECONDS, TELEGRAM_UPLOAD_READ_TIMEOUT_SECONDS)
    last_exc = None
    for attempt in range(1, TELEGRAM_UPLOAD_RETRIES + 1):
        try:
            with open(file_path, 'rb') as handle:
                files = {'document': (os.path.basename(file_path), handle)}
                return requests.post(url, data=data, files=files, proxies=proxies, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt >= TELEGRAM_UPLOAD_RETRIES:
                raise
            time.sleep(min(2 * attempt, 5))
    if last_exc:
        raise last_exc
    raise RuntimeError('Telegram upload failed')


def _build_telegram_backup_caption(server: 'Server', backup_time: datetime) -> str:
    from app import format_jalali
    server_name = (getattr(server, 'name', '') or f"Server {getattr(server, 'id', '')}").strip()
    server_address = (getattr(server, 'host', '') or '').strip() or '-'
    backup_date = format_jalali(backup_time) or backup_time.isoformat()
    return '\n'.join([
        f"🛢 {server_name}",
        f"🖥️ {server_address}",
        f"📅 {backup_date}",
    ])


def _build_telegram_panel_backup_caption(backup_time: datetime) -> str:
    from app import APP_VERSION, format_jalali
    try:
        iran_tz = ZoneInfo('Asia/Tehran') if ZoneInfo is not None else timezone(timedelta(hours=3, minutes=30))
        if backup_time.tzinfo is None:
            backup_dt = backup_time.replace(tzinfo=timezone.utc)
        else:
            backup_dt = backup_time.astimezone(timezone.utc)
        iran_dt = backup_dt.astimezone(iran_tz)
        backup_date = jdatetime_class.fromgregorian(datetime=iran_dt.replace(tzinfo=None)).strftime('%Y/%m/%d %H:%M')
    except Exception:
        backup_date = format_jalali(backup_time) or backup_time.isoformat()
    return '\n'.join([
        f"Panel version: v{APP_VERSION}",
        f"Date time (Iran): {backup_date}",
    ])


def _content_disposition_filename(header_value: str | None) -> str | None:
    if not header_value:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", header_value)
    if match:
        try:
            return unquote(match.group(1))
        except Exception:
            return match.group(1)
    match = re.search(r'filename="?([^";]+)"?', header_value)
    if match:
        return match.group(1)
    return None


def _guess_backup_extension(content_type: str, filename_hint: str | None = None) -> str:
    if filename_hint:
        _, ext = os.path.splitext(filename_hint)
        if ext:
            return ext
    ct = (content_type or '').lower()
    if 'zip' in ct:
        return '.zip'
    if 'gzip' in ct:
        return '.gz'
    if 'sqlite' in ct or 'x-sqlite3' in ct:
        return '.db'
    if 'octet-stream' in ct:
        return '.db'
    return '.db'


def _try_base64_decode(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        return None


def _extract_backup_payload_from_json(data) -> tuple[bytes | None, str | None]:
    if isinstance(data, dict):
        for key in ('obj', 'data', 'result'):
            if key in data:
                return _extract_backup_payload_from_json(data.get(key))

        filename_hint = data.get('filename') or data.get('name')
        for key in ('file', 'content', 'backup', 'bytes'):
            val = data.get(key)
            if isinstance(val, str):
                decoded = _try_base64_decode(val.strip())
                if decoded:
                    return decoded, filename_hint
        return None, filename_hint

    if isinstance(data, str):
        decoded = _try_base64_decode(data.strip())
        return decoded, None

    return None, None


def _extract_backup_bytes_from_response(resp: requests.Response) -> tuple[bytes | None, str | None, str | None]:
    content_type = (resp.headers.get('Content-Type') or '').lower()
    filename_hint = _content_disposition_filename(resp.headers.get('Content-Disposition'))

    if 'application/json' in content_type or content_type.startswith('text/'):
        data, err = _safe_response_json(resp)
        if err:
            return None, None, err
        if isinstance(data, dict) and data.get('success') is False:
            msg = data.get('msg') or data.get('message') or 'Backup failed'
            return None, None, str(msg)
        payload, json_filename = _extract_backup_payload_from_json(data)
        if not payload:
            return None, None, 'Backup payload missing'
        ext = _guess_backup_extension(content_type, json_filename or filename_hint)
        return payload, ext, None

    if resp.status_code != 200:
        return None, None, f"HTTP {resp.status_code}"

    if not resp.content:
        return None, None, "Empty response (status 200)"

    return resp.content, _guess_backup_extension(content_type, filename_hint), None


def _is_sqlite_payload(payload: bytes | None) -> bool:
    if not payload or len(payload) < 16:
        return False
    return payload.startswith(b"SQLite format 3")


def _collect_backup_endpoints(panel_type: str) -> list[tuple[str, str]]:
    normalized = (panel_type or 'auto').strip().lower()
    candidates: list[tuple[str, str]] = []

    if normalized in ('sanaei', 'auto', ''):
        candidates.extend([
            ('POST', '/panel/api/backup'),
            ('GET', '/panel/api/backup'),
            ('GET', '/panel/api/server/getDb'),
            ('GET', '/server/getDb'),
        ])
    if normalized in ('alireza', 'alireza0', 'xui', 'x-ui', 'auto', ''):
        candidates.extend([
            ('POST', '/xui/API/backup'),
            ('GET', '/xui/API/backup'),
            ('POST', '/xui/api/backup'),
            ('GET', '/xui/api/backup'),
            ('GET', '/xui/server/getDb'),
            ('GET', '/api/server/getDb'),
        ])

    candidates.extend([
        ('POST', '/panel/api/backup'),
        ('GET', '/panel/api/backup'),
        ('GET', '/panel/api/server/getDb'),
        ('GET', '/server/getDb'),
        ('POST', '/xui/API/backup'),
        ('GET', '/xui/API/backup'),
        ('POST', '/xui/api/backup'),
        ('GET', '/xui/api/backup'),
        ('GET', '/xui/server/getDb'),
        ('GET', '/api/server/getDb'),
    ])

    seen = set()
    deduped = []
    for method, ep in candidates:
        key = (method, ep)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((method, ep))
    return deduped


def _fetch_xui_backup(session_obj: requests.Session, server: 'Server') -> tuple[bytes | None, str | None, str | None]:
    from app import build_panel_url
    endpoints = _collect_backup_endpoints(getattr(server, 'panel_type', 'auto'))
    errors = []
    for method, template in endpoints:
        full_url = build_panel_url(server.host, template, {})
        if not full_url:
            continue
        try:
            if method == 'POST':
                resp = session_obj.post(full_url, verify=False, timeout=15)
            else:
                resp = session_obj.get(full_url, verify=False, timeout=15)
        except Exception as exc:
            errors.append(f"{method} {template}: {exc}")
            continue

        payload, ext, err = _extract_backup_bytes_from_response(resp)
        if payload:
            if (template.endswith('/getDb') or (ext or '').lower() == '.db') and not _is_sqlite_payload(payload):
                errors.append(f"{method} {template}: Invalid DB payload")
                continue
            return payload, ext, None
        errors.append(f"{method} {template}: {err or resp.status_code}")

    return None, None, '; '.join(errors) or 'No backup endpoint succeeded'


def _run_telegram_backup(trigger: str = 'scheduled', progress_cb=None) -> dict:
    from app import BACKUP_DIR
    if not TELEGRAM_BACKUP_LOCK.acquire(blocking=False):
        return {'success': False, 'error': 'Backup already running'}

    tmp_dir = None
    try:
        if progress_cb:
            try:
                progress_cb({'stage': 'loading_settings', 'progress': {'total': 0, 'processed': 0}})
            except Exception:
                pass

        settings = _get_telegram_backup_settings()
        enabled = bool(settings.get('enabled'))
        send_panel_backup = bool(settings.get('send_panel_backup'))
        if trigger == 'scheduled' and not enabled:
            return {'success': True, 'skipped': True, 'message': 'Telegram backup disabled'}

        token = (settings.get('bot_token') or '').strip()
        chat_id = (settings.get('chat_id') or '').strip()
        if not token or not chat_id:
            return {'success': False, 'error': 'Telegram bot token and chat ID are required'}

        if progress_cb:
            try:
                progress_cb({'stage': 'building_proxy'})
            except Exception:
                pass

        proxies, route_error = _telegram_backup_route_proxies(
            settings, wait_for_runtime=True,
        )
        if route_error:
            return {'success': False, 'error': route_error}

        if proxies:
            if progress_cb:
                try:
                    progress_cb({'stage': 'checking_proxy'})
                except Exception:
                    pass
            proxy_ok, proxy_err = _check_proxy_reachable(proxies)
            if not proxy_ok:
                return {
                    'success': False,
                    'error': f"Proxy unreachable — could not connect before upload started. {proxy_err}. "
                             "Check Settings → Telegram Backup → Proxy."
                }

        now = datetime.utcnow()

        servers = Server.query.filter_by(enabled=True).all()
        if not servers and not send_panel_backup:
            return {'success': False, 'error': 'No enabled servers found'}

        if progress_cb:
            try:
                progress_cb({'stage': 'fetching_servers', 'progress': {'total': len(servers) + (1 if send_panel_backup else 0), 'processed': 0}})
            except Exception:
                pass

        tmp_dir = tempfile.mkdtemp(prefix='telegram_backup_', dir=_telegram_backup_tmp_dir())
        results = []

        total_items = len(servers) + (1 if send_panel_backup else 0)
        processed_items = 0

        for server in servers:
            if progress_cb:
                try:
                    progress_cb({'stage': f"xui_login:{server.name}", 'progress': {'total': total_items, 'processed': processed_items}})
                except Exception:
                    pass

            session_obj, error = get_xui_session(server)
            if error:
                results.append({'server_id': server.id, 'server_name': server.name, 'success': False, 'error': f"X-UI Connection Failed: {error}"})
                processed_items += 1
                if progress_cb:
                    try:
                        progress_cb({'stage': f"xui_failed:{server.name}", 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                    except Exception:
                        pass
                continue

            if progress_cb:
                try:
                    progress_cb({'stage': f"xui_download_backup:{server.name}", 'progress': {'total': total_items, 'processed': processed_items}})
                except Exception:
                    pass

            payload, ext, err = _fetch_xui_backup(session_obj, server)
            if err or not payload:
                results.append({'server_id': server.id, 'server_name': server.name, 'success': False, 'error': f"X-UI Backup Download Failed: {err or 'Empty response'}"})
                processed_items += 1
                if progress_cb:
                    try:
                        progress_cb({'stage': f"xui_failed:{server.name}", 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                    except Exception:
                        pass
                continue

            safe_server_name = secure_filename(server.name) or f"server_{server.id}"
            timestamp = now.strftime('%Y%m%d_%H%M%S')
            ext = ext or '.db'
            filename = f"{safe_server_name}_{timestamp}{ext}"
            file_path = os.path.join(tmp_dir, filename)
            with open(file_path, 'wb') as handle:
                handle.write(payload)

            caption = _build_telegram_backup_caption(server, now)
            if progress_cb:
                try:
                    progress_cb({'stage': f"telegram_upload:{server.name}", 'progress': {'total': total_items, 'processed': processed_items}})
                except Exception:
                    pass
            try:
                resp = _telegram_send_document(token, chat_id, file_path, caption, proxies=proxies)
            except Exception as exc:
                results.append({'server_id': server.id, 'server_name': server.name, 'success': False, 'error': f"Telegram Upload Failed (Network/Proxy): {str(exc)}"})
                processed_items += 1
                if progress_cb:
                    try:
                        progress_cb({'stage': f"telegram_failed:{server.name}", 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                    except Exception:
                        pass
                continue

            resp_json, resp_err = _safe_response_json(resp)
            if resp_err:
                results.append({'server_id': server.id, 'server_name': server.name, 'success': False, 'error': f"Telegram API Error: {resp_err}"})
                processed_items += 1
                if progress_cb:
                    try:
                        progress_cb({'stage': f"telegram_failed:{server.name}", 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                    except Exception:
                        pass
                continue

            server_ok = isinstance(resp_json, dict) and resp_json.get('ok')
            if server_ok:
                results.append({'server_id': server.id, 'server_name': server.name, 'success': True})
            else:
                msg = None
                if isinstance(resp_json, dict):
                    msg = resp_json.get('description') or resp_json.get('error')
                results.append({'server_id': server.id, 'server_name': server.name, 'success': False, 'error': f"Telegram API Refused: {msg or 'Unknown error'}"})

            processed_items += 1
            if progress_cb:
                try:
                    stage_name = f"server_done:{server.name}" if server_ok else f"telegram_failed:{server.name}"
                    progress_cb({'stage': stage_name, 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                except Exception:
                    pass

        if send_panel_backup:
            panel_label = 'Panel Backup'
            panel_file_path = None
            if progress_cb:
                try:
                    progress_cb({'stage': 'panel_backup_create', 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                except Exception:
                    pass
            try:
                panel_filename = _create_database_backup_file('telegram_panel')
                panel_file_path = os.path.join(BACKUP_DIR, panel_filename)
            except Exception as exc:
                results.append({'server_id': None, 'server_name': panel_label, 'kind': 'panel', 'success': False, 'error': f"Panel Backup Create Failed: {str(exc)}"})
                processed_items += 1
                if progress_cb:
                    try:
                        progress_cb({'stage': 'panel_backup_failed', 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                    except Exception:
                        pass
            if panel_file_path:
                if progress_cb:
                    try:
                        progress_cb({'stage': 'panel_backup_upload', 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                    except Exception:
                        pass
                try:
                    caption = _build_telegram_panel_backup_caption(now)
                    resp = _telegram_send_document(token, chat_id, panel_file_path, caption, proxies=proxies)
                    resp_json, resp_err = _safe_response_json(resp)
                    if resp_err:
                        results.append({'server_id': None, 'server_name': panel_label, 'kind': 'panel', 'success': False, 'error': f"Telegram API Error: {resp_err}"})
                    elif isinstance(resp_json, dict) and resp_json.get('ok'):
                        results.append({'server_id': None, 'server_name': panel_label, 'kind': 'panel', 'success': True})
                    else:
                        msg = None
                        if isinstance(resp_json, dict):
                            msg = resp_json.get('description') or resp_json.get('error')
                        results.append({'server_id': None, 'server_name': panel_label, 'kind': 'panel', 'success': False, 'error': f"Telegram API Refused: {msg or 'Unknown error'}"})
                except Exception as exc:
                    results.append({'server_id': None, 'server_name': panel_label, 'kind': 'panel', 'success': False, 'error': f"Telegram Upload Failed (Network/Proxy): {str(exc)}"})

                processed_items += 1
                if progress_cb:
                    try:
                        panel_ok = bool(results and results[-1].get('success'))
                        stage_name = 'panel_backup_done' if panel_ok else 'panel_backup_failed'
                        progress_cb({'stage': stage_name, 'progress': {'total': total_items, 'processed': processed_items}, 'results': list(results)})
                    except Exception:
                        pass

        success_count = sum(1 for r in results if r.get('success'))

        # Only record last_run timestamp when at least one file was actually delivered
        if success_count > 0:
            try:
                _set_system_setting_value('telegram_backup_last_run', now.isoformat())
                db.session.commit()
            except Exception:
                pass

        # specific top-level error generation
        main_error = None
        if success_count == 0 and results:
            # Collect unique error prefixes
            errs = sorted(list(set(r.get('error', 'Unknown') for r in results)))
            raw = ' '.join(errs).lower()
            # Translate cryptic proxy/network errors into a clear, actionable message
            if 'socks5 authentication failed' in raw or ('authentication' in raw and 'socks' in raw):
                main_error = ('SOCKS5 proxy authentication failed — check the proxy username/password '
                              '(or disable proxy auth if the proxy does not require it). '
                              'Telegram backups have been failing since this started.')
            elif 'failed to establish a new connection' in raw or 'max retries exceeded' in raw or 'connection refused' in raw:
                main_error = ('Could not reach Telegram through the proxy — the proxy may be down or the '
                              'host/port is wrong. Check Settings → Telegram Backup → Proxy.')
            elif 'timed out' in raw or 'timeout' in raw:
                main_error = 'Connection to Telegram/proxy timed out. The proxy or network may be slow or blocked.'
            elif len(errs) == 1:
                main_error = errs[0]
            else:
                main_error = f"All backups failed. Errors: {'; '.join(errs[:2])}..."

        return {
            'success': success_count > 0,
            'error': main_error,
            'results': results,
            'success_count': success_count,
            'total': len(results)
        }
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass
        try:
            TELEGRAM_BACKUP_LOCK.release()
        except Exception:
            pass
