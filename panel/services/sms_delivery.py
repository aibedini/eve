"""Shared gateway submission and carrier evidence for renewal and SMS views."""

import json
import os
from datetime import datetime, timedelta, timezone

from panel.models import SmsGatewayEvent

DEFAULT_DLR_PENDING_TIMEOUT_SECONDS = 900


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
              == 'no_carrier_receipt_within_window'):
            carrier = {'state': 'unconfirmed', 'confirmed': False,
                       'evidence': 'no_carrier_receipt_within_window'}
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
    """Read bounded evidence for all messages in one query, including receipts."""
    from sqlalchemy import and_, func, or_
    from sqlalchemy.orm import aliased
    from panel.extensions import db
    from panel.models import SmsSendLog

    result = {log.id: [] for log in logs}
    if not result:
        return result
    match = or_(
        and_(SmsSendLog.request_id.isnot(None), SmsGatewayEvent.message_id == SmsSendLog.request_id),
        and_(SmsSendLog.request_id.is_(None), SmsSendLog.correlation_id.isnot(None),
             SmsGatewayEvent.trace_id == SmsSendLog.correlation_id),
        and_(SmsSendLog.request_id.is_(None), SmsSendLog.correlation_id.is_(None),
             SmsGatewayEvent.eve_notification_id == SmsSendLog.eve_notification_id))
    receipt = SmsGatewayEvent.event_type.in_(('sms.delivered', 'sms.delivery_failed'))
    order = (SmsGatewayEvent.occurred_at.desc(), SmsGatewayEvent.event_id.desc())
    ranked = (db.session.query(
        SmsGatewayEvent, SmsSendLog.id.label('log_id'),
        func.row_number().over(partition_by=SmsSendLog.id, order_by=order).label('recent_rank'),
        func.row_number().over(partition_by=(SmsSendLog.id, receipt), order_by=order).label('kind_rank'))
        .join(SmsSendLog, match).filter(SmsSendLog.id.in_(result)).subquery())
    event = aliased(SmsGatewayEvent, ranked)
    rows = (db.session.query(event, ranked.c.log_id)
            .filter(or_(ranked.c.recent_rank <= 500,
                        and_(event.event_type.in_(('sms.delivered', 'sms.delivery_failed')),
                             ranked.c.kind_rank <= 500)))
            .order_by(event.occurred_at, event.event_id).all())
    for row, log_id in rows:
        result[log_id].append(row)
    return result
