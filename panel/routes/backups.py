"""Backup management and backup-settings API routes (extracted from app.py)."""
import glob
import json
import os
import shutil
import subprocess
from datetime import datetime

from flask import (
    Blueprint, Response, jsonify, make_response, request, send_file,
    session, stream_with_context, url_for,
)
from werkzeug.utils import secure_filename

from panel.extensions import db
from panel.models import SystemSetting
from panel.routes.common import login_required, superadmin_required
from panel.services.backup import (
    _create_database_backup_file, _create_full_migration_zip, _db_uri,
    _get_system_setting_value, _is_postgres_db, _is_sqlite_db, _parse_int,
    _pg_env_from_uri, _pg_reset_public_schema, _pg_restore_backup,
    _pg_restore_jobs, _restore_full_migration_zip,
    _set_system_setting_value,
)


bp = Blueprint('backups', __name__)


BACKUP_UPLOAD_MAX_SIZE = 2048 * 1024 * 1024  # 2 GB — full migration bundles (DB + all uploaded files) can exceed 512 MB


@bp.route('/api/backups', methods=['GET'])
@login_required
def list_backups():
    from app import (  # deferred: app-level helper, avoids circular import
        BACKUP_DIR, app,
    )
    backups = []
    if os.path.exists(BACKUP_DIR):
        patterns = ('*.db', '*.dump', '*.sql', '*.zip')
        files = []
        for pat in patterns:
            files.extend(glob.glob(os.path.join(BACKUP_DIR, pat)))
        files.sort(key=os.path.getmtime, reverse=True)
        for f in files:
            name = os.path.basename(f)
            ext = os.path.splitext(name)[1].lower()
            size = os.path.getsize(f)
            date = datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M:%S')

            if ext == '.zip':
                restore_supported = True  # full migration bundle (DB + files)
            elif _is_sqlite_db():
                restore_supported = ext == '.db'
            else:
                restore_supported = ext in ('.dump', '.sql')

            if ext == '.zip' or 'migration' in name:
                b_type = 'Migration (DB+files)'
            elif name.startswith('upload_'):
                b_type = 'Uploaded'
            elif name.startswith('auto_'):
                b_type = 'Automatic'
            elif name.startswith('pre_restore_'):
                b_type = 'Safety'
            else:
                b_type = 'System'

            backups.append({
                'name': name, 'size': size, 'date': date,
                'type': b_type, 'restore_supported': restore_supported
            })

    return jsonify({
        'success': True,
        'backups': backups,
        'is_postgres': _is_postgres_db(),
        'restore_supported': True,  # always true; per-file flag controls actual button
    })


