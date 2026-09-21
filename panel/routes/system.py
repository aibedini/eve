"""System health, current-user, and self-update routes (extracted from app.py)."""
import json
import os
import re
import subprocess
import time
from datetime import datetime

import requests
from flask import Blueprint, jsonify, request, session
from sqlalchemy import text

from panel.extensions import db
from panel.models import Admin
from panel.routes.common import login_required, step_up_required, superadmin_required
from panel.security import client_ip

bp = Blueprint('system', __name__)

_UPDATE_REF_RE = re.compile(
    r'^(?:main|v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?|[0-9a-fA-F]{7,40})$'
)


def _normalize_update_ref(value):
    """Return a safe updater ref, accepting ``v.2.5.86`` as a convenience."""
    ref = str(value or '').strip()
    if ref.lower().startswith('v.'):
        ref = 'v' + ref[2:]
    if ref in ('', 'latest'):
        ref = 'main'
    return ref if _UPDATE_REF_RE.fullmatch(ref) else None


def _local_version_history():
    """Read version/ref pairs from the local Git checkout, newest first."""
    from app import APP_VERSION  # deferred: app-level helper, avoids circular import

    app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    versions = []
    seen = set()
    try:
        commits = subprocess.run(
            ['git', '-C', app_dir, 'log', '--format=%H', '-G',
             r'^APP_VERSION\s*=\s*["\']', '--', 'app.py'],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=5, check=False,
        )
        if commits.returncode != 0:
            return versions
        for commit in (commits.stdout or '').splitlines()[:100]:
            commit = commit.strip()
            if not commit:
                continue
            source = subprocess.run(
                ['git', '-C', app_dir, 'show', f'{commit}:app.py'],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=5, check=False,
            )
            match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)',
                              source.stdout or '', re.MULTILINE)
            if not match:
                continue
            version = match.group(1)
            if version in seen:
                continue
            seen.add(version)
            versions.append({
                'version': version,
                'ref': commit,
                'current': version == APP_VERSION,
            })
    except (OSError, subprocess.SubprocessError):
        return versions

    if APP_VERSION not in seen:
        versions.insert(0, {'version': APP_VERSION, 'ref': 'main', 'current': True})
    return versions


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
    if status.get('state') == 'running' and os.path.isfile(SYSTEM_UPDATE_UNIT_PATH):
        # Only interpret systemd state when the configured unit is installed;
        # otherwise a stored 'running' status would be rewritten to 'interrupted'
        # on any host that simply has no update unit (or on a foreign runner).
        try:
            unit_name = os.path.basename(SYSTEM_UPDATE_UNIT_PATH)
            probe = subprocess.run(
                ['/bin/systemctl', 'show', '--property=ActiveState', '--value', unit_name],
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


@bp.route('/api/system/memory', methods=['GET'])
@superadmin_required
def system_memory():
    """Memory attribution for Settings -> Overview.

    Read-only and bounded: host totals, per-role PSS/RSS/USS for Eve's processes, what the
    in-process snapshot holds, how many processes hold a copy of it, the compressed snapshot
    in Redis, the caches outside it, and the bounded trend. ``?trend_minutes=`` selects the
    trend window and is clamped to what the ring can answer (5 .. 1440 minutes, i.e. a day);
    the payload echoes the window it actually used. No credentials, commands, environment
    values or customer data - only counts, sizes, pids and roles
    (see panel/core/memory_report.py).
    """
    from panel.core import memory_report  # deferred: keeps the route import light
    minutes = memory_report.clamp_trend_minutes(request.args.get('trend_minutes'))
    payload = memory_report.report(trend_minutes=minutes)
    response = jsonify({'success': True, **payload})
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/system/memory/analyze', methods=['POST'])
@superadmin_required
@step_up_required('system.update')
def system_memory_analyze():
    """Explicit, admin-only deep Python memory sample (never runs continuously).

    Returns the largest current allocations as file/line/size only. It is behind the same
    step-up guard as the updater because it briefly traces allocations, and the sample is
    bounded: a timeout marker is returned rather than letting the request hang.
    """
    from flask import current_app
    from panel.core import memory_report  # deferred: keeps the route import light
    data = request.get_json(silent=True) or {}
    limit = max(1, min(200, int(data.get('limit') or 40)))
    if not current_app.config.get('TESTING') and not os.path.isdir('/proc'):
        return jsonify({'success': False,
                        'error': 'deep memory analysis needs Linux /proc'}), 503
    return jsonify({'success': True, **memory_report.analyze_python_memory(limit=limit)})


@bp.route('/api/system/memory/alloc-probe', methods=['POST'])
@superadmin_required
@step_up_required('system.update')
def system_memory_alloc_probe_request():
    """Ask the background process for one allocator diagnostic (explicit, never automatic).

    The request travels through Redis because the process under investigation is the
    background worker, not the one serving this call. One probe at a time: a second request
    while one is pending is refused instead of queued. The result is bounded by a TTL and
    carries counters and byte sizes only (see panel/core/alloc_probe.py).
    """
    from panel.core import alloc_probe  # deferred: keeps the route import light
    outcome = alloc_probe.request_probe()
    if not outcome.get('ok'):
        return jsonify({'success': False, 'error': outcome.get('reason') or 'refused',
                        'pending': outcome.get('pending')}), 409
    return jsonify({'success': True, **outcome})


@bp.route('/api/system/memory/alloc-probe', methods=['GET'])
@superadmin_required
def system_memory_alloc_probe_result():
    """The pending request or the last bounded result of the allocator diagnostic."""
    from panel.core import alloc_probe  # deferred: keeps the route import light
    return jsonify({'success': True, **alloc_probe.read_result()})


@bp.route('/api/system-update/start', methods=['POST'])
@superadmin_required
@step_up_required('system.update')
def system_update_start():
    from app import (  # deferred: app-level helper, avoids circular import
        SYSTEM_UPDATE_START_COMMAND, SYSTEM_UPDATE_STATE_DIR,
        SYSTEM_UPDATE_UNIT_PATH, app,
    )
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'UPDATE':
        return jsonify({'success': False, 'error': 'Update confirmation is required'}), 400
    target_ref = _normalize_update_ref(data.get('ref') or data.get('version'))
    if target_ref is None:
        return jsonify({
            'success': False,
            'error': 'Invalid version/ref. Use main, a semantic version, or a commit SHA.',
        }), 400
    if not os.path.isfile(SYSTEM_UPDATE_UNIT_PATH):
        return jsonify({
            'success': False,
            'error': 'Browser update service is not installed; run one SSH update first.',
        }), 503
    current = _system_update_status_payload(0).get('status') or {}
    if current.get('state') == 'running':
        return jsonify({'success': False, 'error': 'An update is already running'}), 409

    # systemd intentionally receives only a fixed command. Pass the selected
    # ref through the state directory instead of interpolating it into a shell
    # command or unit override.
    ref_path = os.path.join(SYSTEM_UPDATE_STATE_DIR, 'requested-ref')
    temp_ref_path = f'{ref_path}.tmp-{os.getpid()}'
    try:
        os.makedirs(SYSTEM_UPDATE_STATE_DIR, exist_ok=True)
        with open(temp_ref_path, 'w', encoding='utf-8') as handle:
            handle.write(target_ref + '\n')
        os.replace(temp_ref_path, ref_path)
    except OSError as exc:
        try:
            os.unlink(temp_ref_path)
        except OSError:
            pass
        app.logger.exception('Could not persist requested update ref')
        return jsonify({'success': False, 'error': str(exc)}), 500

    try:
        result = subprocess.run(
            list(SYSTEM_UPDATE_START_COMMAND), capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            os.unlink(ref_path)
        except OSError:
            pass
        app.logger.exception('Could not launch browser update')
        return jsonify({'success': False, 'error': str(exc)}), 500
    if result.returncode != 0:
        try:
            os.unlink(ref_path)
        except OSError:
            pass
        detail = (result.stderr or result.stdout or 'systemd rejected the update').strip()
        return jsonify({'success': False, 'error': detail[:500]}), 500
    app.logger.warning(
        'Browser panel update started by admin_id=%s from %s',
        session.get('admin_id'), client_ip())
    return jsonify({'success': True, 'state': 'starting', 'target_ref': target_ref}), 202


@bp.route('/api/system-update/versions', methods=['GET'])
@superadmin_required
def system_update_versions():
    from app import APP_VERSION  # deferred: app-level helper, avoids circular import
    return jsonify({
        'success': True,
        'current_version': APP_VERSION,
        'versions': _local_version_history(),
    })

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
