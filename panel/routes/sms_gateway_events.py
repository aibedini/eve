"""Signed, idempotent GMweb SMS evidence ingestion."""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone, time as datetime_time

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from panel.extensions import db, limiter
from panel.models import SmsGatewayEvent, SmsSendLog
from panel.routes.common import permission_required


bp = Blueprint('sms_gateway_events', __name__)
_TOKEN = re.compile(r'^[A-Za-z0-9:_-]+$')
_NOTIFICATION_ID = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,119}$')
_CODE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
_DEVICE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_EVENT_TYPE = re.compile(r'^(?:send\.[a-z][a-z0-9_]{0,58}|gateway\.accepted|sms\.(?:delivered|delivery_failed))$')

# Counters and indices are non-negative ...
_UNSIGNED_DIAGNOSTICS = ('schema_version', 'segment_index', 'segment_count',
                         'eve_dispatch_attempts')
_MAX_DIAGNOSTIC = 1000000

# ... but carrier_result_code is the raw Android callback result code, which is
# SIGNED: a successful PendingIntent delivery callback reports Activity.RESULT_OK
# (== -1). Validated with the counters above it was rejected as
# invalid_carrier_result_code, EVE answered HTTP 400, GMweb moved the callback to
# dead_letter and the carrier state stayed pending for ever even though the phone
# held a real receipt.
_MIN_CARRIER_RESULT_CODE = -1000000

# Display-only projection window: after this many seconds a message that was
# definitely submitted and still has no terminal carrier receipt stops reading as
# "waiting for the carrier" and reads as "unconfirmed". 0 disables the derived
# state (everything stays `pending`). Override with
# SMS_DLR_PENDING_TIMEOUT_SECONDS.
DEFAULT_DLR_PENDING_TIMEOUT_SECONDS = 900


def _valid_text(value, maximum, pattern=None):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and (pattern is None or bool(pattern.fullmatch(value))))


def _dlr_pending_timeout_seconds():
    """Configured wait window for a terminal carrier receipt; 0 disables it."""
    raw = os.environ.get('SMS_DLR_PENDING_TIMEOUT_SECONDS')
    if raw is None or not str(raw).strip():
        return DEFAULT_DLR_PENDING_TIMEOUT_SECONDS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DLR_PENDING_TIMEOUT_SECONDS
    return max(0, value)


