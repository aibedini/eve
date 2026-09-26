"""SMS and WhatsApp gateway API routes (extracted from app.py)."""
import os
import json
from datetime import datetime
import requests
import threading
import time
import uuid

from flask import Blueprint, jsonify, request, session

from panel.core.phone import _extract_iran_mobile_from_text
from panel.core.redis_client import get_redis
from panel.extensions import db
from panel.models import Admin, PendingSms, SmsSendLog, SmsScanRun, SmsScanDecision, SystemConfig, ServiceNotificationEvent
from sqlalchemy import or_, func
from panel.routes.common import permission_required
from panel.security import outbound_tls_verify
from panel.services import gmweb_contract
from panel.services.gmweb_transport_probe import (
    PROBE_AUTH_FAILED, PROBE_CONNECTED, PROBE_CONTRACT_MISSING, PROBE_DIAGNOSTICS,
    PROBE_INVALID, PROBE_NOT_CONFIGURED, PROBE_SCOPE_DENIED, PROBE_UNREACHABLE,
    PROBE_VERSION_MISMATCH,
)
from panel.services.gmweb_transport_probe import PROBE_TIMEOUT_SECONDS as _PROBE_TIMEOUT_SECONDS
from panel.services.gmweb_transport_probe import (
    probe_state_for_status as _sms_transport_probe_state,
    project_sections as _sms_transport_health_sections,
)


bp = Blueprint('messaging', __name__)


def _probe_whatsapp_gateway(gateway_url: str, timeout_seconds: int, api_key: str | None = None, provider: str | None = None) -> tuple[bool, int | None, str | None]:
    from app import _normalize_whatsapp_gateway_url  # deferred: app-level helper, avoids circular import
    normalized = _normalize_whatsapp_gateway_url(gateway_url)
    if not normalized:
        return False, None, 'empty_gateway_url'

    headers = {}
    token = (api_key or '').strip()
    if provider == 'openwa':
        # OpenWA exposes GET /api/health and authenticates via X-API-Key.
        health_path = f"{normalized}/api/health"
        if token:
            headers['X-API-Key'] = token
    else:
        health_path = f"{normalized}/health"
        if token:
            headers['Authorization'] = f"Bearer {token}"

    try:
        response = requests.get(
            health_path,
            headers=headers,
            timeout=max(3, int(timeout_seconds or 10)),
            verify=outbound_tls_verify('EVE_WHATSAPP_CA_BUNDLE'),
        )
        status_code = int(response.status_code)
        if 200 <= status_code < 300:
            return True, status_code, None
        return False, status_code, 'non_success_status'
    except Exception as exc:
        return False, None, str(exc)


def _build_whatsapp_gateway_candidates(host_hint: str | None = None, configured_url: str | None = None) -> list[str]:
    from app import _normalize_whatsapp_gateway_url  # deferred: app-level helper, avoids circular import
    candidates = []
    seen = set()

    def add(raw_value: str | None):
        normalized = _normalize_whatsapp_gateway_url(raw_value)
        if not normalized:
            return
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    add(configured_url)
    add(os.environ.get('WHATSAPP_GATEWAY_URL'))

    host = (host_hint or '').strip().split(':')[0].strip().lower()
    local_hosts = ['127.0.0.1', 'localhost']
    if host and host not in ('127.0.0.1', 'localhost'):
        local_hosts.append(host)

    for h in local_hosts:
        add(f"http://{h}:2785")  # OpenWA default API port
        add(f"http://{h}:3000")
        add(f"http://{h}:3001")
        add(f"http://{h}:8080")

    if host and host not in ('127.0.0.1', 'localhost'):
        add(f"https://{host}/wa-gateway")
        add(f"https://{host}/whatsapp-gateway")

    return candidates


def _promote_delayed_high_sms(cfg: dict | None = None) -> dict:
    """Ask GMweb to release every delayed high-priority job to the queue front.

    This mutates existing gateway jobs; it deliberately does not resubmit SMS
    payloads, so pressing the queue button cannot create duplicate messages.
    Gateway contract: POST /queue/promote-high with releaseDelayed/all enabled.
    """
    from app import _get_sms_runtime_settings  # deferred: app-level helper, avoids circular import
    cfg = cfg or _get_sms_runtime_settings()
    base = (cfg.get('base_url') or '').strip().rstrip('/')
    api_key = (cfg.get('api_key') or '').strip()
    if not base or not api_key:
        return {'success': False, 'reason': 'gateway_not_configured', 'status_code': None}

    try:
        resp = requests.post(
            f"{base}/queue/promote-high",
            json={
                'all': True,
                'priority': 'critical',
                'states': ['delayed'],
                'releaseDelayed': True,
                'position': 'front',
            },
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            timeout=int(cfg.get('timeout_seconds') or 15),
        )
    except Exception as exc:
        return {'success': False, 'reason': f'gateway_error: {exc}', 'status_code': None}

    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    if resp.status_code in (200, 202):
        promoted = body.get('promoted') if isinstance(body, dict) else None
        if promoted is None and isinstance(body, dict):
            promoted = body.get('count')
        return {
            'success': True,
            'status_code': resp.status_code,
            'promoted': int(promoted or 0),
            'gateway': body if isinstance(body, dict) else {},
        }

    if resp.status_code == 404:
        reason = 'gateway_missing_promote_endpoint'
    else:
        detail = None
        if isinstance(body, dict):
            detail = body.get('error') or body.get('message')
        reason = detail or f'gateway_http_{resp.status_code}'
    return {'success': False, 'reason': reason, 'status_code': resp.status_code}


