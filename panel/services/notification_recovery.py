"""Operator-reviewed recovery of historical terminal notification gaps.

Preview only reads the ledger. Activation inserts outbox obligations; it never
submits SMS. An unknown state age requires an explicit custom-window selection.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib

from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, or_

from panel.extensions import db
from panel.models import (OPEN_NOTIFICATION_STATUSES, ServiceNotificationEvent,
                          ServiceLifecycleState, ServiceObservedState, SmsSendLog)
from panel.services import telemetry_state


TERMINAL_STATES = ('volume_ended', 'expired')
SUPPRESSION_REASONS = frozenset((
    'no_identity_email', 'reseller_owned', 'opted_out_recheck', 'no_recipient',
    'unlimited_skipped', 'expired_too_old', 'ended_too_old', 'no_template',
    'empty_message',
))
WINDOW_DAYS = {'24h': 1, '3d': 3, '7d': 7}
BUCKETS = ('confirmed', 'active_obligation', 'valid_suppression',
           'needs_review', 'missing_obligation')


def state_entry(row, *, now):
    """Return only evidenced state age; an observation refresh is not entry."""
    entered = row.state_entered_at
    quality = row.state_entered_at_quality
    if entered is None and row.last_state == 'expired':
        try:
            inferred = datetime.utcfromtimestamp(int(row.last_expiry_ms) / 1000)
            if datetime(2000, 1, 1) <= inferred <= now:
                entered, quality = inferred, 'inferred_expiry'
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    if entered is None:
        return None, 'unknown_baseline', None
    return entered, quality or 'observed_transition', max(0, int((now - entered).total_seconds()))


def _evidence(rows):
    """Fetch one page's generations, events and send evidence in three queries."""
    keys = [row.service_key for row in rows]
    if not keys:
        return {}, {}, {}
    generations = {row.service_key: int(row.generation or 0) for row in
                   ServiceLifecycleState.query.filter(
                       ServiceLifecycleState.service_key.in_(keys)).all()}
    current_generations = set(generations.values()) | {0}
    events = defaultdict(list)
    for event in ServiceNotificationEvent.query.filter(
            ServiceNotificationEvent.service_key.in_(keys),
            ServiceNotificationEvent.lifecycle_generation.in_(current_generations)).order_by(
                ServiceNotificationEvent.id.desc()).all():
        events[event.service_key].append(event)
    logs = defaultdict(list)
    identities = defaultdict(list)
    for row in rows:
        if row.client_email:
            identities[(row.server_id, row.client_email.lower())].append(row.service_key)
    legacy_match = and_(SmsSendLog.service_key.is_(None),
                        SmsSendLog.email.in_([email for _, email in identities]),
                        SmsSendLog.server_id.in_([server for server, _ in identities]))
    for log in SmsSendLog.query.filter(
            or_(SmsSendLog.service_key.in_(keys), legacy_match),
            or_(SmsSendLog.lifecycle_generation.in_(current_generations),
                SmsSendLog.lifecycle_generation.is_(None))).all():
        if log.service_key:
            logs[log.service_key].append(log)
        else:
            for key in identities.get((log.server_id, (log.email or '').lower()), ()):
                logs[key].append(log)
    return generations, events, logs


def classify(row, *, now, generation, events, logs):
    """Exactly one primary coverage bucket for a current terminal account."""
    kind = telemetry_state.SERVICE_STATE_TO_NOTIFICATION_KIND.get(row.last_state)
    sms_state = telemetry_state.sms_state_for(row.last_state)
    related = [event for event in events
               if event.lifecycle_generation == generation
               and event.notification_kind == kind]
    send_logs = [log for log in logs
                 if ((log.lifecycle_generation == generation or
                      getattr(log, 'service_key', row.service_key) is None)
                     and log.state == sms_state)]
    confirmed = any(getattr(log, 'service_key', row.service_key) == row.service_key
                    and log.successful and log.verification_status == 'confirmed'
                    for log in send_logs)
    active = next((event for event in related
                   if event.status in OPEN_NOTIFICATION_STATUSES), None)
    suppressed = next((event for event in related
                       if event.status == 'skipped'
                       and event.last_error in SUPPRESSION_REASONS), None)
    request = next((event for event in related if event.gateway_request_id), None)
    submitted = any(log.request_id or getattr(log, 'status', None) in ('queued', 'sent')
                    or getattr(log, 'gateway_state', None) in ('accepted', 'queued', 'active')
                    for log in send_logs)
    legacy = next((event for event in related if event.status == 'sent'), None)
    unresolved = next((event for event in related
                       if event.status not in ('failed_terminal',)), None)
    if confirmed:
        bucket, reason, event = 'confirmed', 'confirmed', related[0] if related else None
    elif active:
        bucket, reason, event = 'active_obligation', 'active_obligation', active
    elif legacy:
        bucket, reason, event = 'needs_review', 'legacy_submission_needs_review', legacy
    elif request or submitted:
        bucket, reason, event = 'needs_review', 'gateway_request_needs_review', request
    elif suppressed:
        bucket, reason, event = 'valid_suppression', 'terminal_suppression', suppressed
    elif unresolved:
        bucket, reason, event = 'needs_review', 'existing_event_needs_review', unresolved
    else:
        bucket, reason, event = 'missing_obligation', 'missing_delivery_obligation', related[0] if related else None
    entered, quality, age_seconds = state_entry(row, now=now)
    return {
        'service_key': row.service_key, 'server_id': row.server_id,
        'account': row.client_email, 'state': row.last_state,
        'notification_kind': kind, 'generation': generation,
        'state_version': int(row.state_version or 0),
        'coverage_bucket': bucket, 'reason': reason,
        'state_entered_at': entered.isoformat() + 'Z' if entered else None,
        'age_quality': quality, 'age_source': quality,
        'age_known': entered is not None, 'age_seconds': age_seconds,
        'age_unknown': entered is None,
        'first_seen_at': row.created_at.isoformat() + 'Z' if row.created_at else None,
        'last_observed_at': row.last_observed_at.isoformat() + 'Z'
                            if row.last_observed_at else None,
        'obligation_status': event.status if event else None,
        'event_id': event.event_id if event else None,
        'attempt_count': int(event.attempt_count or 0) if event else 0,
        'last_error': event.last_error if event else None,
        'last_attempt_at': (event.last_attempt_at.isoformat() + 'Z'
                            if event and event.last_attempt_at else None),
        'next_attempt_at': (event.next_attempt_at.isoformat() + 'Z'
                            if event and event.next_attempt_at else None),
        'gateway_request_id': event.gateway_request_id if event else None,
    }


