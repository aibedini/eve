"""System health, current-user, and self-update routes (extracted from app.py)."""
import json
import os
import subprocess
import time
from datetime import datetime

import requests
from flask import Blueprint, jsonify, request, session
from sqlalchemy import text

from panel.extensions import db
from panel.models import Admin
from panel.routes.common import login_required, superadmin_required

bp = Blueprint('system', __name__)


@bp.route('/healthz', methods=['GET'])
def healthz():
    """Lightweight health endpoint for reverse-proxy / uptime checks."""
    from app import APP_START_TS, APP_VERSION  # deferred: app-level helper, avoids circular import
    db_ok = True
    try:
        db.session.execute(text('SELECT 1'))
        db.session.rollback()
    except Exception:
        db_ok = False
    status = 'ok' if db_ok else 'degraded'
    code = 200 if db_ok else 503
    return jsonify({
        'success': db_ok,
        'status': status,
        'db': 'ok' if db_ok else 'unreachable',
        'version': APP_VERSION,
        'uptime_seconds': int(max(0, time.time() - APP_START_TS)),
        'timestamp_utc': datetime.utcnow().isoformat() + 'Z',
    }), code

@bp.route('/api/me', methods=['GET'])
@login_required
def get_current_user_info():
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401
    return jsonify({
        'success': True,
        'user': user.to_dict()
    })

def _system_update_status_payload(log_offset=0):
    """Read durable updater state without trusting paths from the request."""
    from app import (
        _ANSI_ESCAPE_RE, APP_VERSION, SYSTEM_UPDATE_STATE_DIR,
        SYSTEM_UPDATE_UNIT_PATH,
    )  # deferred: app-level helper, avoids circular import
    status_path = os.path.join(SYSTEM_UPDATE_STATE_DIR, 'status.json')
    log_path = os.path.join(SYSTEM_UPDATE_STATE_DIR, 'update.log')
    status = {'state': 'idle', 'message': '', 'started_at': None,
              'finished_at': None, 'version': APP_VERSION}
    try:
        with open(status_path, 'r', encoding='utf-8') as handle:
            saved = json.load(handle)
        if isinstance(saved, dict):
            status.update({key: saved.get(key) for key in status if key in saved})
    except (OSError, ValueError, TypeError):
        pass

    # A reboot or killed updater must not leave the UI permanently locked in
    # "running". Reading systemd state is unprivileged and uses a fixed unit.
    # The unit is Type=oneshot, so it reports ActiveState=activating for its
    # entire run; `is-active --quiet` would misread that as dead. Query the
    # ActiveState value instead and treat every live state as alive.
    if status.get('state') == 'running':
        try:
            probe = subprocess.run(
                ['/bin/systemctl', 'show', '--property=ActiveState', '--value',
                 'eve-web-update.service'],
                capture_output=True, timeout=3, check=False, text=True,
            )
            if probe.returncode == 0:
                active_state = (probe.stdout or '').strip().lower()
                if active_state in ('active', 'activating', 'reloading', 'refreshing'):
                    active = True
                elif active_state in ('inactive', 'failed', 'deactivating'):
                    active = False
                else:
                    active = None
            else:
                active = None
        except (OSError, subprocess.SubprocessError):
            active = None
        if active is False:
            status['state'] = 'interrupted'
            status['message'] = 'The update process stopped before reporting a result'

    try:
        offset = max(0, int(log_offset or 0))
    except (TypeError, ValueError):
        offset = 0
    log_text = ''
    next_offset = 0
    has_more = False
    try:
        size = os.path.getsize(log_path)
        if offset > size:  # A new run truncated the previous log.
            offset = 0
        with open(log_path, 'rb') as handle:
            handle.seek(offset)
            chunk = handle.read(128 * 1024)
            next_offset = handle.tell()
        has_more = next_offset < size
        log_text = chunk.decode('utf-8', errors='replace')
        log_text = _ANSI_ESCAPE_RE.sub('', log_text)
    except OSError:
        next_offset = 0

    return {
        'success': True,
        'available': os.path.isfile(SYSTEM_UPDATE_UNIT_PATH),
        'current_version': APP_VERSION,
        'status': status,
        'log': log_text,
        'next_offset': next_offset,
        'has_more': has_more,
    }