@bp.route('/api/backups', methods=['POST'])
@login_required
def create_backup():
    from app import app  # deferred: app-level helper, avoids circular import
    try:
        filename = _create_database_backup_file('backup')
        return jsonify({'success': True, 'message': 'Backup created', 'filename': filename})
    except Exception as e:
        app.logger.error(f'create_backup error: {e}')
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/backups/migration', methods=['POST'])
@superadmin_required
def create_migration_backup():
    """Create a COMPLETE migration bundle (DB + all uploaded files) and return its filename.
    Use this to move everything to another server."""
    from app import (  # deferred: app-level helper, avoids circular import
        BACKUP_DIR, app,
    )
    try:
        filename = _create_full_migration_zip('migration')
        size = os.path.getsize(os.path.join(BACKUP_DIR, filename))
        return jsonify({'success': True, 'filename': filename, 'size': size,
                        'message': 'Full migration bundle created (database + uploaded files)'})
    except Exception as e:
        app.logger.error(f'create_migration_backup error: {e}', exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/backups/diag', methods=['GET'])
@superadmin_required
def backup_diag():
    """Diagnostic: show backup directory path and files on disk."""
    from app import (  # deferred: app-level helper, avoids circular import
        BACKUP_DIR, app,
    )
    pg_dump_bin = shutil.which('pg_dump')
    files_on_disk = []
    try:
        if os.path.isdir(BACKUP_DIR):
            for f in sorted(os.listdir(BACKUP_DIR)):
                fp = os.path.join(BACKUP_DIR, f)
                if os.path.isfile(fp):
                    files_on_disk.append({'name': f, 'size': os.path.getsize(fp)})
    except Exception as e:
        files_on_disk = [{'error': str(e)}]
    return jsonify({
        'backup_dir': BACKUP_DIR,
        'dir_exists': os.path.isdir(BACKUP_DIR),
        'dir_writable': os.access(BACKUP_DIR, os.W_OK),
        'instance_path': app.instance_path,
        'pg_dump': pg_dump_bin or 'NOT FOUND',
        'db_type': 'postgresql' if _is_postgres_db() else 'sqlite',
        'files_on_disk': files_on_disk,
    })


@bp.route('/api/backups/upload', methods=['POST'])
@login_required
def upload_backup():
    from app import (  # deferred: app-level helper, avoids circular import
        BACKUP_DIR, app,
    )
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file part'})
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'})
    
    file.seek(0, os.SEEK_END)
    file_length = file.tell()
    file.seek(0)
    if file_length > BACKUP_UPLOAD_MAX_SIZE:
        mb = BACKUP_UPLOAD_MAX_SIZE // (1024 * 1024)
        return jsonify({'success': False, 'error': f'File too large (max {mb} MB)'}), 413

    allowed_exts = {'.db', '.dump', '.sql', '.zip'}  # .zip = full migration bundle
    _, ext = os.path.splitext(file.filename or '')
    ext = (ext or '').lower()

    if file and ext in allowed_exts:
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe_name = secure_filename(file.filename)
            filename = f'upload_{timestamp}_{safe_name}'
            file.save(os.path.join(BACKUP_DIR, filename))
            return jsonify({'success': True, 'message': 'Backup uploaded successfully'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return jsonify({'success': False, 'error': 'Invalid file type. Allowed: .db, .dump, .sql, .zip'})


@bp.route('/api/settings/backup', methods=['GET'])
@login_required
def get_backup_settings():
    from app import (  # deferred: app-level helper, avoids circular import
        _parse_bool, app,
    )
    freq = db.session.get(SystemSetting, 'backup_frequency')
    return jsonify({
        'success': True,
        'frequency': freq.value if freq else 'disabled',
        'retention_enabled': _parse_bool(_get_system_setting_value('backup_retention_enabled', 'false')),
        'retention_days': _parse_int(_get_system_setting_value('backup_retention_days', '14'), 14, min_value=1, max_value=3650),
    })


@bp.route('/api/settings/backup', methods=['POST'])
@login_required
def save_backup_settings():
    from app import app  # deferred: app-level helper, avoids circular import
    data = request.json
    freq_val = data.get('frequency', 'disabled')

    setting = db.session.get(SystemSetting, 'backup_frequency')
    if not setting:
        setting = SystemSetting(key='backup_frequency', value=freq_val)
        db.session.add(setting)
    else:
        setting.value = freq_val

    if 'retention_enabled' in data:
        _set_system_setting_value('backup_retention_enabled', 'true' if data.get('retention_enabled') else 'false')
    if 'retention_days' in data:
        rdays = _parse_int(data.get('retention_days'), 14, min_value=1, max_value=3650)
        _set_system_setting_value('backup_retention_days', str(rdays))

    db.session.commit()
    return jsonify({'success': True, 'message': 'Settings saved'})


@bp.route('/api/backups/cleanup', methods=['POST'])
@login_required
def cleanup_backups_now():
    """Apply the retention rule right now (Clear now button)."""
    from app import (  # deferred: app-level helper, avoids circular import
        _cleanup_old_backups, app,
    )
    data = request.get_json(silent=True) or {}
    # Allow an explicit days override; otherwise use the saved setting
    days = data.get('days')
    if days is None:
        days = _parse_int(_get_system_setting_value('backup_retention_days', '14'), 14, min_value=1, max_value=3650)
    else:
        days = _parse_int(days, 14, min_value=1, max_value=3650)
    result = _cleanup_old_backups(days)
    return jsonify({'success': True, 'days': days, **result})


@bp.route('/api/backups/<filename>/download', methods=['GET'])
@login_required
def download_backup(filename):
    from app import (  # deferred: app-level helper, avoids circular import
        BACKUP_DIR, app,
    )
    filename = secure_filename(filename)
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    file_size = os.path.getsize(path)
    # Files >50 MB: let nginx stream directly via X-Accel-Redirect so the
    # gunicorn worker is freed immediately and never times out mid-download.
    # Requires the /protected-backups/ internal location in nginx.conf.
    # Falls back to send_file when accessed without nginx (dev / direct).
    behind_nginx = bool(request.headers.get('X-Forwarded-For') or
                        request.headers.get('X-Real-IP'))
    if behind_nginx and file_size > 50 * 1024 * 1024:
        resp = make_response()
        resp.headers['X-Accel-Redirect'] = f'/protected-backups/{filename}'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        resp.headers['Content-Type'] = 'application/octet-stream'
        resp.headers['Content-Length'] = str(file_size)
        return resp
    return send_file(path, as_attachment=True)


@bp.route('/api/backups/<filename>/restore', methods=['POST'])
@login_required
def restore_backup(filename):
    from app import (  # deferred: app-level helper, avoids circular import
        BACKUP_DIR, app,
    )
    filename = secure_filename(filename)
    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path):
        return jsonify({'success': False, 'error': 'Backup not found'}), 404

    ext = os.path.splitext(filename)[1].lower()

    try:
        if _is_sqlite_db():
            if ext != '.db':
                return jsonify({'success': False, 'error': 'SQLite databases require a .db backup file'}), 400
            db_path = os.path.join(app.instance_path, 'servers.db')
            # Safety backup before overwrite
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            safety = os.path.join(BACKUP_DIR, f'pre_restore_{timestamp}.db')
            if os.path.exists(db_path):
                shutil.copy2(db_path, safety)
            shutil.copy2(backup_path, db_path)

        elif _is_postgres_db():
            if ext not in ('.dump', '.sql'):
                return jsonify({
                    'success': False,
                    'error': 'PostgreSQL restore requires a .dump or .sql backup file'
                }), 400
            # Safety backup of current DB before restore
            try:
                _create_database_backup_file('pre_restore')
            except Exception as be:
                app.logger.warning(f"Could not create pre-restore safety backup: {be}")
            _pg_restore_backup(backup_path)

        else:
            return jsonify({'success': False, 'error': 'Unsupported database backend'}), 400

        session.clear()
        return jsonify({
            'success': True,
            'message': 'Database restored successfully. Please log in again.',
            'redirect': url_for('auth.login')
        })

    except Exception as e:
        app.logger.error(f"Restore failed for {filename}: {e}")
        return jsonify({'success': False, 'error': str(e)})


@bp.route('/api/backups/<filename>/restore/stream')
@login_required
def restore_backup_stream(filename):
    """SSE endpoint — streams live restore progress to the browser."""
    from app import (  # deferred: app-level helper, avoids circular import
        BACKUP_DIR, app,
    )
    import threading
    filename = secure_filename(filename)
    backup_path = os.path.join(BACKUP_DIR, filename)

    def _sse(type_, message, **extra):
        data = {'type': type_, 'message': message, **extra}
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _heartbeat():
        # SSE comment — keeps the connection alive through nginx/proxies
        return ": heartbeat\n\n"

    def generate():
        # ── Pre-flight checks ──────────────────────────────────────────
        yield _sse('log', '🔍 Checking backup file…')

        if not os.path.exists(backup_path):
            yield _sse('error', f'Backup not found: {filename}')
            return

        ext = os.path.splitext(filename)[1].lower()
        size_mb = round(os.path.getsize(backup_path) / 1024 / 1024, 2)
        yield _sse('log', f'File: {filename}  ({size_mb} MB)')

        db_type = 'PostgreSQL' if _is_postgres_db() else ('SQLite' if _is_sqlite_db() else 'Unknown')
        yield _sse('log', f'Database: {db_type}')

        # ── Full migration bundle (.zip): DB + uploaded files ─────────────
        if ext == '.zip':
            try:
                yield _sse('log', '📦 Full migration bundle detected — restoring database AND uploaded files…')
                _msgs = []
                _restore_full_migration_zip(backup_path, log=lambda m: _msgs.append(m))
                for _m in _msgs:
                    yield _sse('log', _m)
                yield _sse('done', '✓ Migration restore complete (DB + files) — logging you out.',
                           redirect=url_for('auth.logout'))
            except Exception as exc:
                app.logger.error(f"migration restore error: {exc}", exc_info=True)
                yield _sse('error', f'Migration restore failed: {exc}')
            return

        try:
            # ── SQLite ────────────────────────────────────────────────────
            if _is_sqlite_db():
                if ext != '.db':
                    yield _sse('error', f'SQLite requires a .db backup; got {ext!r}')
                    return
                db_path = os.path.join(app.instance_path, 'servers.db')
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                safety = os.path.join(BACKUP_DIR, f'pre_restore_{timestamp}.db')
                if os.path.exists(db_path):
                    shutil.copy2(db_path, safety)
                    yield _sse('log', f'✓ Safety backup: {os.path.basename(safety)}')
                yield _sse('log', 'Replacing database file…')
                shutil.copy2(backup_path, db_path)
                yield _sse('done', '✓ Restore complete — logging you out.', redirect=url_for('auth.logout'))

            # ── PostgreSQL ────────────────────────────────────────────────
            elif _is_postgres_db():
                if ext not in ('.dump', '.sql'):
                    yield _sse('error', f'PostgreSQL requires .dump or .sql; got {ext!r}')
                    return

                # Check tools first
                if ext == '.dump':
                    tool_bin = shutil.which('pg_restore')
                    if not tool_bin:
                        yield _sse('error', 'pg_restore not found.\n  Fix: sudo apt install postgresql-client')
                        return
                    if not shutil.which('psql'):
                        yield _sse('error', 'psql not found.\n  Fix: sudo apt install postgresql-client')
                        return
                    jobs = _pg_restore_jobs()
                    tool = 'pg_restore'
                    cmd = [tool_bin, '--no-owner', '--no-acl',
                           f'--jobs={jobs}', '--dbname', _db_uri(), backup_path]
                else:
                    tool_bin = shutil.which('psql')
                    if not tool_bin:
                        yield _sse('error', 'psql not found.\n  Fix: sudo apt install postgresql-client')
                        return
                    tool = 'psql'
                    cmd = [tool_bin, '--dbname', _db_uri(), '--file', backup_path, '--echo-errors']

                yield _sse('log', f'✓ {tool} found at: {tool_bin}')

                # Safety backup
                try:
                    yield _sse('log', 'Creating safety backup before overwriting…')
                    yield _heartbeat()
                    safety_name = _create_database_backup_file('pre_restore')
                    yield _sse('log', f'✓ Safety backup: {os.path.basename(safety_name)}')
                except Exception as be:
                    yield _sse('log', f'⚠ Safety backup failed (continuing): {be}')

                yield _sse('log', f'Running {tool}…')
                yield _heartbeat()

                uri = _db_uri()
                env = _pg_env_from_uri(uri)

                yield _sse('log', 'Resetting PostgreSQL public schema with CASCADEâ€¦')
                yield _heartbeat()
                _pg_reset_public_schema(uri, env)
                yield _sse('log', 'âœ“ PostgreSQL schema reset complete')

                # --jobs=N means pg_restore spawns parallel workers — no stdout
                # lines will flow during restore. We run it in the background and
                # send heartbeats every 2 s so the SSE connection stays alive.
                import threading as _threading
                import time as _time

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )

                # Collect stderr in a background thread so we can show errors
                stderr_lines = []
                def _read_stderr():
                    for _l in proc.stderr:
                        stderr_lines.append(_l.rstrip())
                _t = _threading.Thread(target=_read_stderr, daemon=True)
                _t.start()

                # Stream heartbeats + elapsed time while waiting
                start = _time.monotonic()
                if ext == '.dump':
                    yield _sse('log', f'Running {tool} with --jobs={jobs} (parallel)…')
                else:
                    yield _sse('log', f'Running {tool}…')
                while proc.poll() is None:
                    _time.sleep(2)
                    elapsed = int(_time.monotonic() - start)
                    yield _sse('progress', f'Restoring… {elapsed}s elapsed')
                    yield _heartbeat()

                _t.join(timeout=5)
                proc.wait(timeout=10)

                elapsed = int(_time.monotonic() - start)
                # Show last few stderr lines (errors / warnings)
                visible = [l for l in stderr_lines if l and not l.startswith('pg_restore:')]
                errors  = [l for l in stderr_lines if 'error' in l.lower()]
                for line in (errors or visible)[-10:]:
                    yield _sse('log', line)

                if proc.returncode != 0:
                    yield _sse('error', f'{tool} exited with code {proc.returncode} after {elapsed}s')
                else:
                    suffix = f' (--jobs={jobs})' if ext == '.dump' else ''
                    yield _sse('log', f'✓ {tool} finished in {elapsed}s{suffix}')
                    yield _sse('done', '✓ Restore complete — logging you out.',
                               redirect=url_for('auth.logout'))
            else:
                uri = _db_uri()
                yield _sse('error', f'Unsupported database type (URI: {uri[:30]}…)')

        except Exception as exc:
            app.logger.error(f"restore_backup_stream error: {exc}", exc_info=True)
            yield _sse('error', f'Unexpected error: {exc}')

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'X-Content-Type-Options': 'nosniff',
        },
    )


@bp.route('/api/backups/<filename>', methods=['DELETE'])
@login_required
def delete_backup(filename):
    from app import (  # deferred: app-level helper, avoids circular import
        BACKUP_DIR, app,
    )
    filename = secure_filename(filename)
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    try:
        os.remove(path)
        return jsonify({'success': True, 'message': 'Backup deleted'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
