"""Backup configs, uploads, app-files, and system-config API routes (extracted from app.py)."""
import os
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, session
from werkzeug.utils import secure_filename

from panel.extensions import db
from panel.models import BackupConfig, Server, SystemConfig
from panel.routes.common import superadmin_required, user_management_required
from panel.services.backup import _parse_int


bp = Blueprint('files', __name__)


@bp.route('/api/backup-configs', methods=['GET'])
@user_management_required
def get_backup_configs():
    from app import app  # deferred: app-level helper, avoids circular import
    items = BackupConfig.query.order_by(BackupConfig.sort_order, BackupConfig.id).all()
    servers = Server.query.order_by(Server.name).all()
    return jsonify({
        'success': True,
        'items': [i.to_dict() for i in items],
        'servers': [{'id': s.id, 'name': s.name} for s in servers],
        'default_description': BackupConfig.DEFAULT_DESCRIPTION,
    })


@bp.route('/api/backup-configs', methods=['POST'])
@user_management_required
def create_backup_config():
    from app import app  # deferred: app-level helper, avoids circular import
    data = request.get_json(force=True) or {}
    title = (data.get('title') or '').strip()
    config_url = (data.get('config_url') or '').strip()
    if not title or not config_url:
        return jsonify({'success': False, 'error': 'Title and config URL are required'}), 400
    item = BackupConfig(
        server_id=data.get('server_id') or None,
        title=title,
        config_url=config_url,
        description=(data.get('description') or '').strip(),
        is_enabled=bool(data.get('is_enabled', True)),
        sort_order=int(data.get('sort_order') or 0),
    )
    try:
        db.session.add(item)
        db.session.commit()
        return jsonify({'success': True, 'item': item.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/backup-configs/<int:item_id>', methods=['PUT'])
@user_management_required
def update_backup_config(item_id):
    from app import app  # deferred: app-level helper, avoids circular import
    item = db.session.get(BackupConfig, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    data = request.get_json(force=True) or {}
    if 'title' in data:
        item.title = (data['title'] or '').strip()
    if 'config_url' in data:
        item.config_url = (data['config_url'] or '').strip()
    if 'description' in data:
        item.description = (data['description'] or '').strip()
    if 'is_enabled' in data:
        item.is_enabled = bool(data['is_enabled'])
    if 'server_id' in data:
        item.server_id = data['server_id'] or None
    if 'sort_order' in data:
        item.sort_order = int(data.get('sort_order') or 0)
    item.updated_at = datetime.utcnow()
    try:
        db.session.commit()
        return jsonify({'success': True, 'item': item.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/backup-configs/<int:item_id>', methods=['DELETE'])
@user_management_required
def delete_backup_config(item_id):
    from app import app  # deferred: app-level helper, avoids circular import
    item = db.session.get(BackupConfig, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/upload', methods=['POST'])
@user_management_required
def upload_file():
    from app import (  # deferred: app-level helper, avoids circular import
        MAX_FILE_SIZE, app,
    )
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'}), 400

    if file:
        file.seek(0, os.SEEK_END)
        file_length = file.tell()
        file.seek(0)
        if file_length > MAX_FILE_SIZE:
            return jsonify({'success': False, 'error': 'File too large'}), 413

        filename = secure_filename(f"{uuid.uuid4().hex[:8]}_{file.filename}")
        upload_folder = os.path.join(app.static_folder, 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        file.save(os.path.join(upload_folder, filename))
        return jsonify({'success': True, 'url': f'/static/uploads/{filename}'})


_APP_FILE_MAX_BYTES  = 500 * 1024 * 1024   # 500 MB (covers large installers + videos)


# Extension → category mapping (whitelist)
_ALLOWED_APP_EXTS = {
    # Installers
    '.apk':  'android', '.aab':  'android',
    '.exe':  'windows', '.msi':  'windows',
    '.dmg':  'macos',   '.pkg':  'macos',
    '.deb':  'linux',   '.rpm':  'linux',   '.appimage': 'linux',
    # Archives / cross-platform
    '.zip':  'archive', '.tar':  'archive', '.gz': 'archive',
    # Videos
    '.mp4':  'video',   '.webm': 'video',   '.mkv': 'video', '.mov': 'video',
    # Images (icons / screenshots)
    '.png':  'image',   '.jpg':  'image',   '.jpeg': 'image', '.webp': 'image',
    '.svg':  'image',
}


def _safe_app_file_path(filename: str) -> str | None:
    """Return absolute path if filename stays inside app-files dir, else None."""
    from app import _app_files_dir  # deferred: app-level helper, avoids circular import
    base = os.path.realpath(_app_files_dir())
    target = os.path.realpath(os.path.join(base, filename))
    return target if target.startswith(base + os.sep) else None


@bp.route('/api/app-files/health', methods=['GET'])
@superadmin_required
def app_files_health():
    """Diagnostic endpoint — returns directory status and write-test result."""
    from app import (  # deferred: app-level helper, avoids circular import
        _APP_FILES_DIR_NAME, _app_files_dir, app,
    )
    static_folder = app.static_folder or '(not set)'
    d = os.path.join(static_folder, _APP_FILES_DIR_NAME)
    info = {
        'static_folder': static_folder,
        'target_dir': d,
        'exists': os.path.isdir(d),
        'writable': os.access(d, os.W_OK) if os.path.isdir(d) else False,
        'file_count': 0,
        'write_test': None,
        'error': None,
    }
    try:
        real_d = _app_files_dir()
        info['target_dir'] = real_d
        info['exists'] = True
        info['writable'] = True
        info['file_count'] = sum(1 for f in os.listdir(real_d) if os.path.isfile(os.path.join(real_d, f)))
        # Write test
        test_path = os.path.join(real_d, f'.write_test_{uuid.uuid4().hex[:6]}')
        with open(test_path, 'w') as t:
            t.write('ok')
        os.remove(test_path)
        info['write_test'] = 'passed'
        info['success'] = True
    except Exception as e:
        info['error'] = str(e)
        info['success'] = False
        app.logger.error(f'app_files_health error: {e}')
    return jsonify(info)


@bp.route('/api/app-files/setup', methods=['POST'])
@superadmin_required
def app_files_setup():
    """Auto-create upload directory, fix permissions, run write test. Returns step-by-step diagnostics."""
    from app import (  # deferred: app-level helper, avoids circular import
        _APP_FILES_DIR_NAME, app,
    )
    static_folder = app.static_folder or ''
    d = os.path.join(static_folder, _APP_FILES_DIR_NAME)
    steps = []

    def _step(name, ok, detail='', fix=''):
        steps.append({'name': name, 'ok': ok, 'detail': detail, 'fix': fix})

    if not os.path.isdir(static_folder):
        _step('Static folder exists', False,
              f"'{static_folder}' does not exist",
              f"mkdir -p '{static_folder}'")
        return jsonify({'success': False, 'steps': steps, 'error': 'Static folder missing'})
    _step('Static folder exists', True, static_folder)

    try:
        os.makedirs(d, exist_ok=True)
        _step('Create upload directory', True, d)
    except OSError as e:
        fix = f"mkdir -p '{d}' && chown $(whoami) '{d}' && chmod 755 '{d}'"
        _step('Create upload directory', False, str(e), fix)
        return jsonify({'success': False, 'steps': steps, 'error': str(e)})

    if not os.access(d, os.W_OK):
        try:
            os.chmod(d, 0o755)
        except OSError:
            pass
        if os.access(d, os.W_OK):
            _step('Set permissions (755)', True, 'Applied successfully')
        else:
            fix = f"sudo chown $(whoami) '{d}' && sudo chmod 755 '{d}'"
            _step('Set permissions (755)', False, 'Directory still not writable — manual fix required', fix)
            return jsonify({'success': False, 'steps': steps, 'error': fix})
    else:
        _step('Directory writable', True)

    test_path = os.path.join(d, f'.write_test_{uuid.uuid4().hex[:6]}')
    try:
        with open(test_path, 'w') as _t:
            _t.write('ok')
        os.remove(test_path)
        _step('Write test', True, 'Temporary file written and removed')
    except Exception as e:
        fix = f"sudo chown -R $(whoami) '{d}'"
        _step('Write test', False, str(e), fix)
        return jsonify({'success': False, 'steps': steps, 'error': str(e)})

    app.logger.info(f"app_files_setup by {session.get('admin_username', '?')}: {d}")
    return jsonify({'success': True, 'steps': steps, 'directory': d})


@bp.route('/api/app-files', methods=['GET'])
@superadmin_required
def list_app_files():
    from app import (  # deferred: app-level helper, avoids circular import
        _APP_FILES_DIR_NAME, _app_files_dir, app,
    )
    try:
        base = _app_files_dir()
    except RuntimeError as e:
        app.logger.error(f'list_app_files dir error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500
    files = []
    try:
        for fname in sorted(os.listdir(base)):
            fpath = os.path.join(base, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            stat = os.stat(fpath)
            files.append({
                'name': fname,
                'size': stat.st_size,
                'modified': int(stat.st_mtime),
                'url': f'/static/{_APP_FILES_DIR_NAME}/{fname}',
                'category': _ALLOWED_APP_EXTS.get(ext, 'other'),
                'ext': ext.lstrip('.'),
            })
    except Exception as e:
        app.logger.error(f'list_app_files error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({'success': True, 'files': files})


@bp.route('/api/app-files/upload', methods=['POST'])
@bp.route('/api/app-files/save', methods=['POST'])
@superadmin_required
def upload_app_file():
    from app import (  # deferred: app-level helper, avoids circular import
        _APP_FILES_DIR_NAME, _app_files_dir, app,
    )
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    original = secure_filename(f.filename)
    if not original:
        return jsonify({'success': False, 'error': 'Invalid filename'}), 400

    ext = os.path.splitext(original)[1].lower()
    if ext not in _ALLOWED_APP_EXTS:
        return jsonify({'success': False, 'error': f'File type not allowed: {ext or "(none)"}'}), 415

    # Use Content-Length header first (fast, no extra read); fall back to seek/tell
    size = request.content_length or 0
    if size == 0:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(0)
        except Exception:
            size = 0

    if size > _APP_FILE_MAX_BYTES:
        return jsonify({'success': False, 'error': f'File too large ({size // (1024*1024)} MB). Max 500 MB.'}), 413

    # Verify upload directory is accessible — return a clear 500 (not a mystery error)
    try:
        base_dir = _app_files_dir()
    except RuntimeError as e:
        app.logger.error(f'upload_app_file dir error: {e}')
        return jsonify({'success': False, 'error': str(e)}), 500

    uid = uuid.uuid4().hex[:10]
    safe_name = f"{uid}_{original}"
    dest = os.path.join(base_dir, safe_name)

    try:
        f.save(dest)
    except Exception as e:
        app.logger.error(f'upload_app_file save error ({dest}): {e}')
        return jsonify({'success': False, 'error': f'Save failed: {e}'}), 500

    try:
        saved_size = os.stat(dest).st_size
        modified   = int(os.stat(dest).st_mtime)
    except OSError:
        saved_size, modified = size, int(__import__('time').time())

    category = _ALLOWED_APP_EXTS.get(ext, 'other')
    app.logger.info(
        f"App file uploaded by {session.get('admin_username','?')}: "
        f"{safe_name} ({saved_size} bytes, category={category})"
    )
    return jsonify({
        'success': True,
        'file': {
            'name': safe_name,
            'size': saved_size,
            'modified': modified,
            'url': f'/static/{_APP_FILES_DIR_NAME}/{safe_name}',
            'category': category,
            'ext': ext.lstrip('.'),
        }
    })


@bp.route('/api/app-files/<path:filename>', methods=['DELETE'])
@superadmin_required
def delete_app_file(filename):
    # Prevent path traversal
    from app import app  # deferred: app-level helper, avoids circular import
    safe_name = secure_filename(filename)
    fpath = _safe_app_file_path(safe_name)
    if not fpath or not os.path.isfile(fpath):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    try:
        os.remove(fpath)
        app.logger.info(f"App file deleted by {session.get('admin_username')}: {safe_name}")
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f'delete_app_file error: {e}')
        return jsonify({'success': False, 'error': 'Delete failed'}), 500


@bp.route('/api/system-config', methods=['POST'])
@superadmin_required
def update_system_config():
    from app import (  # deferred: app-level helper, avoids circular import
        SMS_ANNOUNCEMENT_DAILY_LIMIT_KEY, SMS_AUTOMATION_ENABLED_KEY,
        SMS_COOLDOWN_HOURS_ENDED_KEY, SMS_COOLDOWN_HOURS_EXPIRED_KEY,
        SMS_COOLDOWN_HOURS_LOW_VOLUME_KEY, SMS_COOLDOWN_HOURS_NEAR_EXPIRY_KEY,
        SMS_DAILY_LIMIT_KEY,
        SMS_DEPLETION_COOLDOWN_DAYS_KEY, SMS_DEPLETION_EXPIRY_DAYS_KEY,
        SMS_DEPLETION_VOLUME_GB_KEY, SMS_ENDED_MAX_AGE_DAYS_KEY,
        SMS_EXPIRED_MAX_AGE_DAYS_KEY, SMS_GMWEB_API_KEY_KEY,
        SMS_GMWEB_BASE_URL_KEY, SMS_GMWEB_TIMEOUT_KEY,
        SMS_MIN_INTERVAL_SECONDS_KEY, SMS_QUIET_ENABLED_KEY,
        SMS_QUIET_END_KEY, SMS_QUIET_START_KEY,
        SMS_ROYALTY_COOLDOWN_DAYS_KEY, SMS_ROYALTY_DAYS_KEY,
        SMS_SEND_PACE_SECONDS_KEY, SMS_SKIP_UNLIMITED_KEY,
        SMS_TRIGGER_CREATED_KEY, SMS_TRIGGER_DEPLETION_KEY,
        SMS_TRIGGER_ENDED_KEY, SMS_TRIGGER_EXPIRED_KEY,
        SMS_TRIGGER_LOW_VOLUME_KEY, SMS_TRIGGER_NEAR_EXPIRY_KEY,
        SMS_TRIGGER_RENEW_KEY, SMS_TRIGGER_ROYALTY_KEY,
        TG_DEPLETION_COOLDOWN_DAYS_KEY, TG_DEPLETION_ENABLED_KEY,
        TG_DEPLETION_EXPIRY_DAYS_KEY, TG_DEPLETION_RECOMMEND_KEY,
        TG_DEPLETION_VOLUME_GB_KEY, TG_TPL_LOW_VOLUME_KEY,
        TG_TPL_NEAR_EXPIRY_KEY, TG_TPL_RENEW_KEY, TG_TRIGGER_RENEW_KEY,
        WHATSAPP_BACKOFF_SECONDS_KEY, WHATSAPP_BOT_TPL_CREATED_KEY,
        WHATSAPP_BOT_TPL_ENDED_KEY, WHATSAPP_BOT_TPL_INFO_KEY,
        WHATSAPP_BOT_TPL_RENEW_KEY, WHATSAPP_CIRCUIT_BREAKER_KEY,
        WHATSAPP_DAILY_LIMIT_KEY, WHATSAPP_DEPLETION_COOLDOWN_DAYS_KEY,
        WHATSAPP_DEPLETION_ENABLED_KEY, WHATSAPP_DEPLETION_EXPIRY_DAYS_KEY,
        WHATSAPP_DEPLETION_VOLUME_GB_KEY, WHATSAPP_DEPLOYMENT_REGION_KEY,
        WHATSAPP_ENABLED_KEY, WHATSAPP_GATEWAY_API_KEY,
        WHATSAPP_GATEWAY_TIMEOUT_KEY, WHATSAPP_GATEWAY_URL_KEY,
        WHATSAPP_MIN_INTERVAL_SECONDS_KEY, WHATSAPP_PACE_ENABLED_KEY,
        WHATSAPP_PACE_JITTER_KEY, WHATSAPP_PACE_MIN_GAP_KEY,
        WHATSAPP_PRE_EXPIRY_HOURS_KEY, WHATSAPP_PROVIDER_KEY,
        WHATSAPP_RETRY_COUNT_KEY, WHATSAPP_SESSION_KEY,
        WHATSAPP_TEMPLATE_PRE_EXPIRY_KEY, WHATSAPP_TEMPLATE_RENEW_KEY,
        WHATSAPP_TEMPLATE_WELCOME_KEY, WHATSAPP_TRIGGER_PRE_EXPIRY_KEY,
        WHATSAPP_TRIGGER_RENEW_KEY, WHATSAPP_TRIGGER_WELCOME_KEY,
        WHATSAPP_WARMUP_ENABLED_KEY, WHATSAPP_WARMUP_RAMP_DAYS_KEY,
        WHATSAPP_WARMUP_START_DATE_KEY, WHATSAPP_WARMUP_START_PER_DAY_KEY,
        _get_system_config_text, _normalize_whatsapp_gateway_url,
        _normalize_whatsapp_provider, _normalize_whatsapp_region,
        _normalize_whatsapp_session, _parse_bool, app, sanitize_html,
    )
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
        
    try:
        normalized_region = None
        if WHATSAPP_DEPLOYMENT_REGION_KEY in data:
            normalized_region = _normalize_whatsapp_region(data.get(WHATSAPP_DEPLOYMENT_REGION_KEY))

        for key, value in data.items():
            config = db.session.get(SystemConfig, key)

            if key == WHATSAPP_DEPLOYMENT_REGION_KEY:
                sanitized_value = _normalize_whatsapp_region(value)
            elif key == WHATSAPP_PROVIDER_KEY:
                sanitized_value = _normalize_whatsapp_provider(value)
            elif key in {
                WHATSAPP_ENABLED_KEY,
                WHATSAPP_TRIGGER_RENEW_KEY,
                WHATSAPP_TRIGGER_WELCOME_KEY,
                WHATSAPP_TRIGGER_PRE_EXPIRY_KEY,
                WHATSAPP_CIRCUIT_BREAKER_KEY,
            }:
                sanitized_value = 'true' if _parse_bool(value) else 'false'
            elif key == WHATSAPP_MIN_INTERVAL_SECONDS_KEY:
                sanitized_value = str(_parse_int(value, 45, min_value=45, max_value=3600))
            elif key == WHATSAPP_DAILY_LIMIT_KEY:
                sanitized_value = str(_parse_int(value, 100, min_value=1, max_value=50000))
            elif key == WHATSAPP_PRE_EXPIRY_HOURS_KEY:
                sanitized_value = str(_parse_int(value, 24, min_value=1, max_value=720))
            elif key == WHATSAPP_RETRY_COUNT_KEY:
                sanitized_value = str(_parse_int(value, 3, min_value=0, max_value=10))
            elif key == WHATSAPP_BACKOFF_SECONDS_KEY:
                sanitized_value = str(_parse_int(value, 30, min_value=5, max_value=3600))
            elif key == WHATSAPP_GATEWAY_URL_KEY:
                sanitized_value = _normalize_whatsapp_gateway_url(value)
            elif key == WHATSAPP_GATEWAY_API_KEY:
                sanitized_value = sanitize_html(str(value))[:512]
            elif key == WHATSAPP_SESSION_KEY:
                sanitized_value = _normalize_whatsapp_session(value)
            elif key == WHATSAPP_GATEWAY_TIMEOUT_KEY:
                sanitized_value = str(_parse_int(value, 10, min_value=3, max_value=60))
            elif key in {WHATSAPP_TEMPLATE_RENEW_KEY, WHATSAPP_TEMPLATE_WELCOME_KEY, WHATSAPP_TEMPLATE_PRE_EXPIRY_KEY}:
                sanitized_value = sanitize_html(str(value))[:2000]
            elif key in {
                WHATSAPP_WARMUP_ENABLED_KEY, WHATSAPP_PACE_ENABLED_KEY, WHATSAPP_DEPLETION_ENABLED_KEY,
            }:
                sanitized_value = 'true' if _parse_bool(value) else 'false'
            elif key == WHATSAPP_WARMUP_START_DATE_KEY:
                # Accept YYYY-MM-DD only; anything else stored empty.
                _raw = str(value or '').strip()
                try:
                    sanitized_value = datetime.strptime(_raw, '%Y-%m-%d').strftime('%Y-%m-%d') if _raw else ''
                except ValueError:
                    sanitized_value = ''
            elif key == WHATSAPP_WARMUP_START_PER_DAY_KEY:
                sanitized_value = str(_parse_int(value, 20, min_value=1, max_value=50000))
            elif key == WHATSAPP_WARMUP_RAMP_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 14, min_value=1, max_value=120))
            elif key == WHATSAPP_PACE_MIN_GAP_KEY:
                sanitized_value = str(_parse_int(value, 8, min_value=0, max_value=600))
            elif key == WHATSAPP_PACE_JITTER_KEY:
                sanitized_value = str(_parse_int(value, 5, min_value=0, max_value=600))
            elif key == WHATSAPP_DEPLETION_EXPIRY_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 3, min_value=0, max_value=60))
            elif key == WHATSAPP_DEPLETION_VOLUME_GB_KEY:
                try:
                    _v = max(0.0, min(1000.0, float(value)))
                except (TypeError, ValueError):
                    _v = 2.0
                sanitized_value = str(_v)
            elif key == WHATSAPP_DEPLETION_COOLDOWN_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 7, min_value=1, max_value=120))
            elif key in {
                WHATSAPP_BOT_TPL_CREATED_KEY, WHATSAPP_BOT_TPL_RENEW_KEY,
                WHATSAPP_BOT_TPL_ENDED_KEY, WHATSAPP_BOT_TPL_INFO_KEY,
            }:
                sanitized_value = sanitize_html(str(value))[:2000]
            # ── SMS Automation (GMweb) ──
            elif key in {TG_DEPLETION_ENABLED_KEY, TG_DEPLETION_RECOMMEND_KEY, TG_TRIGGER_RENEW_KEY}:
                sanitized_value = 'true' if _parse_bool(value) else 'false'
            elif key == TG_DEPLETION_EXPIRY_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 3, min_value=0, max_value=60))
            elif key == TG_DEPLETION_VOLUME_GB_KEY:
                try:
                    _v = max(0.0, min(1000.0, float(value)))
                except (TypeError, ValueError):
                    _v = 2.0
                sanitized_value = str(_v)
            elif key == TG_DEPLETION_COOLDOWN_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 7, min_value=1, max_value=120))
            elif key in {TG_TPL_RENEW_KEY, TG_TPL_NEAR_EXPIRY_KEY, TG_TPL_LOW_VOLUME_KEY}:
                sanitized_value = sanitize_html(str(value))[:2000]
            elif key in {
                SMS_AUTOMATION_ENABLED_KEY, SMS_TRIGGER_CREATED_KEY,
                SMS_TRIGGER_RENEW_KEY, SMS_TRIGGER_DEPLETION_KEY,
                SMS_TRIGGER_NEAR_EXPIRY_KEY, SMS_TRIGGER_LOW_VOLUME_KEY,
                SMS_TRIGGER_EXPIRED_KEY, SMS_TRIGGER_ENDED_KEY,
            }:
                sanitized_value = 'true' if _parse_bool(value) else 'false'
            elif key == SMS_GMWEB_BASE_URL_KEY:
                sanitized_value = str(value or '').strip().rstrip('/')[:512]
            elif key == SMS_GMWEB_API_KEY_KEY:
                sanitized_value = str(value or '').strip()[:512]
            elif key == SMS_GMWEB_TIMEOUT_KEY:
                sanitized_value = str(_parse_int(value, 15, min_value=3, max_value=90))
            elif key == SMS_DEPLETION_EXPIRY_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 3, min_value=0, max_value=60))
            elif key == SMS_DEPLETION_VOLUME_GB_KEY:
                try:
                    _v = max(0.0, min(1000.0, float(value)))
                except (TypeError, ValueError):
                    _v = 2.0
                sanitized_value = str(_v)
            elif key == SMS_DEPLETION_COOLDOWN_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 7, min_value=1, max_value=120))
            elif key in {
                SMS_COOLDOWN_HOURS_NEAR_EXPIRY_KEY, SMS_COOLDOWN_HOURS_LOW_VOLUME_KEY,
                SMS_COOLDOWN_HOURS_EXPIRED_KEY, SMS_COOLDOWN_HOURS_ENDED_KEY,
            }:
                _dflt = 48 if key == SMS_COOLDOWN_HOURS_EXPIRED_KEY else 24
                sanitized_value = str(_parse_int(value, _dflt, min_value=1, max_value=8760))
            elif key == SMS_EXPIRED_MAX_AGE_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 30, min_value=0, max_value=3650))
            elif key == SMS_ENDED_MAX_AGE_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 0, min_value=0, max_value=3650))
            elif key == SMS_MIN_INTERVAL_SECONDS_KEY:
                sanitized_value = str(_parse_int(value, 30, min_value=0, max_value=3600))
            elif key == SMS_DAILY_LIMIT_KEY:
                sanitized_value = str(_parse_int(value, 200, min_value=1, max_value=100000))
            elif key == SMS_ANNOUNCEMENT_DAILY_LIMIT_KEY:
                sanitized_value = str(_parse_int(value, 500, min_value=1, max_value=100000))
            elif key == SMS_SEND_PACE_SECONDS_KEY:
                try:
                    _p = max(0.0, min(60.0, float(value)))
                except (TypeError, ValueError):
                    _p = 3.0
                sanitized_value = str(_p)
            elif key == SMS_QUIET_ENABLED_KEY:
                sanitized_value = 'true' if _parse_bool(value) else 'false'
            elif key in {SMS_QUIET_START_KEY, SMS_QUIET_END_KEY}:
                sanitized_value = str(_parse_int(value, 0, min_value=0, max_value=23))
            elif key == SMS_SKIP_UNLIMITED_KEY:
                sanitized_value = 'true' if _parse_bool(value) else 'false'
            elif key == SMS_TRIGGER_ROYALTY_KEY:
                sanitized_value = 'true' if _parse_bool(value) else 'false'
            elif key == SMS_ROYALTY_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 3, min_value=1, max_value=365))
            elif key == SMS_ROYALTY_COOLDOWN_DAYS_KEY:
                sanitized_value = str(_parse_int(value, 30, min_value=1, max_value=365))
            else:
                sanitized_value = sanitize_html(str(value))

            if config:
                config.value = sanitized_value
            else:
                config = SystemConfig(key=key, value=sanitized_value)
                db.session.add(config)

        effective_region = normalized_region
        if effective_region is None:
            effective_region = _normalize_whatsapp_region(_get_system_config_text(WHATSAPP_DEPLOYMENT_REGION_KEY, 'outside'))

        warning = None
        if effective_region == 'iran':
            enabled_conf = db.session.get(SystemConfig, WHATSAPP_ENABLED_KEY)
            if enabled_conf:
                enabled_conf.value = 'false'
            else:
                db.session.add(SystemConfig(key=WHATSAPP_ENABLED_KEY, value='false'))
            warning = 'WhatsApp automation is not available when the panel is deployed in Iran.'
        
        db.session.commit()
        return jsonify({'success': True, 'warning': warning})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