def _assess(row, *, now, window, allow_unknown_age=False,
            date_from=None, date_to=None, evidence=None):
    generations, events, logs = evidence if evidence is not None else _evidence([row])
    item = classify(row, now=now,
                    generation=generations.get(row.service_key, 0),
                    events=events.get(row.service_key, ()),
                    logs=logs.get(row.service_key, ()))
    age_seconds = item['age_seconds']
    if window in WINDOW_DAYS:
        in_window = age_seconds is not None and age_seconds <= WINDOW_DAYS[window] * 86400
    elif age_seconds is None:
        in_window = bool(allow_unknown_age)
    else:
        in_window = bool(date_from and date_to and
                         date_from <= datetime.fromisoformat(item['state_entered_at'][:-1]).date() <= date_to)
    item['recoverable'] = item['coverage_bucket'] == 'missing_obligation' and in_window
    return item


def _fresh_transition(row, moment):
    return bool(row.state_entered_at_quality == 'observed_transition'
                and row.state_entered_at
                and row.state_entered_at >= moment - timedelta(hours=1))


def accounting(*, now=None, bucket=None, state=None, server_id=None,
               limit=50, offset=0, age_filter=None, fresh_gap=False,
               historical_missing_only=False, oldest_only=False,
               q=None,
               window=None, allow_unknown_age=False,
               date_from=None, date_to=None):
    """Complete, read-only terminal coverage census with bounded evidence reads."""
    moment = now or datetime.utcnow()
    if bucket is not None and bucket not in BUCKETS + ('needs_notification',):
        raise ValueError('invalid_bucket')
    if age_filter not in (None, 'known', 'unknown'):
        raise ValueError('invalid_age_filter')
    if state is not None and state not in TERMINAL_STATES:
        raise ValueError('invalid_state')
    search = (q or '').strip()[:100]
    counts = {name: 0 for name in BUCKETS}
    by_state = {name: {key: 0 for key in BUCKETS} for name in TERMINAL_STATES}
    by_state_totals = {name: 0 for name in TERMINAL_STATES}
    rows_out = []
    selected_total = 0
    unknown_missing = 0
    historical_missing = 0
    fresh_gap_count = 0
    known_missing = 0
    selected_known_missing = 0
    oldest_known_missing = None
    oldest_known_outstanding = None
    oldest_item = None
    unknown_outstanding = 0
    cursor = 0
    while True:
        query = ServiceObservedState.query.filter(
            ServiceObservedState.id > cursor,
            ServiceObservedState.last_state.in_(TERMINAL_STATES))
        if state:
            query = query.filter(ServiceObservedState.last_state == state)
        if server_id is not None:
            query = query.filter(ServiceObservedState.server_id == int(server_id))
        if search:
            query = query.filter(or_(ServiceObservedState.client_email.ilike(f'%{search}%'),
                                     ServiceObservedState.service_key.ilike(f'%{search}%')))
        page = query.order_by(ServiceObservedState.id).limit(200).all()
        if not page:
            break
        cursor = page[-1].id
        evidence = _evidence(page)
        for row in page:
            item = (_assess(row, now=moment, window=window,
                            allow_unknown_age=allow_unknown_age,
                            date_from=date_from, date_to=date_to,
                            evidence=evidence)
                    if window else classify(
                        row, now=moment,
                        generation=evidence[0].get(row.service_key, 0),
                        events=evidence[1].get(row.service_key, ()),
                        logs=evidence[2].get(row.service_key, ())))
            name = item['coverage_bucket']
            counts[name] += 1
            by_state[row.last_state][name] += 1
            by_state_totals[row.last_state] += 1
            if name in ('needs_review', 'missing_obligation') and _fresh_transition(row, moment):
                fresh_gap_count += 1
            if name in ('active_obligation', 'missing_obligation'):
                if item['age_known']:
                    if (oldest_known_outstanding is None or
                            item['age_seconds'] > oldest_known_outstanding):
                        oldest_known_outstanding = item['age_seconds']
                        oldest_item = item
                else:
                    unknown_outstanding += 1
            if name == 'missing_obligation':
                if not _fresh_transition(row, moment):
                    historical_missing += 1
                if item['age_known']:
                    known_missing += 1
                    if item.get('recoverable'):
                        selected_known_missing += 1
                    oldest_known_missing = max(oldest_known_missing or 0,
                                               item['age_seconds'])
                else:
                    unknown_missing += 1
            selected = (bucket is None or bucket == name or
                        bucket == 'needs_notification' and name in
                        ('active_obligation', 'missing_obligation'))
            if age_filter == 'known':
                selected = selected and item['age_known']
            elif age_filter == 'unknown':
                selected = selected and not item['age_known']
            if fresh_gap:
                selected = (selected and name in ('needs_review', 'missing_obligation')
                            and _fresh_transition(row, moment))
            if historical_missing_only:
                selected = (selected and name == 'missing_obligation' and
                            not _fresh_transition(row, moment))
            if selected:
                if selected_total >= offset and len(rows_out) < limit:
                    rows_out.append(item)
                selected_total += 1
    total = sum(counts.values())
    if oldest_only:
        rows_out = [oldest_item] if oldest_item and offset == 0 and limit else []
        selected_total = 1 if oldest_item else 0
    return {
        'current_terminal_accounts': total, 'counts': counts,
        'by_state': {name: {'total': by_state_totals[name], **by_state[name]}
                     for name in TERMINAL_STATES},
        'known_age_missing_obligations': known_missing,
        'unknown_age_missing_obligations': unknown_missing,
        'historical_missing_obligations': historical_missing,
        'fresh_gap_count': fresh_gap_count,
        'selected_known_age_missing': selected_known_missing,
        'oldest_known_missing_age_seconds': oldest_known_missing,
        'oldest_known_outstanding_age_seconds': oldest_known_outstanding,
        'unknown_age_outstanding_count': unknown_outstanding,
        'needs_notification': counts['active_obligation'] + counts['missing_obligation'],
        'selected_total': selected_total, 'offset': offset,
        'has_more': offset + len(rows_out) < selected_total,
        'rows': rows_out,
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
    evidence = _evidence(rows)
    candidates = [_assess(row, now=moment, window=window,
                          allow_unknown_age=allow_unknown_age,
                          date_from=date_from, date_to=date_to,
                          evidence=evidence) for row in rows]
    counts = {}
    for item in candidates:
        counts[item['reason']] = counts.get(item['reason'], 0) + 1
    summary = accounting(now=moment, window=window,
                         allow_unknown_age=allow_unknown_age,
                         date_from=date_from, date_to=date_to,
                         state=state, server_id=server_id, limit=0)
    return {'window': window, 'current_terminal_accounts': total,
            'inspected': len(candidates), 'offset': start,
            'has_more': start + len(candidates) < total,
            'truncated': total > len(candidates),
            'counts': counts, 'summary': summary,
            'candidates': candidates}


def open_obligation(row, *, generation, moment, source='reconciliation_recovery'):
    """Stage one generation-safe outbox row; caller owns commit and lifecycle gate."""
    recovery_id = telemetry_state.transition_event_id(
        row.service_key, row.last_state, row.state_version)
    collision = ServiceNotificationEvent.query.filter_by(event_id=recovery_id).first()
    if collision:
        if collision.lifecycle_generation == generation:
            return False
        # An older lifecycle can have the same state/version identity. Its
        # unique key cannot be reused; the generation-bound fallback is stable.
        recovery_id = 'rc:' + hashlib.sha256(
            f'{row.service_key}|{generation}|{row.last_state}'.encode()
        ).hexdigest()[:40]
    if ServiceNotificationEvent.query.filter_by(event_id=recovery_id).first():
        return False
    db.session.add(ServiceNotificationEvent(
        event_id=recovery_id, service_key=row.service_key,
        server_id=row.server_id, client_uuid=row.client_uuid,
        client_email=row.client_email, state=row.last_state,
        notification_kind=telemetry_state.SERVICE_STATE_TO_NOTIFICATION_KIND[row.last_state],
        state_version=row.state_version,
        lifecycle_generation=generation, observed_at=moment,
        telemetry_updated_at=row.last_telemetry_updated_at,
        source=source, status='pending',
        attempt_count=0, next_attempt_at=moment,
        idempotency_key=('depletion-' + recovery_id)[:160],
        created_at=moment, updated_at=moment))
    return True


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
    try:
        if not open_obligation(row, generation=item['generation'], moment=moment):
            return 'already_covered'
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return 'already_covered'
    return 'created'