@bp.route('/api/system-update/status', methods=['GET'])
@superadmin_required
def system_update_status():
    payload = _system_update_status_payload(request.args.get('offset', 0))
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/system-update/start', methods=['POST'])
@superadmin_required
def system_update_start():
    from app import SYSTEM_UPDATE_START_COMMAND, SYSTEM_UPDATE_UNIT_PATH, app  # deferred: app-level helper, avoids circular import
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'UPDATE':
        return jsonify({'success': False, 'error': 'Update confirmation is required'}), 400
    if not os.path.isfile(SYSTEM_UPDATE_UNIT_PATH):
        return jsonify({
            'success': False,
            'error': 'Browser update service is not installed; run one SSH update first.',
        }), 503
    current = _system_update_status_payload(0).get('status') or {}
    if current.get('state') == 'running':
        return jsonify({'success': False, 'error': 'An update is already running'}), 409
    try:
        result = subprocess.run(
            list(SYSTEM_UPDATE_START_COMMAND), capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        app.logger.exception('Could not launch browser update')
        return jsonify({'success': False, 'error': str(exc)}), 500
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'systemd rejected the update').strip()
        return jsonify({'success': False, 'error': detail[:500]}), 500
    app.logger.warning(
        'Browser panel update started by admin_id=%s from %s',
        session.get('admin_id'), request.remote_addr)
    return jsonify({'success': True, 'state': 'starting'}), 202

@bp.route('/api/check-update', methods=['GET'])
@login_required
def check_update():
    from app import APP_VERSION, GITHUB_REPO, UPDATE_CACHE  # deferred: app-level helper, avoids circular import
    def _normalize_version_str(v: str) -> str:
        if not v:
            return ''
        v = str(v).strip()
        # GitHub tags are often like "v1.7.0"
        if v[:1] in ('v', 'V'):
            v = v[1:]
        return v.strip()

    def _parse_semver(v: str):
        """Best-effort semver parsing.

        Returns (major, minor, patch, is_prerelease) or None.
        Accepts: 1, 1.7, 1.7.0, 1.7.0-rc1, 1.7.0+meta
        """
        v = _normalize_version_str(v)
        if not v:
            return None
        # Split build metadata
        core = v.split('+', 1)[0]
        # Split prerelease
        core_part, prerelease_part = (core.split('-', 1) + [''])[:2]
        is_prerelease = bool(prerelease_part)
        parts = core_part.split('.')
        try:
            major = int(parts[0]) if len(parts) >= 1 and parts[0] != '' else 0
            minor = int(parts[1]) if len(parts) >= 2 and parts[1] != '' else 0
            patch = int(parts[2]) if len(parts) >= 3 and parts[2] != '' else 0
        except Exception:
            return None
        return (major, minor, patch, is_prerelease)

    def _is_update_available(current: str, latest: str) -> bool:
        cur_norm = _normalize_version_str(current)
        lat_norm = _normalize_version_str(latest)
        if not cur_norm or not lat_norm:
            return False
        cur = _parse_semver(cur_norm)
        lat = _parse_semver(lat_norm)
        if cur and lat:
            cur_key = (cur[0], cur[1], cur[2])
            lat_key = (lat[0], lat[1], lat[2])
            if lat_key != cur_key:
                return lat_key > cur_key
            # Same base version: stable beats prerelease.
            # (So 1.7.0 should NOT report update vs 1.7.0-rc1)
            return (cur[3] is True) and (lat[3] is False)
        # Fallback: normalized string compare
        return lat_norm != cur_norm

    # Check cache first (but don't reuse cache across app version changes)
    current_time = time.time()
    if UPDATE_CACHE['data'] and (current_time - UPDATE_CACHE['last_check'] < UPDATE_CACHE['ttl']):
        try:
            if str(UPDATE_CACHE['data'].get('current_version')) == str(APP_VERSION):
                return jsonify(UPDATE_CACHE['data'])
        except Exception:
            pass

    try:
        resp = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            latest_version_raw = data.get('tag_name', '')
            latest_version = _normalize_version_str(latest_version_raw)
            
            result = {
                'success': True,
                'current_version': APP_VERSION,
                'latest_version': latest_version,
                'update_available': _is_update_available(APP_VERSION, latest_version),
                'release_url': data.get('html_url', '')
            }
            
            # Update cache
            UPDATE_CACHE['last_check'] = current_time
            UPDATE_CACHE['data'] = result
            
            return jsonify(result)
        return jsonify({'success': False, 'error': 'GitHub API error'})
    except Exception as e:
        # If request fails (timeout/network), return cached data if available (even if expired) to avoid error
        if UPDATE_CACHE['data']:
            return jsonify(UPDATE_CACHE['data'])
        return jsonify({'success': False, 'error': str(e)})
