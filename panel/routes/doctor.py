"""Eve Doctor: read-only diagnostics for a signed-in operator.

Phase 8 exposes the TLS certificate report plus a compact overall health
summary. The expensive checks (database, disk, certificates) are the same ones
the background watchdog runs, so the endpoint reflects exactly what is being
monitored. Nothing here mutates state and no certificate key material is ever
read or returned.
"""
import os
import shutil

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from panel.extensions import db, limiter
from panel.models import HealthLog, Server, SystemSetting
from panel.routes.common import permission_required
from panel.services import certificates

bp = Blueprint('doctor', __name__)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _configured_cert_path():
    """Prefer the operator-saved path, then autodetect the host certificate."""
    from app import _autodetect_ssl_paths  # deferred: app-level helper
    try:
        saved = db.session.get(SystemSetting, 'ssl_cert_path')
        path = (saved.value if saved and saved.value else '').strip() if saved else ''
        if path:
            return path
    except Exception:
        pass
    try:
        cert_path, _key_path = _autodetect_ssl_paths()
        return cert_path or ''
    except Exception:
        return ''


def _tls_endpoints():
    """https endpoints the operator cares about: public panel URL + servers."""
    from app import _public_base_url  # deferred: app-level helper
    endpoints = []
    try:
        public = (_public_base_url() or '').strip()
        if public.lower().startswith('https://'):
            endpoints.append(public)
    except Exception:
        pass
    try:
        for server in Server.query.filter_by(enabled=True).all():
            host = (getattr(server, 'host', '') or '').strip()
            if host.lower().startswith('https://'):
                endpoints.append(host)
    except Exception:
        pass
    return endpoints


@bp.route('/api/doctor/tls', methods=['GET'])
@limiter.limit('20 per minute')
@permission_required('settings.read')
def doctor_tls():
    """Certificate report for the panel certificate and its https endpoints."""
    refresh = (request.args.get('refresh') or '').lower() in ('1', 'true', 'yes')
    report = certificates.get_tls_report(
        refresh=refresh,
        cert_path=_configured_cert_path() or None,
        endpoints=_tls_endpoints(),
    )
    return jsonify({'success': True, **report})


@bp.route('/api/doctor', methods=['GET'])
@limiter.limit('30 per minute')
@permission_required('settings.read')
def doctor_summary():
    """Compact operator-facing health summary (Eve Doctor)."""
    from app import APP_VERSION  # deferred: app-level constant

    checks = {}

    try:
        db.session.execute(text('SELECT 1'))
        db.session.rollback()
        checks['database'] = {'state': 'ok'}
    except Exception as exc:
        db.session.rollback()
        checks['database'] = {'state': 'error', 'error': str(exc)[:200]}

    try:
        usage = shutil.disk_usage(_repo_root())
        used_pct = round(usage.used / usage.total * 100, 1)
        state = 'critical' if used_pct > 95 else ('warning' if used_pct > 90 else 'ok')
        checks['disk'] = {
            'state': state,
            'used_pct': used_pct,
            'free_gb': round(usage.free / (1024 ** 3), 2),
        }
    except Exception as exc:
        checks['disk'] = {'state': 'unknown', 'error': str(exc)[:200]}

    secret_key = (os.environ.get('SERVER_PASSWORD_KEY') or '').strip()
    checks['secret_key'] = {
        'state': 'ok' if secret_key else 'warning',
        'detail': ('SERVER_PASSWORD_KEY is configured' if secret_key
                   else 'SERVER_PASSWORD_KEY is not set; stored secrets are not protected'),
    }

    try:
        tls = certificates.get_tls_report(
            refresh=False,
            cert_path=_configured_cert_path() or None,
            endpoints=_tls_endpoints(),
        )
    except Exception as exc:
        tls = {'healthy': False, 'summary': {}, 'checked_at': None, 'error': str(exc)[:200]}
    checks['tls'] = {
        'state': 'ok' if tls.get('healthy') else 'warning',
        'summary': tls.get('summary') or {},
        'checked_at': tls.get('checked_at'),
    }

    try:
        from panel.core.panel_limits import panel_metrics
        checks['panel_limits'] = {'state': 'ok', **panel_metrics()}
    except Exception as exc:
        checks['panel_limits'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.core import redis_client as _redis_cache
        checks['snapshot_cache'] = {
            'state': 'ok',
            'redis_configured': bool(_redis_cache.REDIS_URL),
            **_redis_cache.snapshot_metrics(),
        }
    except Exception as exc:
        checks['snapshot_cache'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        recent = [row.to_dict() for row in HealthLog.query.filter(
            HealthLog.level.in_(('critical', 'error')),
        ).order_by(HealthLog.id.desc()).limit(5).all()]
    except Exception:
        recent = []

    order = {'ok': 0, 'unknown': 1, 'warning': 2, 'error': 3, 'critical': 4}
    overall = 'ok'
    for check in checks.values():
        if order.get(check.get('state'), 3) > order.get(overall, 0):
            overall = check.get('state')

    return jsonify({
        'success': True,
        'version': APP_VERSION,
        'state': overall,
        'checks': checks,
        'recent_errors': recent,
        'certificate_thresholds': {
            'warn_days': certificates.warn_days(),
            'critical_days': certificates.critical_days(),
            'check_interval_seconds': certificates.check_interval_seconds(),
        },
    })
