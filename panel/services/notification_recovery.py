"""Operator-reviewed recovery of historical terminal notification gaps.

Preview only reads the ledger. Activation inserts outbox obligations; it never
submits SMS. An unknown state age requires an explicit custom-window selection.
"""
from datetime import date, datetime, timezone
import hashlib

from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import (OPEN_NOTIFICATION_STATUSES, ServiceNotificationEvent,
                          ServiceObservedState, SmsSendLog)
from panel.services import lifecycle, telemetry_state


TERMINAL_STATES = ('volume_ended', 'expired')
SUPPRESSION_REASONS = frozenset((
    'no_identity_email', 'reseller_owned', 'opted_out_recheck', 'no_recipient',
    'unlimited_skipped', 'expired_too_old', 'ended_too_old', 'no_template',
    'empty_message',
))
WINDOW_DAYS = {'24h': 1, '3d': 3, '7d': 7}


def _assess(row, *, now, window, allow_unknown_age=False,
            date_from=None, date_to=None):
    generation = lifecycle.generation_state(row.service_key).get('generation')
    generation = int(generation or 0)
    kind = telemetry_state.SERVICE_STATE_TO_NOTIFICATION_KIND.get(row.last_state)
    event = (ServiceNotificationEvent.query
             .filter_by(service_key=row.service_key, notification_kind=kind,
                        lifecycle_generation=generation)
             .order_by(ServiceNotificationEvent.id.desc()).first())
    sms_state = telemetry_state.sms_state_for(row.last_state)
    logs = SmsSendLog.query.filter_by(
        service_key=row.service_key, lifecycle_generation=generation,
        state=sms_state)
    confirmed = logs.filter_by(verification_status='confirmed', successful=True).first() is not None
    submitted = logs.filter(SmsSendLog.request_id.isnot(None)).first() is not None
    if confirmed:
        reason = 'confirmed'
    elif event and event.status in OPEN_NOTIFICATION_STATUSES:
        reason = 'active_obligation'
    elif event and event.status == 'skipped' and event.last_error in SUPPRESSION_REASONS:
        reason = 'terminal_suppression'
    elif event and event.status == 'sent':
        # Older builds closed on gateway acceptance. Do not risk a duplicate.
        reason = 'legacy_submission_needs_review'
    elif event and event.gateway_request_id:
        reason = 'gateway_request_needs_review'
    elif submitted:
        reason = 'gateway_request_needs_review'
    elif event and event.status not in ('failed_terminal',):
        reason = 'existing_event_needs_review'
    else:
        reason = 'missing_delivery_obligation'

    age_seconds = None
    if row.last_state == 'expired' and row.last_expiry_ms:
        try:
            age_seconds = max(0, int(now.replace(tzinfo=timezone.utc).timestamp()
                                     - int(row.last_expiry_ms) / 1000))
        except (TypeError, ValueError, OverflowError):
            pass
    if window in WINDOW_DAYS:
        in_window = age_seconds is not None and age_seconds <= WINDOW_DAYS[window] * 86400
    elif age_seconds is None:
        in_window = bool(allow_unknown_age)
    else:
        expiry_date = datetime.fromtimestamp(int(row.last_expiry_ms) / 1000,
                                             timezone.utc).date()
        in_window = bool(date_from and date_to and date_from <= expiry_date <= date_to)
    recoverable = reason == 'missing_delivery_obligation' and in_window
    return {
        'service_key': row.service_key, 'server_id': row.server_id,
        'account': row.client_email, 'state': row.last_state,
        'notification_kind': kind, 'generation': generation,
        'state_version': int(row.state_version or 0),
        'age_seconds': age_seconds, 'age_unknown': age_seconds is None,
        'last_observed_at': row.last_observed_at.isoformat() + 'Z'
                            if row.last_observed_at else None,
        'reason': reason, 'recoverable': recoverable,
        'obligation_status': event.status if event else None,
    }