def _sms_scan_cancel_set():
    """Request cancellation reachable by whichever worker runs the scan."""
    from app import (  # deferred: app-level helper, avoids circular import
        SMS_SCAN_CANCEL, SMS_SCAN_CANCEL_REDIS_KEY, SMS_SCAN_REDIS_TTL,
    )
    SMS_SCAN_CANCEL.set()
    client = get_redis()
    if client is not None:
        try:
            client.set(SMS_SCAN_CANCEL_REDIS_KEY, b'1', ex=SMS_SCAN_REDIS_TTL)
        except Exception:
            pass


@bp.route('/api/sms/test-connection', methods=['POST'])
@permission_required('secrets.manage')
def test_sms_connection():
    """Verify the selected SMS gateway: /health then authenticated /ready."""
    from app import (  # deferred: app-level helper, avoids circular import
        _get_sms_runtime_settings, app,
    )
    cfg = _get_sms_runtime_settings()
    base = (cfg.get('base_url') or '').strip().rstrip('/')
    api_key = (cfg.get('api_key') or '').strip()
    if not base:
        return jsonify({'success': False, 'error': 'Selected SMS gateway Base URL is not configured.'}), 400
    if not api_key:
        return jsonify({'success': False, 'error': 'Selected SMS gateway API key is not configured.'}), 400
    timeout = int(cfg.get('timeout_seconds') or 15)
    provider_label = 'Custom HTTP' if cfg.get('provider') == 'custom_http' else 'GMweb'
    try:
        h = requests.get(f"{base}/health", timeout=timeout)
        if h.status_code != 200:
            return jsonify({'success': False, 'error': f'Gateway /health returned HTTP {h.status_code}.'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f'Cannot reach gateway: {e}'}), 400
    # /ready also validates the token (401 → bad key, 503 → not paired yet).
    try:
        r = requests.get(f"{base}/ready", headers={'Authorization': f'Bearer {api_key}'}, timeout=timeout)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Gateway reachable but /ready failed: {e}'}), 400
    if r.status_code == 401:
        return jsonify({'success': False, 'error': 'Invalid API key (gateway returned 401).'}), 400
    if r.status_code == 503:
        return jsonify({'success': True, 'ready': False,
                        'provider': cfg.get('provider', 'gmweb'),
                        'message': 'Gateway reachable and key valid, but it is not ready to send yet (503).'})
    if r.status_code == 200:
        return jsonify({'success': True, 'ready': True,
                        'provider': cfg.get('provider', 'gmweb'),
                        'message': f'{provider_label} gateway reachable, key valid, and ready to send.'})
    return jsonify({'success': False, 'error': f'Gateway /ready returned HTTP {r.status_code}.'}), 400


@bp.route('/api/sms/test-send', methods=['POST'])
@permission_required('secrets.manage')
def sms_test_send():
    """Send a real test SMS so the admin can confirm the gateway works end-to-end.

    Recipient resolution (in order):
      1) the logged-in superadmin's own Support SMS number (their profile), then
      2) the panel-wide Support SMS number from the Contact section.
    If neither is set, returns a helpful message telling them where to add one.
    Bypasses the enabled/trigger/owner gates — it only needs the gateway set up.
    """
    from app import (  # deferred: app-level helper, avoids circular import
        _get_sms_runtime_settings, _send_sms_via_gmweb,
        _sms_accepted_status, _sms_log_row, _sms_refund_daily_segments,
        _sms_segment_info, _sms_take_send_slot, app,
    )
    cfg = _get_sms_runtime_settings()
    if not (cfg.get('base_url') and cfg.get('api_key')):
        return jsonify({'success': False,
                        'error': 'The selected SMS gateway is not configured. Set its Base URL and API key (and Save) first.'}), 400

    user = db.session.get(Admin, session['admin_id'])
    own_raw = (getattr(user, 'support_sms', None) or '').strip()
    contact_conf = db.session.get(SystemConfig, 'support_sms')
    contact_raw = ((contact_conf.value if contact_conf else '') or '').strip()

    source = None
    recipient = ''
    if own_raw:
        recipient = _extract_iran_mobile_from_text(own_raw)
        if recipient:
            source = 'superadmin_profile'
    if not recipient and contact_raw:
        recipient = _extract_iran_mobile_from_text(contact_raw)
        if recipient:
            source = 'panel_contact'

    if not recipient:
        if own_raw or contact_raw:
            bad = own_raw or contact_raw
            return jsonify({'success': False,
                            'error': f'The configured number ("{bad}") is not a valid Iranian mobile. Fix it in your profile (Support SMS) or in the Contact section.'}), 400
        return jsonify({'success': False, 'needs_number': True,
                        'error': 'No phone number set. Add your Support SMS number in your profile, or set the panel-wide Support SMS in the Contact section, then test again.'}), 400

    text = ('EVE panel — test SMS ✅\n'
            'پیام تستی پنل. اگر این پیام را دریافت کردید، اتوماسیون SMS درست کار می‌کند.')
    segment_info = _sms_segment_info(text)
    segments = segment_info['sms_segments']
    slot_ok, slot_reason = _sms_take_send_slot(recipient, cfg, segments, priority='test')
    if not slot_ok:
        _sms_log_row(None, (getattr(user, 'username', None) or 'test').strip().lower(),
                     0, 'Eve', 'test', recipient, 'skipped', slot_reason, segment_info)
        return jsonify({'success': False, 'recipient': recipient,
                        'error': 'Daily SMS segment limit reached.'}), 429
    res = _send_sms_via_gmweb(recipient, text, cfg, priority='critical',
                              idempotency_key=f"test-{int(time.time())}")
    if res.get('sent'):
        _sms_log_row(None, (getattr(user, 'username', None) or 'test').strip().lower(),
                     0, 'Eve', 'test', recipient, _sms_accepted_status(res), None, res)
        src_label = 'your profile number' if source == 'superadmin_profile' else 'the panel contact number'
        message = ('Test SMS queued' if res.get('request_id') else 'Test SMS sent')
        return jsonify({'success': True, 'recipient': recipient, 'source': source,
                        'request_id': res.get('request_id'), 'job_id': res.get('job_id'),
                        'status': _sms_accepted_status(res),
                        'message': f'{message} for {recipient} ({src_label}).'})
    if res.get('manual_review'):
        _sms_log_row(None, (getattr(user, 'username', None) or 'test').strip().lower(),
                     0, 'Eve', 'test', recipient, 'manual_review',
                     'unverified_manual_review', res)
        return jsonify({
            'success': False,
            'manual_review': True,
            'recipient': recipient,
            'request_id': res.get('request_id'),
            'error': 'The SMS gateway submitted the test once but could not verify it. Do not resend automatically; review the provider.',
        }), 409
    _sms_refund_daily_segments(segments)
    _sms_log_row(None, (getattr(user, 'username', None) or 'test').strip().lower(),
                 0, 'Eve', 'test', recipient, 'failed', res.get('reason'), res)
    return jsonify({'success': False, 'recipient': recipient,
                    'error': f'Send failed ({res.get("reason") or "unknown"}).',
                    'status_code': res.get('status_code')}), 400


