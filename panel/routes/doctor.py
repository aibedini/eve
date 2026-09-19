"""Eve Doctor: read-only diagnostics for a signed-in operator.

Phase 8 exposes the TLS certificate report plus a compact overall health
summary. The expensive checks (database, disk, certificates) are the same ones
the background watchdog runs, so the endpoint reflects exactly what is being
monitored. Nothing here mutates state and no certificate key material is ever
read or returned.
"""
import os
import shutil
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from panel.extensions import db, limiter
from panel.models import HealthLog, Server, SystemSetting
from panel.routes.common import permission_required
from panel.services import certificates

bp = Blueprint('doctor', __name__)


def _block_stamp_ages(snapshot, *, now):
    """Per-server age of the snapshot's own two freshness layers.

    Configuration and telemetry age independently (a renew updates configuration
    immediately while traffic arrives on the next poll), so an operator asking "why does
    this card look old?" needs both numbers. They are read from the block the dashboard
    is actually rendering -- the scheduler's memory cannot answer a question about data
    another process is serving.
    """
    ages = {}
    for inbound in (snapshot or {}).get('inbounds') or []:
        try:
            sid = str(int(inbound.get('server_id', -1)))
        except (TypeError, ValueError):
            continue
        if sid in ages:
            continue
        row = {}
        for key, field in (('config_age_seconds', 'config_updated_at'),
                           ('telemetry_age_seconds', 'telemetry_updated_at')):
            stamp = inbound.get(field)
            if not stamp:
                row[key] = None
                continue
            try:
                parsed = datetime.fromisoformat(str(stamp))
            except (TypeError, ValueError):
                row[key] = None
                continue
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            row[key] = round(max(0.0, (datetime.utcnow() - parsed).total_seconds()), 3)
        ages[sid] = row
    return ages


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
        from panel.core import subscription_cache
        checks['subscription_cache'] = {'state': 'ok', **subscription_cache.metrics()}
    except Exception as exc:
        checks['subscription_cache'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from app import GLOBAL_SERVER_DATA  # deferred: app-level state
        from panel.core import refresh_policy
        from panel.jobs import schedulers as scheduler_jobs
        server_states = refresh_policy.server_states()
        sync_summary = refresh_policy.sync_summary()
        # The two freshness layers come from the snapshot itself (each block's own
        # stamps), because they describe the DATA being served, not the scheduler.
        stamp_ages = _block_stamp_ages(GLOBAL_SERVER_DATA, now=time.time())
        servers_sync = {}
        for sid in sorted(server_states):
            row = refresh_policy.server_sync_state(sid)
            row.update(stamp_ages.get(str(sid)) or {})
            servers_sync[sid] = row
        # The panels that are not live, in the same shape: the aggregate tells the
        # operator whether it is one panel or the whole sync path.
        attention = {sid: row for sid, row in servers_sync.items()
                     if row.get('sync_health') in ('stale', 'backoff', 'down')}
        checks['refresh_policy'] = {
            'state': 'ok',
            **refresh_policy.status(
                snapshot_age=refresh_policy.snapshot_age_seconds(
                    GLOBAL_SERVER_DATA.get('last_update'))),
            # Phase 10: per-server cadence, so "why is panel 3 behind?" is answerable
            # without reading the process memory of the fetcher worker.
            'servers': server_states,
            'servers_tracked': len(server_states),
            'servers_due': sum(1 for row in server_states.values() if row.get('due')),
            # Aggregate health, the scheduler's own evidence (queue delay, worker
            # saturation) and the full per-server sync state: mode, watched, inflight,
            # wake_pending, every stamp and age, the two revisions and the health word.
            # Ids, modes, ages and counters only -- never a credential, address or email.
            'sync': sync_summary,
            'scheduler': scheduler_jobs.scheduler_metrics(),
            'servers_sync': servers_sync,
            'servers_attention': attention,
            'server_intervals': {
                'active_seconds': refresh_policy.server_active_seconds(),
                'warm_seconds': refresh_policy.server_warm_seconds(),
                'idle_seconds': refresh_policy.server_idle_seconds(),
                'active_ttl_seconds': refresh_policy.server_active_ttl(),
                'warm_ttl_seconds': refresh_policy.server_warm_ttl(),
                'idle_jitter_seconds': refresh_policy.idle_jitter_span(),
                'backoff_base_seconds': refresh_policy.server_backoff_base(),
                'backoff_max_seconds': refresh_policy.server_backoff_max(),
                'watch_limit': refresh_policy.server_watch_limit(),
                'fetch_batch_servers': refresh_policy.fetch_batch_limit(),
                'client_fence_seconds': refresh_policy.client_fence_seconds(),
                'renew_baseline_max_age_seconds': refresh_policy.baseline_max_age_seconds(),
            },
        }
    except Exception as exc:
        checks['refresh_policy'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.models import Server
        from panel.services import xui_compat
        # Per-panel compatibility: which version was detected, which profile is
        # active, how it was detected, whether it is certified and whether the
        # credentials are usable. A panel whose management cannot work because of
        # insufficient token scope must not sit under a green state.
        panels = []
        degraded = 0
        for srv in db.session.query(Server).order_by(Server.id).all():
            compat = xui_compat.cached_compatibility(srv.id)
            if compat is None:
                row = {
                    'server_id': srv.id,
                    'name': srv.name,
                    'detected_version': None,
                    'normalized_version': None,
                    'family': None,
                    'profile': xui_compat.PROFILE_BASELINE_V3.name,
                    'detection_source': xui_compat.SOURCE_NONE,
                    'confidence': xui_compat.CONF_UNKNOWN,
                    'certification': xui_compat.CERT_UNVERIFIED,
                    'auth_state': 'unknown',
                    'warnings': [xui_compat.WARN_VERSION_UNKNOWN],
                }
                degraded += 1
            else:
                row = {'server_id': srv.id, 'name': srv.name}
                row.update(compat.as_public_dict())
                # Only a certified panel with no degraded condition is healthy.
                if (compat.certification != xui_compat.CERT_SUPPORTED
                        or compat.warnings):
                    degraded += 1
            panels.append(row)
        checks['panel_compatibility'] = {
            'state': 'ok' if degraded == 0 else 'warning',
            'panels_total': len(panels),
            'panels_degraded': degraded,
            'profiles': sorted(xui_compat.CERTIFIED_FAMILIES_PROFILE_NAMES),
            'panels': panels,
        }
    except Exception as exc:
        checks['panel_compatibility'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.core import fetch_sequence, refresh_policy
        from panel.services import depletion_pipeline
        # The telemetry pipeline block answers the two questions an operator has
        # after a missed reminder: is the detector running in this install, and is
        # anything stuck in the outbox? Counters only -- never a phone number,
        # an email address or a message body.
        status = depletion_pipeline.status()
        checks['telemetry_pipeline'] = {
            # The pipeline reports its OWN state: partial coverage (one panel refused
            # by the transport policy, a missing worker heartbeat, an exhausted retry
            # ladder) must not sit under a green "ok".
            'state': status.pop('state', 'ok'),
            **status,
            'fetch_sequence': fetch_sequence.status(),
            'watch_marks': refresh_policy.server_watch_marks(),
        }
    except Exception as exc:
        checks['telemetry_pipeline'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.services.usage_intelligence import observability, shadow
        from panel.services.usage_intelligence.recommendation import recommendation_mode
        checks['usage_intelligence'] = {
            'state': 'ok',
            'mode': recommendation_mode(),
            **observability.snapshot(),
            'shadow': shadow.shadow_metrics(),
        }
    except Exception as exc:
        checks['usage_intelligence'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.core.db_pool import pool_summary
        checks['db_pool'] = {'state': 'ok', **pool_summary(db.engine)}
    except Exception as exc:
        checks['db_pool'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.core.db_pool import host_of, pg_health, ssl_mode
        info = pg_health(db.engine)
        if info.get('state') == 'skipped':
            checks['postgres'] = {'state': 'ok', 'detail': 'sqlite deployment'}
        else:
            mode = ssl_mode()
            remote = bool(host_of(db.engine.url))
            tls_warning = None
            if remote and mode in ('', 'disable', 'allow', 'prefer'):
                tls_warning = (
                    'PostgreSQL at %s is configured with sslmode=%s; use require or '
                    'verify-full.' % (host_of(db.engine.url), mode or 'unset'))
            state = 'ok'
            if info.get('state') != 'ok' or tls_warning:
                state = 'warning'
            checks['postgres'] = {
                'state': state, **info, 'sslmode': mode or None,
                'tls_warning': tls_warning,
            }
    except Exception as exc:
        checks['postgres'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.core.panel_limits import panel_metrics
        checks['panel_limits'] = {'state': 'ok', **panel_metrics()}
    except Exception as exc:
        checks['panel_limits'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.services import retention as _retention
        checks['retention'] = {'state': 'ok', **_retention.status()}
    except Exception as exc:
        checks['retention'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.services import audit as _audit
        chain = _audit.verify_chain(limit=2000)
        checks['audit_chain'] = {
            'state': 'ok' if chain.get('ok') else 'warning', **chain}
    except Exception as exc:
        checks['audit_chain'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.core import http_metrics
        metrics = http_metrics.snapshot(limit=10)
        state = 'warning' if (metrics.get('error_rate') or 0) > 0.05 else 'ok'
        checks['http_metrics'] = {'state': state, **metrics}
    except Exception as exc:
        checks['http_metrics'] = {'state': 'unknown', 'error': str(exc)[:200]}

    try:
        from panel.jobs.schedulers import worker_inventory
        info = worker_inventory()
        failed = sorted(
            name for name, item in (info.get('workers') or {}).items()
            if item.get('state') == 'failed')
        degraded = bool(failed or info.get('singleton_errors'))
        checks['workers'] = {'state': 'warning' if degraded else 'ok',
                             'failed': failed, **info}
    except Exception as exc:
        checks['workers'] = {'state': 'unknown', 'error': str(exc)[:200]}

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