def _parse_utc(value):
    """ISO-8601 (with Z or an offset) -> naive UTC datetime, or None."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp


def _parse_delivery_diagnostics(data):
    """Keep only bounded delivery metadata; never retain body, recipient or secrets."""
    fields = {}
    for name in _UNSIGNED_DIAGNOSTICS:
        value = data.get(name)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_DIAGNOSTIC:
                raise ValueError(f'invalid_{name}')
            fields[name] = value
    # Signed on purpose — see _MIN_CARRIER_RESULT_CODE above. The accepted range
    # must match GMweb's (gatewayRoutes.js: minimum -1000000, maximum 1000000)
    # and Android's raw BroadcastReceiver result code.
    value = data.get('carrier_result_code')
    if value is not None:
        if (isinstance(value, bool) or not isinstance(value, int)
                or not _MIN_CARRIER_RESULT_CODE <= value <= _MAX_DIAGNOSTIC):
            raise ValueError('invalid_carrier_result_code')
        fields['carrier_result_code'] = value
    for name in ('carrier_result', 'last_error'):
        value = data.get(name)
        if value is not None:
            if not _valid_text(value, 120, _CODE):
                raise ValueError(f'invalid_{name}')
            fields[name] = value
    for name in ('android_delivery_received_at', 'gmweb_delivery_received_at',
                 'eve_last_attempt_at', 'eve_ack_at'):
        value = data.get(name)
        if value is not None:
            try:
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f'invalid_{name}') from exc
            fields[name] = stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    value = data.get('all_segments_delivered')
    if value is not None:
        if not isinstance(value, bool):
            raise ValueError('invalid_all_segments_delivered')
        fields['all_segments_delivered'] = value
    return json.dumps(fields, sort_keys=True, separators=(',', ':')) if fields else None


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
        diagnostics_json=_parse_delivery_diagnostics(data),
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
        diagnostics=json.loads(row.diagnostics_json) if row.diagnostics_json else {},
    )


def _project_evidence(rows, log=None, now=None):
    """Derive transport submission and carrier truth from immutable evidence.

    `now` is injectable so the derived `unconfirmed` window is testable without
    waiting (and without depending on the wall clock).
    """
    submission = {'state': 'unknown', 'confirmed': False, 'evidence': 'not_available',
                  'occurred_at': None, 'received_at': None}
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
    for row in sorted(rows, key=lambda item: (item.occurred_at, item.event_id)):
        mapped = submission_map.get(row.event_type)
        if mapped:
            submission_events.append((mapped[0], row.occurred_at, row.event_id,
                                      mapped[1], mapped[2], mapped[3],
                                      getattr(row, 'received_at', row.occurred_at)))
        carrier_state = row.carrier_status
        if row.event_type == 'sms.delivered':
            carrier_state = 'delivered'
        elif row.event_type == 'sms.delivery_failed':
            carrier_state = 'failed'
        raw_diagnostics = getattr(row, 'diagnostics_json', None)
        diagnostics = json.loads(raw_diagnostics) if raw_diagnostics else {}
        if carrier_state == 'delivered' and diagnostics.get('all_segments_delivered') is False:
            carrier_state = 'pending'
        if carrier_state in ('delivered', 'failed'):
            carrier_events.append((row.occurred_at, row.event_id, carrier_state,
                                   row.evidence or 'carrier_dlr',
                                   getattr(row, 'received_at', row.occurred_at),
                                   getattr(row, 'reason_code', None), diagnostics))
    if submission_events:
        latest = max(submission_events, key=lambda item: (item[0], item[1], item[2]))
        submission = dict(state=latest[3], confirmed=latest[4], evidence=latest[5],
                          occurred_at=latest[1].isoformat() + 'Z',
                          received_at=latest[6].isoformat() + 'Z')
    if carrier_events:
        # A positive receipt is stronger than a failure report and cannot be
        # downgraded by delayed/conflicting weaker evidence.
        latest = max(carrier_events, key=lambda item: (
            1 if item[2] == 'delivered' else 0, item[0], item[1]))
        carrier = {'state': latest[2], 'confirmed': True, 'evidence': latest[3],
                   'occurred_at': latest[0].isoformat() + 'Z',
                   'received_at': latest[4].isoformat() + 'Z',
                   'event_id': latest[1], 'reason_code': latest[5],
                   'diagnostics': latest[6]}
    else:
        polled_state = str(getattr(log, 'carrier_state', '') or '').lower()
        if polled_state in ('delivered', 'failed'):
            carrier = {
                'state': polled_state,
                'confirmed': True,
                'evidence': getattr(log, 'carrier_evidence', None) or 'carrier_dlr',
                'occurred_at': getattr(log, 'carrier_occurred_at', None),
            }
        elif (polled_state == 'unavailable' and getattr(log, 'carrier_evidence', None)
              in ('device_unsupported', 'carrier_unsupported', 'dlr_disabled')):
            carrier = {'state': 'unavailable', 'confirmed': False,
                       'evidence': log.carrier_evidence}
        elif polled_state == 'pending' or submission['state'] == 'sent' or getattr(log, 'status', None) == 'sent':
            carrier = {'state': 'pending', 'confirmed': False,
                       'evidence': 'awaiting_carrier_receipt'}
        else:
            carrier = {'state': 'unknown', 'confirmed': False,
                       'evidence': 'not_reported'}
    if (getattr(log, 'carrier_state', None) == 'delivered'
            and carrier['state'] != 'delivered'):
        carrier = {'state': 'delivered', 'confirmed': True,
                   'evidence': getattr(log, 'carrier_evidence', None) or 'carrier_dlr',
                   'occurred_at': getattr(log, 'carrier_occurred_at', None)}
    # Derived, display-only: a message that WAS submitted and still has no
    # terminal receipt after the wait window is neither delivered nor failed nor
    # unavailable — the evidence simply never arrived. This never writes to the
    # immutable evidence tables and never invents a receipt.
    if carrier['state'] == 'pending' and submission['state'] == 'sent':
        timeout = _dlr_pending_timeout_seconds()
        submitted_at = _parse_utc(submission.get('occurred_at'))
        if timeout > 0 and submitted_at is not None:
            reference = now or datetime.now(timezone.utc).replace(tzinfo=None)
            if reference - submitted_at >= timedelta(seconds=timeout):
                carrier = dict(carrier, state='unconfirmed',
                               evidence='no_carrier_receipt_within_window',
                               timeout_seconds=timeout)
    return submission, carrier


def _events_for_logs(logs):
    """Read the latest 500 events per message; a busy message cannot starve another."""
    result = {}
    for log in logs:
        if log.request_id:
            query = SmsGatewayEvent.query.filter_by(message_id=log.request_id)
        elif log.correlation_id:
            query = SmsGatewayEvent.query.filter_by(trace_id=log.correlation_id)
        elif log.eve_notification_id:
            query = SmsGatewayEvent.query.filter_by(eve_notification_id=log.eve_notification_id)
        else:
            result[log.id] = []
            continue
        matched = (query.order_by(SmsGatewayEvent.occurred_at.desc(), SmsGatewayEvent.event_id.desc())
                   .limit(500).all())
        terminal = (query.filter(SmsGatewayEvent.event_type.in_(('sms.delivered', 'sms.delivery_failed')))
                    .order_by(SmsGatewayEvent.occurred_at.desc(), SmsGatewayEvent.event_id.desc())
                    .limit(500).all())
        matched = list({row.event_id: row for row in matched + terminal}.values())
        matched.sort(key=lambda row: (row.occurred_at, row.event_id), reverse=True)
        result[log.id] = list(reversed(matched))
    return result


def _delivery_pipeline(log, rows, submission, carrier):
    """Only confirm a stage when its own EVE-side evidence is present."""
    sent = next((row for row in reversed(rows) if row.event_type in ('send.sent', 'send.completed')), None)
    receipt = next((row for row in reversed(rows)
                    if row.event_id == carrier.get('event_id')), None)
    diagnostics = carrier.get('diagnostics') or {}
    waiting = carrier['state'] == 'pending'
    # The same missing-evidence fact, read one window apart: still inside the
    # carrier wait window ("waiting") or past it ("unconfirmed").
    awaiting = 'unconfirmed' if carrier['state'] == 'unconfirmed' else (
        'waiting' if waiting else 'not_reported')
    accepted = next((row for row in rows if row.event_type == 'gateway.accepted'), None)
    queued = [row for row in rows if row.event_type == 'send.queued']
    job_created = min(([accepted] if accepted is not None else []) + queued,
                      key=lambda row: (row.occurred_at, row.event_id), default=None)
    # This stage used to be hardcoded to not_reported, so a message could show
    # "SmsManager submission: confirmed" directly above "Android accepted job:
    # not reported" — Android cannot have handed the parts to SmsManager without
    # accepting the task. Prefer an explicit acceptance event; otherwise say
    # plainly that the stage is implied by the submission, and never invent a
    # timestamp that is not the submission's own.
    if accepted is not None:
        android_state, android_at = 'confirmed', accepted.occurred_at.isoformat() + 'Z'
        android_evidence = 'gateway_acceptance'
    elif submission['state'] == 'sent':
        android_state, android_at = 'confirmed', submission.get('occurred_at')
        android_evidence = 'implied_by_android_submission'
    else:
        android_state = 'waiting' if log.request_id else 'not_reported'
        android_at, android_evidence = None, None

    def stage(name, state, at=None, evidence=None, reason=None):
        return {'name': name, 'state': state, 'at': at, 'evidence': evidence,
                'reason': reason}

    return [
        stage('EVE request created', 'confirmed',
              log.created_at.isoformat() + 'Z' if log.created_at else None, 'sms_send_log'),
        stage('GMweb job created', 'confirmed' if log.gateway_job_id else 'not_reported',
              job_created.occurred_at.isoformat() + 'Z' if job_created is not None else None,
              'gateway_job_id' if log.gateway_job_id else None),
        stage('Android accepted job', android_state, android_at, android_evidence),
        stage('SmsManager submission', 'confirmed' if submission['state'] == 'sent' else
              'failed' if submission['state'] == 'failed' else 'waiting' if log.request_id else 'not_reported',
              submission.get('occurred_at'), submission.get('evidence')),
        stage('SENT callback ingested', 'confirmed' if sent else 'waiting' if log.request_id else 'not_reported',
              sent.received_at.isoformat() + 'Z' if sent else None,
              sent.event_id if sent else None),
        stage('Carrier DELIVERY callback', 'failed' if receipt and carrier['state'] == 'failed' else
              'confirmed' if receipt else awaiting,
              diagnostics.get('android_delivery_received_at') or carrier.get('occurred_at') if receipt else None,
              carrier.get('evidence') if receipt else None,
              carrier.get('reason_code') or
              ('carrier_receipt_timeout' if carrier['state'] == 'unconfirmed' else None)),
        stage('GMweb delivery report received',
              'confirmed' if diagnostics.get('gmweb_delivery_received_at') else awaiting,
              diagnostics.get('gmweb_delivery_received_at'),
              carrier.get('event_id') if diagnostics.get('gmweb_delivery_received_at') else None),
        stage('EVE delivery receipt ingested', 'confirmed' if receipt else awaiting,
              carrier.get('received_at') if receipt else None,
              carrier.get('event_id') if receipt else None),
    ]


def _delivery_view(log, rows, now=None):
    submission, carrier = _project_evidence(rows, log, now=now)
    return {'submission': submission, 'carrier': carrier,
            'pipeline': _delivery_pipeline(log, rows, submission, carrier)}


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
    """Count gateway events in the current Tehran day; no inferred deliveries."""
    tehran_offset = timedelta(hours=3, minutes=30)
    local_date = (datetime.utcnow() + tehran_offset).date()
    start = datetime.combine(local_date, datetime_time.min) - tehran_offset
    end = start + timedelta(days=1)
    counts = dict(db.session.query(SmsGatewayEvent.event_type,
                                   func.count(func.distinct(SmsGatewayEvent.message_id)))
                  .filter(SmsGatewayEvent.occurred_at >= start,
                          SmsGatewayEvent.occurred_at < end)
                  .group_by(SmsGatewayEvent.event_type).all())
    terminal_message_ids = (db.session.query(SmsGatewayEvent.message_id)
          .filter(SmsGatewayEvent.occurred_at >= start,
                  SmsGatewayEvent.occurred_at < end)
                            .filter(SmsGatewayEvent.event_type.in_(
                                ('sms.delivered', 'sms.delivery_failed'))))
    carrier_pending = (db.session.query(
        func.count(func.distinct(SmsGatewayEvent.message_id)))
        .filter(SmsGatewayEvent.occurred_at >= start,
                SmsGatewayEvent.occurred_at < end,
                SmsGatewayEvent.event_type == 'send.sent',
                ~SmsGatewayEvent.message_id.in_(terminal_message_ids))
        .scalar() or 0)
    response = jsonify({
        'window_start': start.isoformat() + 'Z',
        'gateway_accepted': counts.get('gateway.accepted', 0),
        'sent': counts.get('send.sent', 0),
        'carrier_delivered': counts.get('sms.delivered', 0),
        'carrier_failed': counts.get('sms.delivery_failed', 0),
        'carrier_pending': carrier_pending,
        'evidence_available': bool(counts),
        'scope': 'signed_callback_events_only',
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/sms/messages/<int:log_id>/timeline', methods=['GET'])
@permission_required('secrets.manage')
def sms_message_timeline(log_id):
    log = db.session.get(SmsSendLog, log_id)
    if log is None:
        return jsonify({'error': 'not_found'}), 404
    rows = _events_for_logs([log])[log.id]
    delivery = _delivery_view(log, rows)
    submission, carrier = delivery['submission'], delivery['carrier']
    log_view = log.to_dict()
    log_view['carrier_state'] = carrier['state']
    log_view['carrier_evidence'] = carrier['evidence']
    response = jsonify({
        'log': log_view,
        'gateway_events': [_event_dict(row) for row in rows],
        'submission': submission,
        'carrier': carrier,
        'pipeline': delivery['pipeline'],
        'delivery_confirmed': (True if carrier['state'] == 'delivered'
                               else False if carrier['state'] == 'failed' else None),
        'delivery_evidence': carrier['evidence'],
    })
    response.headers['Cache-Control'] = 'no-store'
    return response