@bp.route('/api/sms/scan/run', methods=['POST'])
@permission_required('secrets.manage')
def sms_scan_run():
    """Kick off the automated state-based SMS scan now (non-blocking). The UI then
    polls /api/sms/scan/status to watch progress."""
    from app import (  # deferred: app-level helper, avoids circular import
        SMS_SCAN_STATES, _get_sms_runtime_settings,
        _normalize_sms_scan_states, _run_sms_depletion_scan,
        _sms_gateway_ready, _sms_in_quiet_hours, _sms_scan_set,
        _sms_scan_snapshot, _utc_iso_now, app,
    )
    try:
        cfg = _get_sms_runtime_settings()
    except Exception as exc:
        app.logger.exception('[sms-scan/run] failed to read settings')
        return jsonify({'success': False, 'error': f'Could not load SMS settings: {exc}'}), 500

    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}
    requested_states = _normalize_sms_scan_states(payload.get('states'))

    if not cfg.get('enabled'):
        return jsonify({'success': False, 'error': 'SMS automation is disabled. Enable it (and Save) first.'}), 400
    if not (cfg.get('base_url') and cfg.get('api_key')):
        return jsonify({'success': False, 'error': 'The selected SMS gateway is not configured.'}), 400
    if not requested_states and not any(cfg.get(f'trigger_{s}') for s in SMS_SCAN_STATES):
        return jsonify({'success': False, 'error': 'No state triggers are enabled (near expiry / low volume / expired / ended).'}), 400
    if payload.get('states') is not None and not requested_states:
        return jsonify({'success': False, 'error': 'Select at least one reminder state to start.'}), 400
    ready, ready_reason, ready_status = _sms_gateway_ready(cfg)
    if not ready:
        if ready_reason == 'gateway_not_paired':
            message = 'The SMS gateway is reachable but not ready. Prepare it first, then start again.'
        elif ready_reason == 'gateway_auth_failed':
            message = 'The SMS gateway rejected the API key (401). Check the configured key.'
        else:
            message = f'SMS gateway is not ready: {ready_reason or "unknown"}'
        return jsonify({'success': False, 'error': message,
                        'reason': ready_reason, 'gateway_status': ready_status}), 400
    if _sms_in_quiet_hours(cfg):
        return jsonify({'success': False,
                        'error': f"Quiet hours are active ({int(cfg.get('quiet_start', 0)):02d}:00–{int(cfg.get('quiet_end', 0)):02d}:00 Tehran). Sends are paused and resume automatically after the window. Turn off quiet hours to send now."}), 400

    running_job = _sms_scan_snapshot()
    if running_job.get('state') == 'running':
        return jsonify({'success': False, 'error': 'A scan is already running.',
                        'job': running_job}), 409

    jid = uuid.uuid4().hex
    run = SmsScanRun(run_id=jid, triggered_by='manual',
                     selected_states=json.dumps(requested_states),
                     priority_order=json.dumps(requested_states),
                     started_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.session.add(run)
    db.session.commit()

    def _worker():
        with app.app_context():
            try:
                _run_sms_depletion_scan(
                    job_id=jid,
                    triggered_by='manual',
                    states=requested_states if payload.get('states') is not None else None,
                )
            except Exception:
                app.logger.exception('[sms-scan] manual run failed')
                _sms_scan_set(state='error', finished_at=_utc_iso_now())

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({'success': True, 'job_id': jid, 'states': requested_states})


@bp.route('/api/sms/scan/preview', methods=['POST'])
@permission_required('secrets.manage')
def sms_scan_preview():
    """Fail closed until preview can evaluate candidates without reconciliation."""
    return jsonify({'success': False,
                    'error': 'SMS audience preview is temporarily unavailable.'}), 503


@bp.route('/api/sms/notification-debt', methods=['GET'])
@permission_required('secrets.manage')
def sms_notification_debt():
    """Outstanding durable obligations, independent of scan/send-log history."""
    active = ('pending', 'retry', 'sending', 'gateway_accepted')
    query = ServiceNotificationEvent.query.filter(
        ServiceNotificationEvent.status.in_(active))
    total = query.count()
    terminal = query.filter(ServiceNotificationEvent.notification_kind.in_(
        ('volume_ended', 'expired'))).count()
    oldest = query.with_entities(func.min(ServiceNotificationEvent.created_at)).scalar()
    try:
        limit = min(max(int(request.args.get('limit', 50)), 1), 200)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid limit.'}), 400
    rows = query.order_by(ServiceNotificationEvent.created_at.asc()).limit(limit).all()
    data = []
    for row in rows:
        item = row.to_dict()
        item['account'] = row.client_email
        item['needs_attention'] = (row.notification_kind in ('volume_ended', 'expired')
                                   and int(row.attempt_count or 0) >= 7)
        data.append(item)
    response = jsonify({
        'success': True, 'active': total, 'terminal': terminal,
        'retrying': query.filter(ServiceNotificationEvent.status == 'retry').count(),
        'needs_attention': query.filter(
            ServiceNotificationEvent.notification_kind.in_(('volume_ended', 'expired')),
            ServiceNotificationEvent.attempt_count >= 7).count(),
        'oldest_age_seconds': (max(0, int((datetime.utcnow() - oldest).total_seconds()))
                               if oldest else None),
        'obligations': data,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/sms/transport-health', methods=['GET'])
@permission_required('secrets.manage')
def sms_transport_health():
    """Probe the GMweb transport-health contract and report the real verdict.

    A failed probe is reported as a machine-readable probe_state plus a
    diagnostic, never as an anonymous "unknown" the operator cannot act on. The
    request goes through panel.services.gmweb_contract so the path, the
    Authorization: Bearer header and the base-URL rules are the same ones every
    other GMweb call uses.
    """
    from app import _get_sms_runtime_settings
    cfg = _get_sms_runtime_settings()
    validated = gmweb_contract.validate_base_url(cfg.get('base_url'))
    base_url = validated.get('base')
    api_key = (cfg.get('api_key') or '').strip()
    expected_version = gmweb_contract.transport_health_contract_version()

    def verdict(state, *, status=None, contract=None, health=None, error=None):
        payload = {
            'success': False,
            'probe_state': state,
            'diagnostic': error or PROBE_DIAGNOSTICS.get(state),
            'http_status': status,
            'contract_version': contract,
            'contract_supported': contract is not None and contract == expected_version,
            'health': health,
        }
        if state == PROBE_CONNECTED:
            payload['success'] = True
            payload.pop('diagnostic')
        response = jsonify(payload)
        response.headers['Cache-Control'] = 'no-store'
        if state == PROBE_CONNECTED:
            return response
        return response, 400 if state == PROBE_NOT_CONFIGURED else 502

    if not base_url or not api_key:
        return verdict(PROBE_NOT_CONFIGURED,
                       error=validated.get('reason') or PROBE_DIAGNOSTICS[PROBE_NOT_CONFIGURED])

    try:
        response = requests.get(
            base_url + gmweb_contract.endpoint_path('transport_health'),
            headers=gmweb_contract.request_headers(api_key),
            timeout=_PROBE_TIMEOUT_SECONDS,
            verify=outbound_tls_verify())
    except Exception as exc:
        return verdict(PROBE_UNREACHABLE, error='%s: %s' % (type(exc).__name__, exc))

    if response.status_code >= 400:
        return verdict(_sms_transport_probe_state(response.status_code),
                       status=response.status_code)
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        return verdict(PROBE_INVALID, status=response.status_code,
                       error='GMweb answered with a body that is not JSON.')
    if not isinstance(payload, dict):
        return verdict(PROBE_INVALID, status=response.status_code,
                       error='GMweb answered with a %s, not an object.' % type(payload).__name__)

    version = payload.get('contract_version')
    if version != expected_version:
        return verdict(PROBE_VERSION_MISMATCH, status=response.status_code, contract=version,
                       error='GMweb reports contract_version %r, expected %r.'
                             % (version, expected_version))

    health = _sms_transport_health_sections(payload)
    if not isinstance(health.get('gmweb', {}).get('ready'), bool):
        return verdict(PROBE_INVALID, status=response.status_code, contract=version,
                       error='GMweb transport-health response has no readiness data.')
    return verdict(PROBE_CONNECTED, status=response.status_code, contract=version, health=health)


@bp.route('/api/sms/reports', methods=['GET'])
@permission_required('secrets.manage')
def sms_reports():
    """Server-side aggregates for the SMS intelligence/reporting view."""
    start_text = (request.args.get('start') or '').strip()
    end_text = (request.args.get('end') or '').strip()
    group = (request.args.get('group_by') or 'state').strip().lower()
    allowed_groups = {'hour': func.strftime('%Y-%m-%d %H:00', SmsScanDecision.created_at),
                      'day': func.strftime('%Y-%m-%d', SmsScanDecision.created_at),
                      'month': func.strftime('%Y-%m', SmsScanDecision.created_at),
                      'year': func.strftime('%Y', SmsScanDecision.created_at),
                      'run': SmsScanDecision.run_id, 'state': SmsScanDecision.state,
                      'reason': SmsScanDecision.reason_code,
                      'server': SmsScanDecision.server_id}
    if group not in allowed_groups:
        return jsonify({'success': False, 'error': 'Invalid group_by.'}), 400
    query = SmsScanDecision.query
    try:
        if start_text:
            query = query.filter(SmsScanDecision.created_at >= datetime.fromisoformat(start_text.replace('Z', '+00:00')).replace(tzinfo=None))
        if end_text:
            query = query.filter(SmsScanDecision.created_at <= datetime.fromisoformat(end_text.replace('Z', '+00:00')).replace(tzinfo=None))
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid date range.'}), 400
    totals = dict(query.with_entities(SmsScanDecision.disposition, func.count(SmsScanDecision.id)).group_by(SmsScanDecision.disposition).all())
    grouped = query.with_entities(allowed_groups[group].label('bucket'), SmsScanDecision.disposition, func.count(SmsScanDecision.id)).group_by(allowed_groups[group], SmsScanDecision.disposition).order_by(allowed_groups[group]).all()
    buckets = {}
    for bucket, disposition, count in grouped:
        buckets.setdefault(str(bucket), {})[disposition or 'unknown'] = count
    reasons = dict(query.with_entities(SmsScanDecision.reason_code, func.count(SmsScanDecision.id)).filter(SmsScanDecision.reason_code.isnot(None)).group_by(SmsScanDecision.reason_code).order_by(func.count(SmsScanDecision.id).desc()).all())
    return jsonify({'success': True, 'group_by': group, 'totals': totals, 'groups': buckets, 'reasons': reasons})


@bp.route('/api/sms/scan/runs/<run_id>', methods=['GET'])
@permission_required('secrets.manage')
def sms_scan_run_detail(run_id):
    run = SmsScanRun.query.filter_by(run_id=run_id).first_or_404()
    counts = dict(db.session.query(SmsScanDecision.disposition, func.count(SmsScanDecision.id))
                  .filter(SmsScanDecision.run_id == run_id)
                  .group_by(SmsScanDecision.disposition).all())
    run.matched_count = sum(counts.values())
    run.submitted_count = counts.get('submitted', 0)
    run.confirmed_count = counts.get('confirmed', 0)
    run.inflight_count = counts.get('inflight', 0)
    run.deferred_count = counts.get('deferred', 0)
    run.suppressed_count = counts.get('suppressed', 0)
    run.failed_count = sum(counts.get(k, 0) for k in ('failed_retryable', 'failed_terminal'))
    run.cancelled_count = sum(counts.get(k, 0) for k in ('cancelled', 'superseded'))
    run.audit_gap_count = max(0, run.matched_count - sum((
        run.confirmed_count, run.inflight_count, run.deferred_count,
        run.suppressed_count, run.failed_count, run.cancelled_count)))
    query = SmsScanDecision.query.filter_by(run_id=run_id).order_by(SmsScanDecision.created_at.desc())
    limit = min(max(int(request.args.get('limit', 100)), 1), 500)
    decisions = query.limit(limit).all()
    data = run.to_dict()
    data['decisions'] = [d.to_dict() for d in decisions]
    return jsonify({'success': True, 'run': data})


@bp.route('/api/sms/scan/runs/<run_id>/decisions', methods=['GET'])
@permission_required('secrets.manage')
def sms_scan_run_decisions(run_id):
    SmsScanRun.query.filter_by(run_id=run_id).first_or_404()
    try:
        limit = min(max(int(request.args.get('limit', 100)), 1), 500)
        offset = max(int(request.args.get('offset', 0)), 0)
    except (TypeError, ValueError):
        limit, offset = 100, 0
    query = SmsScanDecision.query.filter_by(run_id=run_id)
    for field in ('state', 'disposition', 'reason_code', 'server_id'):
        value = (request.args.get(field) or '').strip()
        if value:
            query = query.filter(getattr(SmsScanDecision, field) == value)
    q = (request.args.get('q') or '').strip().lower()
    if q:
        term = f'%{q}%'
        query = query.filter(or_(func.lower(SmsScanDecision.client_email).like(term),
                                func.lower(SmsScanDecision.service_key).like(term),
                                func.lower(SmsScanDecision.gateway_request_id).like(term),
                                func.lower(SmsScanDecision.gateway_job_id).like(term)))
    total = query.count()
    rows = query.order_by(SmsScanDecision.created_at.desc()).offset(offset).limit(limit).all()
    return jsonify({'success': True, 'decisions': [row.to_dict() for row in rows],
                    'total': total, 'offset': offset, 'limit': limit})


@bp.route('/api/sms/scan/runs', methods=['GET'])
@permission_required('secrets.manage')
def sms_scan_runs():
    limit = min(max(int(request.args.get('limit', 50)), 1), 200)
    rows = SmsScanRun.query.order_by(SmsScanRun.started_at.desc()).limit(limit).all()
    return jsonify({'success': True, 'runs': [r.to_dict() for r in rows]})


@bp.route('/api/sms/decisions', methods=['GET'])
@permission_required('secrets.manage')
def sms_decisions():
    """Search the durable candidate audit across runs, including non-sends."""
    try:
        limit = min(max(int(request.args.get('limit', 20)), 1), 100)
        offset = max(int(request.args.get('offset', 0)), 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid pagination.'}), 400
    query = SmsScanDecision.query
    for field in ('run_id', 'state', 'reason_code'):
        value = (request.args.get(field) or '').strip()
        if value:
            query = query.filter(getattr(SmsScanDecision, field) == value)
    server_id = (request.args.get('server_id') or '').strip()
    if server_id:
        if not server_id.isdigit():
            return jsonify({'success': False, 'error': 'Invalid server_id.'}), 400
        query = query.filter(SmsScanDecision.server_id == int(server_id))
    disposition = (request.args.get('disposition') or '').strip()
    if disposition == 'failed':
        query = query.filter(SmsScanDecision.disposition.in_(('failed_retryable', 'failed_terminal')))
    elif disposition:
        query = query.filter(SmsScanDecision.disposition == disposition)
    for argument, operator in (('from', lambda value: SmsScanDecision.created_at >= value),
                               ('to', lambda value: SmsScanDecision.created_at < value)):
        value = (request.args.get(argument) or '').strip()
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                return jsonify({'success': False, 'error': f'Invalid {argument} timestamp.'}), 400
            query = query.filter(operator(parsed.replace(tzinfo=None)))
    search = (request.args.get('q') or '').strip().lower()
    if search:
        term = f'%{search}%'
        query = query.filter(or_(func.lower(SmsScanDecision.client_email).like(term),
                                 func.lower(SmsScanDecision.server_name).like(term),
                                 func.lower(SmsScanDecision.service_key).like(term),
                                 func.lower(SmsScanDecision.gateway_request_id).like(term),
                                 func.lower(SmsScanDecision.gateway_job_id).like(term),
                                 func.lower(SmsScanDecision.run_id).like(term)))
    total = query.count()
    rows = query.order_by(SmsScanDecision.created_at.desc(), SmsScanDecision.id.desc()).offset(offset).limit(limit).all()
    response = jsonify({'success': True, 'decisions': [row.to_dict() for row in rows],
                        'total': total, 'offset': offset, 'limit': limit})
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/sms/scan/status', methods=['GET'])
@permission_required('secrets.manage')
def sms_scan_status():
    """Live progress of the current/last SMS scan (shared across all workers)."""
    from app import (  # deferred: app-level helper, avoids circular import
        _get_sms_runtime_settings, _sms_announcement_segments_used_today,
        _sms_db_segment_stats_today, _sms_db_segments_used_this_hour,
        _sms_scan_snapshot, app,
    )
    try:
        pending_high = PendingSms.query.count()
    except Exception:
        pending_high = 0
    segment_stats = _sms_db_segment_stats_today()
    sms_cfg = _get_sms_runtime_settings()
    try:
        ann_segments_used = _sms_announcement_segments_used_today()
    except Exception:
        ann_segments_used = 0
    try:
        hour_segments_used = _sms_db_segments_used_this_hour()
    except Exception:
        hour_segments_used = 0
    job_snapshot = _sms_scan_snapshot()
    if job_snapshot.get('id'):
        run = SmsScanRun.query.filter_by(run_id=job_snapshot['id']).first()
        if run:
            if job_snapshot.get('state') not in ('running', 'starting'):
                run.status = 'completed' if job_snapshot.get('state') == 'done' else str(job_snapshot.get('state') or 'finished')
                run.finished_at = run.finished_at or datetime.utcnow()
            run.scanned_count = int(job_snapshot.get('total_clients') or 0)
            run.updated_at = datetime.utcnow()
            db.session.commit()
    return jsonify({
        'success': True,
        'job': job_snapshot,
        'pending_high': pending_high,
        'segments_used_today': segment_stats.get('completed', 0),
        'segments_completed_today': segment_stats.get('completed', 0),
        'segments_submitted_today': segment_stats.get('submitted', 0),
        'segments_failed_today': segment_stats.get('failed', 0),
        'segments_inflight_today': segment_stats.get('inflight', 0),
        'segment_daily_limit': int(sms_cfg.get('daily_limit') or 200),
        # Hourly throttle (0 = unlimited). Bulk lanes only; create/renew exempt.
        'segments_used_this_hour': hour_segments_used,
        'segment_hourly_limit': int(sms_cfg.get('hourly_limit') or 0),
        'announcement_segments_used_today': ann_segments_used,
        'announcement_daily_limit': int(sms_cfg.get('announcement_daily_limit') or 500),
    })


@bp.route('/api/sms/queue/promote-high', methods=['POST'])
@permission_required('secrets.manage')
def sms_queue_promote_high():
    """Move all delayed high-priority GMweb jobs to the queue front now."""
    from app import (  # deferred: app-level helper, avoids circular import
        _flush_pending_sms, _get_sms_runtime_settings, app,
    )
    cfg = _get_sms_runtime_settings()
    result = _promote_delayed_high_sms(cfg)
    if not result.get('success'):
        reason = result.get('reason') or 'gateway_rejected_request'
        if reason == 'gateway_missing_promote_endpoint':
            message = (
                'GMweb does not support POST /queue/promote-high yet. '
                'Update the gateway, then press this button again.'
            )
        else:
            message = f'Could not promote the GMweb queue: {reason}'
        return jsonify({
            'success': False,
            'error': message,
            'reason': reason,
            'status_code': result.get('status_code'),
        }), 502

    # Normally transactional jobs are already inside GMweb. If an older Eve
    # build left any high-priority rows locally, release those too, without
    # making this request wait for paced network sends.
    try:
        local_pending = PendingSms.query.count()
    except Exception:
        local_pending = 0
    if local_pending:
        def _worker():
            with app.app_context():
                try:
                    _flush_pending_sms(force=True)
                except Exception:
                    app.logger.exception('[sms-queue] forced local high flush failed')
        threading.Thread(target=_worker, daemon=True).start()

    return jsonify({
        'success': True,
        'promoted': result.get('promoted', 0),
        'local_pending_released': local_pending,
        'message': 'Delayed high-priority messages moved to the front.',
    })


@bp.route('/api/sms/scan/stop', methods=['POST'])
@permission_required('secrets.manage')
def sms_scan_stop():
    """Signal the running scan to abort after the current item, then disable
    SMS automation so no new scan starts automatically. The UI should reflect
    the disabled state by unchecking the toggle."""
    from app import (  # deferred: app-level helper, avoids circular import
        SMS_AUTOMATION_ENABLED_KEY, _sms_scan_snapshot, app,
    )
    _sms_scan_cancel_set()
    # Persist sms_automation_enabled = false so the background worker skips
    # future cycles and the toggle shows the correct state on next page load.
    try:
        cfg_row = db.session.get(SystemConfig, SMS_AUTOMATION_ENABLED_KEY)
        if cfg_row:
            cfg_row.value = 'false'
        else:
            db.session.add(SystemConfig(key=SMS_AUTOMATION_ENABLED_KEY, value='false'))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify({'success': True, 'job': _sms_scan_snapshot()})


@bp.route('/api/sms/logs', methods=['GET'])
@permission_required('secrets.manage')
def sms_logs():
    """Recent SMS send-log history (newest first), paginated."""
    from app import app  # deferred: app-level helper, avoids circular import
    try:
        limit = max(1, min(int(request.args.get('limit', 100)), 1000))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    status_filter = (request.args.get('status') or '').strip().lower()
    state_filter = (request.args.get('state') or '').strip().lower()
    disposition_filter = (request.args.get('disposition') or '').strip().lower()
    reason_filter = (request.args.get('reason_code') or '').strip().lower()
    run_filter = (request.args.get('run_id') or '').strip()
    server_filter = (request.args.get('server_id') or '').strip()
    search = (request.args.get('q') or '').strip()
    q = SmsSendLog.query
    if status_filter == 'confirmed':
        q = q.filter(SmsSendLog.verification_status == 'confirmed')
    elif status_filter in ('queued', 'active', 'sent', 'completed', 'failed', 'skipped',
                           'cancelled', 'manual_review'):
        q = q.filter(SmsSendLog.status == status_filter)
    if state_filter in ('near_expiry', 'low_volume', 'expired', 'ended'):
        q = q.filter(SmsSendLog.state == state_filter)
    if run_filter:
        q = q.filter(SmsSendLog.job_id == run_filter)
    for argument, operator in (('from', lambda value: SmsSendLog.created_at >= value),
                               ('to', lambda value: SmsSendLog.created_at < value)):
        value = (request.args.get(argument) or '').strip()
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                return jsonify({'success': False, 'error': f'Invalid {argument} timestamp.'}), 400
            q = q.filter(operator(parsed.replace(tzinfo=None)))
    if server_filter.isdigit():
        q = q.filter(SmsSendLog.server_id == int(server_filter))
    if reason_filter:
        q = q.filter(func.lower(SmsSendLog.reason).contains(reason_filter))
    if disposition_filter:
        q = q.filter(func.lower(SmsSendLog.status) == disposition_filter)
    if search:
        term = f'%{search.lower()}%'
        q = q.filter(or_(func.lower(SmsSendLog.email).like(term),
                         func.lower(SmsSendLog.server_name).like(term),
                         func.lower(SmsSendLog.request_id).like(term),
                         func.lower(SmsSendLog.gateway_job_id).like(term),
                         func.lower(SmsSendLog.job_id).like(term),
                         func.lower(SmsSendLog.recipient).like(term)))
    total = q.count()
    rows = q.order_by(SmsSendLog.created_at.desc()).offset(offset).limit(limit).all()
    resp = jsonify({'success': True, 'logs': [r.to_dict() for r in rows],
                    'total': total, 'offset': offset, 'limit': limit})
    # Never let a proxy/browser serve a stale log — it must always reflect now.
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@bp.route('/api/sms/capacity', methods=['GET'])
@permission_required('secrets.manage')
def sms_capacity():
    """Expose the selected gateway's authenticated lane/capacity snapshot."""
    from panel.jobs.messaging import _get_gmweb_send_capacity

    result = _get_gmweb_send_capacity()
    code = 200 if result.get('ok') else 502
    return jsonify({'success': bool(result.get('ok')), **result}), code


@bp.route('/api/whatsapp/test-connection', methods=['POST'])
@permission_required('secrets.manage')
def test_whatsapp_connection():
    from app import (  # deferred: app-level helper, avoids circular import
        _get_whatsapp_runtime_settings, _openwa_session_status, app,
    )
    runtime_cfg = _get_whatsapp_runtime_settings()
    if runtime_cfg.get('deployment_region') == 'iran':
        return jsonify({
            'success': False,
            'error': 'WhatsApp automation is not available when the panel is deployed in Iran.',
            'blocked_reason': 'deployment_in_iran'
        }), 400

    gateway_url = (runtime_cfg.get('gateway_url') or '').strip()
    if not gateway_url:
        return jsonify({'success': False, 'error': 'WhatsApp gateway URL is not configured.'}), 400

    provider = (runtime_cfg.get('provider') or 'baileys').strip().lower()
    timeout_seconds = int(runtime_cfg.get('gateway_timeout_seconds') or 10)
    api_key = (runtime_cfg.get('gateway_api_key') or '').strip()

    ok, status_code, error_reason = _probe_whatsapp_gateway(
        gateway_url,
        timeout_seconds=timeout_seconds,
        api_key=api_key,
        provider=provider,
    )

    if ok and provider == 'openwa':
        # Health is up — also verify the configured session is actually connected,
        # otherwise sends will silently fail with "session not active".
        session_name = (runtime_cfg.get('session_id') or '').strip()
        if not session_name:
            return jsonify({
                'success': False,
                'status_code': status_code,
                'message': 'Gateway reachable, but no OpenWA session is configured. Set the session name.'
            }), 400
        sess = _openwa_session_status(gateway_url, api_key, session_name, timeout_seconds)
        if not sess.get('found'):
            return jsonify({
                'success': False,
                'status_code': status_code,
                'message': f"Gateway reachable, but session '{session_name}' was not found in OpenWA."
            }), 400
        if not sess.get('connected'):
            return jsonify({
                'success': False,
                'status_code': status_code,
                'message': f"Session '{session_name}' is {sess.get('status') or 'disconnected'}. Reconnect it in the OpenWA dashboard (scan QR)."
            }), 400
        return jsonify({
            'success': True,
            'status_code': status_code,
            'message': f"Connected — session '{session_name}' ({sess.get('phone') or 'no number'}) is {sess.get('status')}."
        })

    if ok:
        return jsonify({
            'success': True,
            'status_code': status_code,
            'message': 'Gateway reachable'
        })

    if status_code is not None:
        return jsonify({
            'success': False,
            'status_code': status_code,
            'message': 'Gateway returned non-success status'
        }), 400

    return jsonify({'success': False, 'error': f'Gateway connection failed: {error_reason}'}), 400


@bp.route('/api/whatsapp/auto-configure', methods=['POST'])
@permission_required('secrets.manage')
def auto_configure_whatsapp_gateway():
    from app import (  # deferred: app-level helper, avoids circular import
        WHATSAPP_GATEWAY_URL_KEY, _get_whatsapp_runtime_settings,
        _normalize_whatsapp_gateway_url, _parse_bool, app,
    )
    runtime_cfg = _get_whatsapp_runtime_settings()
    if runtime_cfg.get('deployment_region') == 'iran':
        return jsonify({
            'success': False,
            'error': 'WhatsApp automation is not available when the panel is deployed in Iran.',
            'blocked_reason': 'deployment_in_iran'
        }), 400

    timeout_seconds = int(runtime_cfg.get('gateway_timeout_seconds') or 10)
    api_key = (runtime_cfg.get('gateway_api_key') or '').strip()
    provider = (runtime_cfg.get('provider') or 'baileys').strip().lower()
    configured_url = (runtime_cfg.get('gateway_url') or '').strip()
    host_hint = request.host

    candidates = _build_whatsapp_gateway_candidates(host_hint=host_hint, configured_url=configured_url)
    checked = []
    first_error = None

    for candidate in candidates:
        ok, status_code, error_reason = _probe_whatsapp_gateway(candidate, timeout_seconds=timeout_seconds, api_key=api_key, provider=provider)
        checked.append({
            'url': candidate,
            'ok': bool(ok),
            'status_code': int(status_code) if status_code is not None else None,
            'error': None if ok else (error_reason or 'health_check_failed')
        })
        if ok:
            normalized = _normalize_whatsapp_gateway_url(candidate)
            conf = db.session.get(SystemConfig, WHATSAPP_GATEWAY_URL_KEY)
            if conf:
                conf.value = normalized
            else:
                db.session.add(SystemConfig(key=WHATSAPP_GATEWAY_URL_KEY, value=normalized))
            db.session.commit()
            return jsonify({
                'success': True,
                'gateway_url': normalized,
                'auth_url': f"{normalized}/auth",
                'checked': checked,
            })

        if first_error is None and error_reason:
            first_error = str(error_reason)

    debug_enabled = _parse_bool(request.args.get('debug'))
    response_payload = {
        'success': False,
        'error': 'No WhatsApp gateway service is available yet. Auto setup will retry when you open this section again.',
        'checked': checked,
    }
    if debug_enabled and first_error:
        response_payload['details'] = first_error
    return jsonify(response_payload), 400
