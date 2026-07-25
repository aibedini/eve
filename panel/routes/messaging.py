"""SMS and WhatsApp gateway API routes (extracted from app.py)."""
import os
import requests
import threading
import time
import uuid

from flask import Blueprint, jsonify, request, session

from panel.core.phone import _extract_iran_mobile_from_text
from panel.core.redis_client import get_redis
from panel.extensions import db
from panel.models import Admin, PendingSms, SmsSendLog, SystemConfig
from panel.routes.common import superadmin_required


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
            verify=False,
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
                'priority': 'high',
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
@superadmin_required
def test_sms_connection():
    """Verify the GMweb gateway: /health (public) then /ready (with the token)."""
    from app import (  # deferred: app-level helper, avoids circular import
        _get_sms_runtime_settings, app,
    )
    cfg = _get_sms_runtime_settings()
    base = (cfg.get('base_url') or '').strip().rstrip('/')
    api_key = (cfg.get('api_key') or '').strip()
    if not base:
        return jsonify({'success': False, 'error': 'GMweb Base URL is not configured.'}), 400
    if not api_key:
        return jsonify({'success': False, 'error': 'GMweb API key is not configured.'}), 400
    timeout = int(cfg.get('timeout_seconds') or 15)
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
                        'message': 'Gateway reachable and key valid, but Google Messages is not paired yet (503).'})
    if r.status_code == 200:
        return jsonify({'success': True, 'ready': True, 'message': 'Gateway reachable, key valid, and ready to send.'})
    return jsonify({'success': False, 'error': f'Gateway /ready returned HTTP {r.status_code}.'}), 400


@bp.route('/api/sms/test-send', methods=['POST'])
@superadmin_required
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
                        'error': 'GMweb gateway is not configured. Set the Base URL and API key (and Save) first.'}), 400

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
    slot_ok, slot_reason = _sms_take_send_slot(recipient, cfg, segments)
    if not slot_ok:
        _sms_log_row(None, (getattr(user, 'username', None) or 'test').strip().lower(),
                     0, 'Eve', 'test', recipient, 'skipped', slot_reason, segment_info)
        return jsonify({'success': False, 'recipient': recipient,
                        'error': 'Daily SMS segment limit reached.'}), 429
    res = _send_sms_via_gmweb(recipient, text, cfg, priority='high',
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
    _sms_refund_daily_segments(segments)
    _sms_log_row(None, (getattr(user, 'username', None) or 'test').strip().lower(),
                 0, 'Eve', 'test', recipient, 'failed', res.get('reason'), res)
    return jsonify({'success': False, 'recipient': recipient,
                    'error': f'Send failed ({res.get("reason") or "unknown"}).',
                    'status_code': res.get('status_code')}), 400


@bp.route('/api/sms/scan/run', methods=['POST'])
@superadmin_required
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
        return jsonify({'success': False, 'error': 'GMweb gateway is not configured.'}), 400
    if not requested_states and not any(cfg.get(f'trigger_{s}') for s in SMS_SCAN_STATES):
        return jsonify({'success': False, 'error': 'No state triggers are enabled (near expiry / low volume / expired / ended).'}), 400
    if payload.get('states') is not None and not requested_states:
        return jsonify({'success': False, 'error': 'Select at least one reminder state to start.'}), 400
    ready, ready_reason, ready_status = _sms_gateway_ready(cfg)
    if not ready:
        if ready_reason == 'gateway_not_paired':
            message = 'GMweb is reachable but Google Messages is not paired/ready. Pair it first, then start again.'
        elif ready_reason == 'gateway_auth_failed':
            message = 'GMweb rejected the API key (401). Check the project API key.'
        else:
            message = f'GMweb is not ready: {ready_reason or "unknown"}'
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


@bp.route('/api/sms/scan/status', methods=['GET'])
@superadmin_required
def sms_scan_status():
    """Live progress of the current/last SMS scan (shared across all workers)."""
    from app import (  # deferred: app-level helper, avoids circular import
        _get_sms_runtime_settings, _sms_db_segment_stats_today,
        _sms_scan_snapshot, app,
    )
    try:
        pending_high = PendingSms.query.count()
    except Exception:
        pending_high = 0
    segment_stats = _sms_db_segment_stats_today()
    return jsonify({
        'success': True,
        'job': _sms_scan_snapshot(),
        'pending_high': pending_high,
        'segments_used_today': segment_stats.get('completed', 0),
        'segments_completed_today': segment_stats.get('completed', 0),
        'segments_submitted_today': segment_stats.get('submitted', 0),
        'segments_failed_today': segment_stats.get('failed', 0),
        'segments_inflight_today': segment_stats.get('inflight', 0),
        'segment_daily_limit': int(_get_sms_runtime_settings().get('daily_limit') or 200),
    })


@bp.route('/api/sms/queue/promote-high', methods=['POST'])
@superadmin_required
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
@superadmin_required
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
@superadmin_required
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
    q = SmsSendLog.query
    if status_filter in ('sent', 'failed', 'skipped', 'cancelled'):
        q = q.filter(SmsSendLog.status == status_filter)
    if state_filter in ('near_expiry', 'low_volume', 'expired', 'ended'):
        q = q.filter(SmsSendLog.state == state_filter)
    total = q.count()
    rows = q.order_by(SmsSendLog.created_at.desc()).offset(offset).limit(limit).all()
    resp = jsonify({'success': True, 'logs': [r.to_dict() for r in rows],
                    'total': total, 'offset': offset, 'limit': limit})
    # Never let a proxy/browser serve a stale log — it must always reflect now.
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@bp.route('/api/whatsapp/test-connection', methods=['POST'])
@superadmin_required
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
@superadmin_required
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