def preview(*, window='7d', server_id=None, state=None, allow_unknown_age=False,
            date_from=None, date_to=None, limit=200, offset=0, now=None):
    """Read-only bounded preview. A reported gap is not automatically sent."""
    if window not in WINDOW_DAYS and window != 'custom':
        raise ValueError('invalid_window')
    if window != 'custom' and allow_unknown_age:
        raise ValueError('unknown_age_requires_custom_window')
    if window == 'custom':
        if date_from or date_to:
            if not date_from or not date_to:
                raise ValueError('custom_date_range_required')
            date_from, date_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if date_from > date_to:
                raise ValueError('invalid_date_range')
        elif not allow_unknown_age:
            raise ValueError('custom_date_range_required')
    moment = now or datetime.utcnow()
    query = ServiceObservedState.query.filter(
        ServiceObservedState.last_state.in_(TERMINAL_STATES))
    if server_id is not None:
        query = query.filter(ServiceObservedState.server_id == int(server_id))
    if state is not None:
        if state not in TERMINAL_STATES:
            raise ValueError('invalid_state')
        query = query.filter(ServiceObservedState.last_state == state)
    total = query.count()
    page_size = max(1, min(int(limit), 500))
    start = max(0, int(offset))
    rows = query.order_by(ServiceObservedState.id).offset(start).limit(page_size).all()
    candidates = [_assess(row, now=moment, window=window,
                          allow_unknown_age=allow_unknown_age,
                          date_from=date_from, date_to=date_to) for row in rows]
    counts = {}
    for item in candidates:
        counts[item['reason']] = counts.get(item['reason'], 0) + 1
    return {'window': window, 'current_terminal_accounts': total,
            'inspected': len(candidates), 'offset': start,
            'has_more': start + len(candidates) < total,
            'truncated': total > len(candidates),
            'counts': counts, 'candidates': candidates}


def activate(identity, *, window, allow_unknown_age=False, now=None):
    """Revalidate one reviewed identity and create/reopen an obligation only."""
    moment = now or datetime.utcnow()
    row = ServiceObservedState.query.filter_by(service_key=identity['service_key']).first()
    if row is None or row.last_state not in TERMINAL_STATES:
        return 'state_changed'
    date_from = identity.get('date_from')
    date_to = identity.get('date_to')
    if date_from and date_to:
        date_from, date_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
    item = _assess(row, now=moment, window=window,
                   allow_unknown_age=allow_unknown_age,
                   date_from=date_from, date_to=date_to)
    for field in ('service_key', 'state', 'generation', 'state_version'):
        if item[field] != identity.get(field):
            return 'identity_changed'
    if not item['recoverable']:
        return item['reason'] if item['reason'] != 'missing_delivery_obligation' else 'outside_window_or_stale'

    # Re-read the current shared snapshot, recipient and opt-out immediately
    # before creating debt. The delivery worker repeats these gates before POST.
    from panel.jobs import messaging
    from panel.core.phone import _extract_iran_mobile_from_text
    email = (row.client_email or '').strip().lower()
    clients = messaging._cached_snapshot_clients(row.server_id, email)
    if not clients:
        return 'snapshot_missing'
    cfg = messaging._get_sms_runtime_settings()
    sms_state = telemetry_state.sms_state_for(row.last_state)
    if any(messaging._classify_cached_client_state(client, cfg) != sms_state
           for client in clients):
        return 'state_changed'
    valid, reason = messaging._sms_depletion_state_still_valid(
        row.server_id, email, sms_state, cfg, service_key=row.service_key,
        expected_generation=item['generation'])
    if not valid:
        return reason or 'state_unproven'
    if messaging._sms_account_opted_out(row.server_id, email,
                                        clients[0].get('comment') or ''):
        return 'opted_out'
    if not _extract_iran_mobile_from_text(email, clients[0].get('comment') or ''):
        return 'invalid_recipient'
    existing = ServiceNotificationEvent.query.filter_by(
        service_key=row.service_key, notification_kind=item['notification_kind'],
        lifecycle_generation=item['generation']).order_by(
            ServiceNotificationEvent.id.desc()).first()
    if existing and existing.status == 'failed_terminal':
        existing.status = 'retry'
        existing.next_attempt_at = moment
        existing.last_error = 'missing_delivery_obligation'
        db.session.commit()
        return 'reactivated'
    if existing:
        return 'already_covered'
    recovery_id = 'rc:' + hashlib.sha256(
        f'{row.service_key}|{item["generation"]}|{item["notification_kind"]}'.encode()
    ).hexdigest()[:40]
    try:
        db.session.add(ServiceNotificationEvent(
            event_id=recovery_id, service_key=row.service_key,
            server_id=row.server_id, client_uuid=row.client_uuid,
            client_email=row.client_email, state=row.last_state,
            notification_kind=item['notification_kind'],
            state_version=row.state_version,
            lifecycle_generation=item['generation'], observed_at=moment,
            telemetry_updated_at=row.last_telemetry_updated_at,
            source='reconciliation_recovery', status='pending',
            attempt_count=0, next_attempt_at=moment,
            idempotency_key=('depletion-' + recovery_id)[:160],
            created_at=moment, updated_at=moment))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return 'already_covered'
    return 'created'
