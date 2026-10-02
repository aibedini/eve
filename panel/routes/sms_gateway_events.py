"""Signed, idempotent GMweb SMS evidence ingestion."""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone, time as datetime_time, timedelta

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, or_

from panel.extensions import db, limiter
from panel.models import SmsGatewayEvent, SmsSendLog
from panel.routes.common import permission_required


bp = Blueprint('sms_gateway_events', __name__)
_TOKEN = re.compile(r'^[A-Za-z0-9:_-]+$')
_NOTIFICATION_ID = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,119}$')
_CODE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
_DEVICE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_EVENT_TYPE = re.compile(r'^(?:send\.[a-z][a-z0-9_]{0,58}|gateway\.accepted|sms\.(?:delivered|delivery_failed))$')


def _valid_text(value, maximum, pattern=None):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and (pattern is None or bool(pattern.fullmatch(value))))


def _evidence_grace_seconds():
    """How long a callback-era send may wait before missing evidence is actionable."""
    try:
        value = int(os.environ.get('EVE_SMS_EVIDENCE_GRACE_SECONDS', '300'))
    except (TypeError, ValueError):
        value = 300
    return max(60, min(value, 3600))


def _naive_utc(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _callback_age_seconds(log, now=None):
    created = _naive_utc(getattr(log, 'created_at', None))
    if created is None:
        return None
    now = _naive_utc(now or datetime.utcnow())
    return max(0.0, (now - created).total_seconds())


def _expects_signed_callback(log):
    """Only rows carrying the rollout identity are expected to have signed evidence.

    This deliberately does not use a deployment date. Older SmsSendLog rows may be
    imported or restored later, while eve_notification_id was introduced as part of
    the signed EVE -> GMweb lifecycle contract. Absence therefore means legacy /
    unmeasured rather than callback failure.
    """
    return bool(getattr(log, 'eve_notification_id', None))


def _parse_event(data):
    if not isinstance(data, dict):
        raise ValueError('invalid_event')
    if not _valid_text(data.get('event_id'), 196, _TOKEN):
        raise ValueError('invalid_event_id')
    if not _valid_text(data.get('trace_id'), 64, _TOKEN):
        raise ValueError('invalid_trace_id')
    if not _valid_text(data.get('message_id'), 128, re.compile(r'^send_[0-9]+$')):
        raise ValueError('invalid_message_id')
    if not _valid_text(data.get('type'), 64, _EVENT_TYPE):
        raise ValueError('invalid_event_type')
    notification_id = data.get('eve_notification_id')
    if notification_id is not None and not _valid_text(
            notification_id, 120, _NOTIFICATION_ID):
        raise ValueError('invalid_notification_id')
    request_id = data.get('request_id')
    if request_id is not None and not _valid_text(request_id, 120, _TOKEN):
        raise ValueError('invalid_request_id')
    gateway_request_id = data.get('gateway_request_id')
    if gateway_request_id is not None and not _valid_text(
            gateway_request_id, 120, _TOKEN):
        raise ValueError('invalid_gateway_request_id')
    carrier_status = data.get('carrier_status')
    if carrier_status is not None and carrier_status not in (
            'unavailable', 'pending', 'delivered', 'failed'):
        raise ValueError('invalid_carrier_status')
    expected_carrier = {
        'sms.delivered': 'delivered',
        'sms.delivery_failed': 'failed',
    }.get(data['type'])
    if expected_carrier and carrier_status not in (None, expected_carrier):
        raise ValueError('carrier_status_conflict')
    if expected_carrier:
        carrier_status = expected_carrier
    evidence = data.get('evidence')
    if evidence is not None and not _valid_text(evidence, 64, _CODE):
        raise ValueError('invalid_evidence')
    for name in ('device_id', 'reason_code', 'stage'):
        value = data.get(name)
        pattern = _DEVICE if name == 'device_id' else _CODE
        if value is not None and not _valid_text(value, 64, pattern):
            raise ValueError(f'invalid_{name}')
    attempt = data.get('attempt')
    if attempt is not None and (type(attempt) is not int or not 0 <= attempt <= 100000):
        raise ValueError('invalid_attempt')
    try:
        occurred_at = datetime.fromisoformat(data['occurred_at'].replace('Z', '+00:00'))
        if occurred_at.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError('invalid_occurred_at') from exc
    return dict(
        event_id=data['event_id'], trace_id=data['trace_id'], message_id=data['message_id'],
        eve_notification_id=notification_id, request_id=request_id,
        gateway_request_id=gateway_request_id, carrier_status=carrier_status,
        evidence=evidence, event_type=data['type'],
        occurred_at=occurred_at.astimezone(timezone.utc).replace(tzinfo=None),
        attempt=attempt, device_id=data.get('device_id'),
        reason_code=data.get('reason_code'), stage=data.get('stage'),
    )


def _event_dict(row):
    return dict(
        event_id=row.event_id, trace_id=row.trace_id, message_id=row.message_id,
        request_id=row.request_id, gateway_request_id=row.gateway_request_id,
        eve_notification_id=row.eve_notification_id,
        type=row.event_type, occurred_at=row.occurred_at.isoformat() + 'Z',
        received_at=row.received_at.isoformat() + 'Z',
        carrier_status=row.carrier_status, evidence=row.evidence,
        attempt=row.attempt, device_id=row.device_id,
        reason_code=row.reason_code, stage=row.stage,
    )


def _project_evidence(rows, log=None, now=None):
    """Derive transport submission and carrier truth from immutable evidence.

    Legacy rows are explicitly *unmeasured*: a historical SmsSendLog.status='sent'
    is useful local history, but it is not retroactive proof of a signed GMweb
    callback and it is never proof of carrier delivery.
    """
    callback_expected = _expects_signed_callback(log)
    generation = 'signed_callback' if callback_expected else ('legacy' if log else 'unknown')
    age_seconds = _callback_age_seconds(log, now=now)
    submission = {
        'state': 'unknown', 'confirmed': False, 'evidence': 'not_available',
        'actionable': False, 'generation': generation,
    }
    submission_map = {
        'gateway.accepted': (1, 'queued', False, 'gateway_acceptance'),
        'send.queued': (1, 'queued', False, 'gateway_queue'),
        'send.sent': (3, 'sent', True, 'android_submission'),
        'send.completed': (3, 'sent', True, 'android_submission'),
        'send.failed': (3, 'failed', True, 'gateway_terminal'),
        'send.cancelled': (3, 'cancelled', True, 'gateway_terminal'),
        'send.superseded': (3, 'superseded', True, 'gateway_terminal'),
    }
    submission_events = []
    carrier_events = []
    carrier_pending_events = []
    carrier_unavailable_events = []
    for row in sorted(rows, key=lambda item: (item.occurred_at, item.event_id)):
        mapped = submission_map.get(row.event_type)
        if mapped:
            submission_events.append((mapped[0], row.occurred_at, row.event_id,
                                      mapped[1], mapped[2], mapped[3]))
        carrier_state = row.carrier_status
        if row.event_type == 'sms.delivered':
            carrier_state = 'delivered'
        elif row.event_type == 'sms.delivery_failed':
            carrier_state = 'failed'
        if carrier_state in ('delivered', 'failed'):
            carrier_events.append((row.occurred_at, row.event_id, carrier_state,
                                   row.evidence or 'carrier_dlr'))
        elif carrier_state == 'pending':
            carrier_pending_events.append((row.occurred_at, row.event_id))
        elif carrier_state == 'unavailable':
            carrier_unavailable_events.append((row.occurred_at, row.event_id))

    if submission_events:
        latest = max(submission_events, key=lambda item: (item[0], item[1], item[2]))
        submission = dict(
            state=latest[3], confirmed=latest[4], evidence=latest[5],
            actionable=False, generation='signed_callback')
    elif log is not None and not callback_expected:
        submission = dict(
            state='legacy_record', confirmed=False,
            evidence='historical_evidence_unavailable', actionable=False,
            generation='legacy')
    elif callback_expected and age_seconds is not None and age_seconds <= _evidence_grace_seconds():
        submission = dict(
            state='pending', confirmed=False, evidence='callback_pending',
            actionable=False, generation='signed_callback')
    elif callback_expected:
        submission = dict(
            state='callback_missing', confirmed=False, evidence='callback_missing',
            actionable=True, generation='signed_callback')

    if carrier_events:
        # A positive receipt is stronger than a failure report and cannot be
        # downgraded by delayed/conflicting weaker evidence.
        latest = max(carrier_events, key=lambda item: (
            1 if item[2] == 'delivered' else 0, item[0], item[1]))
        carrier = {
            'state': latest[2], 'confirmed': True, 'evidence': latest[3],
            'actionable': latest[2] == 'failed', 'generation': 'signed_callback',
        }
    else:
        polled_state = str(getattr(log, 'carrier_state', '') or '').lower()
        if polled_state in ('delivered', 'failed'):
            carrier = {
                'state': polled_state,
                'confirmed': True,
                'evidence': getattr(log, 'carrier_evidence', None) or 'carrier_dlr',
                'actionable': polled_state == 'failed',
                'generation': generation,
            }
        elif polled_state == 'pending' or carrier_pending_events:
            carrier = {
                'state': 'pending', 'confirmed': False,
                'evidence': 'awaiting_carrier_receipt', 'actionable': False,
                'generation': generation,
            }
        elif log is not None and not callback_expected:
            carrier = {
                'state': 'historical_unavailable', 'confirmed': False,
                'evidence': 'historical_evidence_unavailable', 'actionable': False,
                'generation': 'legacy',
            }
        else:
            # send.sent proves Android/modem submission, not carrier delivery.
            # Unless GMweb/Android explicitly reports a pending DLR, absence of a
            # receipt is "not exposed", not a yellow "pending" delivery promise.
            carrier = {
                'state': 'not_exposed', 'confirmed': False,
                'evidence': ('carrier_not_exposed' if carrier_unavailable_events or callback_expected
                             else 'not_available'),
                'actionable': False, 'generation': generation,
            }
    return submission, carrier


def _timeline_clauses(log):
    """Return every safe identity that can correlate an EVE log to a GMweb event.

    Older code picked exactly one field with if/elif. A populated but non-matching
    request_id therefore prevented a valid eve_notification_id/correlation_id from
    ever being tried, producing a false "no signed callback". Correlation is now a
    bounded OR over all identifiers; it never falls back to phone number or text.
    """
    clauses = []
    if log.request_id:
        clauses.extend((SmsGatewayEvent.message_id == log.request_id,
                        SmsGatewayEvent.request_id == log.request_id))
    if log.correlation_id:
        clauses.append(SmsGatewayEvent.trace_id == log.correlation_id)
    if log.eve_notification_id:
        clauses.append(SmsGatewayEvent.eve_notification_id == log.eve_notification_id)
    if log.gateway_request_id:
        clauses.append(SmsGatewayEvent.gateway_request_id == log.gateway_request_id)
    return clauses


@bp.route('/internal/gmweb/sms/events', methods=['POST'])
@limiter.exempt
def ingest_sms_event():
    secret = os.environ.get('EVE_SMS_EVENTS_SECRET', '')
    if len(secret) < 32:
        return jsonify({'error': 'callback_not_configured'}), 503
    raw = request.get_data(cache=True)
    if not raw or len(raw) > 4096:
        return jsonify({'error': 'invalid_body'}), 400
    timestamp = request.headers.get('X-GMweb-Timestamp', '')
    delivery_id = request.headers.get('X-GMweb-Delivery-Id', '')
    signature = request.headers.get('X-GMweb-Signature', '')
    if not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12:
        return jsonify({'error': 'invalid_signature'}), 401
    if not _valid_text(delivery_id, 196, _TOKEN):
        return jsonify({'error': 'invalid_signature'}), 401
    if abs(int(time.time()) - int(timestamp)) > 300:
        return jsonify({'error': 'stale_signature'}), 401
    digest = hmac.new(secret.encode('utf-8'),
                      timestamp.encode('ascii') + b'.' + delivery_id.encode('ascii') + b'.' + raw,
                      hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, f'sha256={digest}'):
        return jsonify({'error': 'invalid_signature'}), 401
    try:
        values = _parse_event(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        return jsonify({'error': 'invalid_event'}), 400
    if delivery_id != values['event_id']:
        return jsonify({'error': 'delivery_id_mismatch'}), 400
    existing = db.session.get(SmsGatewayEvent, values['event_id'])
    if existing:
        if any(getattr(existing, key) != value for key, value in values.items()):
            return jsonify({'error': 'event_id_conflict'}), 409
        return jsonify({'accepted': True, 'duplicate': True})
    try:
        db.session.add(SmsGatewayEvent(**values))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = db.session.get(SmsGatewayEvent, values['event_id'])
        if existing and all(getattr(existing, key) == value for key, value in values.items()):
            return jsonify({'accepted': True, 'duplicate': True})
        return jsonify({'error': 'event_id_conflict'}), 409
    return jsonify({'accepted': True, 'duplicate': False})


@bp.route('/api/sms/gateway-events', methods=['GET'])
@permission_required('secrets.manage')
def sms_gateway_events():
    trace_id = request.args.get('trace_id', '')
    if not _valid_text(trace_id, 64, _TOKEN):
        return jsonify({'error': 'trace_id_required'}), 400
    rows = (SmsGatewayEvent.query.filter_by(trace_id=trace_id)
            .order_by(SmsGatewayEvent.occurred_at, SmsGatewayEvent.event_id)
            .limit(500).all())
    response = jsonify({'events': [_event_dict(row) for row in rows]})
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/sms/overview', methods=['GET'])
@permission_required('secrets.manage')
def sms_transport_overview():
    """Count only evidence actually ingested today; never infer carrier DLR."""
    start = datetime.combine(datetime.utcnow().date(), datetime_time.min)
    counts = dict(db.session.query(SmsGatewayEvent.event_type,
                                   func.count(func.distinct(SmsGatewayEvent.message_id)))
                  .filter(SmsGatewayEvent.occurred_at >= start)
                  .group_by(SmsGatewayEvent.event_type).all())
    terminal_message_ids = (db.session.query(SmsGatewayEvent.message_id)
                            .filter(SmsGatewayEvent.occurred_at >= start)
                            .filter(SmsGatewayEvent.event_type.in_(
                                ('sms.delivered', 'sms.delivery_failed'))))
    carrier_pending = (db.session.query(
        func.count(func.distinct(SmsGatewayEvent.message_id)))
        .filter(SmsGatewayEvent.occurred_at >= start,
                SmsGatewayEvent.carrier_status == 'pending',
                ~SmsGatewayEvent.message_id.in_(terminal_message_ids))
        .scalar() or 0)
    sent_message_ids = (db.session.query(SmsGatewayEvent.message_id)
                        .filter(SmsGatewayEvent.occurred_at >= start,
                                SmsGatewayEvent.event_type == 'send.sent'))
    unavailable = (db.session.query(func.count(func.distinct(SmsGatewayEvent.message_id)))
                   .filter(SmsGatewayEvent.occurred_at >= start,
                           SmsGatewayEvent.message_id.in_(sent_message_ids),
                           ~SmsGatewayEvent.message_id.in_(terminal_message_ids),
                           SmsGatewayEvent.carrier_status == 'unavailable')
                   .scalar() or 0)
    response = jsonify({
        'window_start': start.isoformat() + 'Z',
        'gateway_accepted': counts.get('gateway.accepted', 0),
        'sent': counts.get('send.sent', 0),
        'carrier_delivered': counts.get('sms.delivered', 0),
        'carrier_failed': counts.get('sms.delivery_failed', 0),
        'carrier_pending': carrier_pending,
        'carrier_not_exposed': unavailable,
        'evidence_available': bool(counts),
        'scope': 'signed_callback_events_only',
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/sms/evidence-health', methods=['GET'])
@permission_required('secrets.manage')
def sms_evidence_health():
    """Read-only local audit: are callback-era EVE sends gaining signed evidence?"""
    now = datetime.utcnow()
    grace = _evidence_grace_seconds()
    since = now - timedelta(hours=24)
    cutoff = now - timedelta(seconds=grace)
    recent = (SmsSendLog.query
              .filter(SmsSendLog.created_at >= since,
                      SmsSendLog.created_at <= cutoff,
                      SmsSendLog.eve_notification_id.isnot(None))
              .order_by(SmsSendLog.created_at.desc())
              .limit(500).all())
    notification_ids = [row.eve_notification_id for row in recent if row.eve_notification_id]
    seen = set()
    if notification_ids:
        seen = {value for (value,) in
                (db.session.query(SmsGatewayEvent.eve_notification_id)
                 .filter(SmsGatewayEvent.eve_notification_id.in_(notification_ids))
                 .distinct().all()) if value}
    missing = [row for row in recent if row.eve_notification_id not in seen]
    last_received = db.session.query(func.max(SmsGatewayEvent.received_at)).scalar()
    configured = len(os.environ.get('EVE_SMS_EVENTS_SECRET', '')) >= 32
    if not configured:
        state = 'not_configured'
    elif missing:
        state = 'degraded'
    elif recent:
        state = 'healthy'
    else:
        state = 'idle'
    response = jsonify({
        'state': state,
        'configured': configured,
        'grace_seconds': grace,
        'window_hours': 24,
        'expected_after_grace': len(recent),
        'with_signed_callback': len(recent) - len(missing),
        'missing_after_grace': len(missing),
        'missing_log_ids': [row.id for row in missing[:25]],
        'last_callback_received_at': (
            last_received.isoformat() + 'Z' if last_received else None),
        'legacy_rows_are_excluded': True,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/sms/messages/<int:log_id>/timeline', methods=['GET'])
@permission_required('secrets.manage')
def sms_message_timeline(log_id):
    log = db.session.get(SmsSendLog, log_id)
    if log is None:
        return jsonify({'error': 'not_found'}), 404
    clauses = _timeline_clauses(log)
    query = SmsGatewayEvent.query
    if clauses:
        query = query.filter(or_(*clauses))
    else:
        query = query.filter(SmsGatewayEvent.event_id == '')
    rows = query.order_by(SmsGatewayEvent.occurred_at, SmsGatewayEvent.event_id).limit(500).all()
    submission, carrier = _project_evidence(rows, log)
    response = jsonify({
        'log': log.to_dict(),
        'gateway_events': [_event_dict(row) for row in rows],
        'submission': submission,
        'carrier': carrier,
        'delivery_confirmed': (True if carrier['state'] == 'delivered'
                               else False if carrier['state'] == 'failed' else None),
        'delivery_evidence': carrier['evidence'],
    })
    response.headers['Cache-Control'] = 'no-store'
    return response
