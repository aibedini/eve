"""Durable service-lifecycle generation and notification invalidation.

Stable service identity, the durable lifecycle generation, and the invalidation
outbox that keeps EVE and the SMS gateway consistent across a renewal.

Why this module exists
----------------------
A depletion reminder is a claim about a service's *current* state. Two races make
that claim wrong even when the scan itself is correct:

* RACE A -- the scan decides 'volume ended', the reminder reaches the gateway,
  and the customer renews. The gateway still holds the old SMS.
* RACE B -- a worker scans a cached panel snapshot that predates the renewal and
  creates a brand-new 'volume ended' reminder afterwards.

Both are prevented by one durable, monotonically increasing per-service number:
the lifecycle ``generation``. Every reminder carries the generation it was
computed from; renewal advances the generation and asks the gateway to revoke
everything older. The generation lives in the database, never in Redis or a
process-local dict, because the worker that renews and the worker that scans are
different OS processes (gunicorn worker A renews, worker B still has an old
snapshot -- only a durable barrier stops worker B).

Contract with the gateway: see ``shared/eve-gmweb-contract-v1.json``
(``post_invalidate``) and ``docs/GMWEB_CONTRACT.md``.
"""
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from panel.core.redis_client import GLOBAL_SERVER_DATA
from panel.extensions import db
from panel.models import (
    SERVICE_INVALIDATION_BACKOFF_SECONDS,
    SERVICE_INVALIDATION_STATUSES,
    ServiceLifecycleState,
    ServiceNotificationOutbox,
    SmsSendLog,
)

logger = logging.getLogger(__name__)

# The canonical external notification kinds. GMweb filters on exactly these.
NOTIFICATION_KINDS = ('near_expiry', 'low_volume', 'expired', 'volume_ended')
# Lifecycle invalidation only ever targets automated depletion reminders. The
# transactional created/renew confirmations carry requires_validation=False and
# are deliberately absent here, so renewal can never cancel the message that
# tells the customer their renewal worked.
DEPLETION_NOTIFICATION_KINDS = NOTIFICATION_KINDS
TRANSACTIONAL_NOTIFICATION_KINDS = ('created', 'renew')

# EVE's internal depletion state `ended` is the customer-facing 'volume ended'.
# Keep the internal name (cooldowns, config keys, logs) and translate only at the
# gateway boundary, so existing settings keep working.
SMS_STATE_TO_NOTIFICATION_KIND = {
    'near_expiry': 'near_expiry',
    'low_volume': 'low_volume',
    'expired': 'expired',
    'ended': 'volume_ended',
    'renew': 'renew',
    'created': 'created',
}

# How long an identical lifecycle operation is treated as the same event. A
# retried renewal request (v3 panels take 10-18s, so operators retry while the
# first attempt is still settling) must not advance the generation twice.
LIFECYCLE_OPERATION_DEDUPE_SECONDS = 1800

_GENERATION_RACE_RETRIES = 5


def resolve_client_uuid(client) -> str | None:
    """The stable panel client identity, in the 3x-ui v3 / legacy order."""
    if not isinstance(client, dict):
        return None
    for key in ('uuid', 'id', 'subId', 'client_uuid'):
        value = client.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def client_uuid_from_email(email: str) -> str | None:
    """Best durable identity available for an email-only call site.

    Not every renewal path has the panel client dict in hand (the bulk repair
    jobs address clients by email). The email is lower-cased and used as the
    identity *fallback* -- never as the primary identity, because one phone can
    own several services and one email can be reused on several inbounds."""
    text = str(email or '').strip().lower()
    return text or None


def make_service_key(server_id, client_uuid) -> str:
    """The one canonical serviceKey: ``eve:<serverId>:<clientUuid>``.

    Every producer in EVE must call this helper. Constructing the string inline
    somewhere else is how the renewal side and the scan side silently stop
    agreeing on identity, at which point invalidation quietly stops working."""
    try:
        sid = int(server_id)
    except (TypeError, ValueError):
        sid = 0
    identity = str(client_uuid or '').strip()
    if not identity:
        identity = 'unknown'
    return f"eve:{sid}:{identity}"


def service_key_for_client(server_id, client, *, email: str | None = None) -> str:
    """serviceKey for a panel client dict or an email-only call site."""
    identity = resolve_client_uuid(client)
    if not identity:
        identity = client_uuid_from_email(
            email if email is not None else (client or {}).get('email'))
    return make_service_key(server_id, identity)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def lifecycle_event_id(service_key: str, generation: int, operation_id=None) -> str:
    """Stable id for one lifecycle transition.

    Reusing the caller's operation id when it has one is what makes the gateway
    call idempotent across a retried renewal: the same renewal produces the same
    eventId, so GMweb replays its first answer instead of applying twice."""
    stable = str(operation_id or '').strip()
    if stable:
        return f"lc:{service_key}:{stable}"[:128]
    return f"lc:{service_key}:{int(generation)}"[:128]


def sms_notification_kind(state: str) -> str:
    """Map an internal SMS state (or event) to the external notificationKind."""
    key = str(state or '').strip().lower().replace('-', '_')
    return SMS_STATE_TO_NOTIFICATION_KIND.get(key, key or 'unknown')


def is_depletion_kind(kind: str) -> bool:
    return str(kind or '').strip().lower() in DEPLETION_NOTIFICATION_KINDS


def build_invalidation_payload(*, service_key: str, generation: int, reason: str,
                               correlation_id: str, event_id: str, kinds=None) -> dict:
    """The exact POST /send/invalidate body EVE sends."""
    selected = [k for k in (kinds or DEPLETION_NOTIFICATION_KINDS) if k]
    return {
        'source': 'eve',
        'serviceKey': service_key,
        'currentGeneration': int(generation),
        'invalidateKinds': selected,
        'reason': str(reason or 'lifecycle_change')[:64],
        'correlationId': str(correlation_id or new_correlation_id())[:64],
        'eventId': str(event_id)[:200],
    }


def validate_invalidation_response(body) -> tuple[bool, str | None]:
    """Validate the gateway answer before any of it is trusted.

    A malformed or partial body is treated exactly like a transport failure: the
    outbox keeps the event and retries. Nothing here may raise on hostile input."""
    if not isinstance(body, dict):
        return False, 'invalid_invalidate_response'
    if body.get('ok') is not True:
        return False, str(body.get('error') or 'invalidate_not_ok')[:255]
    for field in ('cancelledPending', 'revokedActive', 'revokedInflight',
                  'alreadyTerminal'):
        value = body.get(field)
        if value is None:
            continue
        try:
            if int(value) < 0:
                return False, f'invalid_invalidate_count:{field}'
        except (TypeError, ValueError):
            return False, f'invalid_invalidate_count:{field}'
    generation = body.get('currentGeneration')
    if generation is not None:
        try:
            if int(generation) < 0:
                return False, 'invalid_invalidate_generation'
        except (TypeError, ValueError):
            return False, 'invalid_invalidate_generation'
    return True, None


def _state_for_key(service_key: str):
    try:
        return ServiceLifecycleState.query.filter_by(service_key=service_key).first()
    except Exception:
        return None


def generation_state(service_key: str) -> dict:
    """Current durable generation for a service (None when never seen).

    Deliberately a plain indexed lookup: the depletion scan calls this for
    candidates only, never for every row on the dashboard."""
    if not service_key:
        return {'service_key': '', 'generation': None,
                'last_lifecycle_change_at': None}
    state = _state_for_key(service_key)
    if state is None:
        return {'service_key': service_key, 'generation': None,
                'last_lifecycle_change_at': None}
    return {
        'service_key': service_key,
        'generation': int(state.generation or 0),
        'last_lifecycle_change_at': _as_naive(state.last_lifecycle_change_at),
    }


def generations_for_keys(keys) -> dict:
    """Batch variant of :func:`generation_state` -- one query, no N+1.

    Returns ``{service_key: {...}}``; keys with no row are absent, which the
    caller must read as 'no lifecycle change ever recorded', not as an
    error."""
    wanted = [str(k) for k in (keys or []) if k]
    if not wanted:
        return {}
    out = {}
    try:
        rows = (ServiceLifecycleState.query
                .filter(ServiceLifecycleState.service_key.in_(wanted))
                .all())
    except Exception:
        return {}
    for row in rows:
        out[row.service_key] = {
            'service_key': row.service_key,
            'generation': int(row.generation or 0),
            'last_lifecycle_change_at': _as_naive(row.last_lifecycle_change_at),
        }
    return out


def _is_duplicate_operation(state, operation_id, now) -> bool:
    """True when this exact lifecycle operation was already applied moments ago.

    v3 panels can take 10-18s to settle, so an operator (or the Telegram bot)
    retries a renewal whose response was slow. That retry must not advance the
    generation a second time -- it is the same lifecycle event."""
    if not operation_id or state is None:
        return False
    if str(state.last_operation_id or '') != str(operation_id):
        return False
    changed_at = _as_naive(state.last_lifecycle_change_at)
    if changed_at is None:
        return False
    return (now - changed_at).total_seconds() <= LIFECYCLE_OPERATION_DEDUPE_SECONDS


def _advance_generation(service_key, *, server_id, client_uuid, client_email,
                        event_type, now, operation_id, correlation_id):
    """Bump the durable generation exactly once and return the new row."""
    for _attempt in range(_GENERATION_RACE_RETRIES):
        state = _state_for_key(service_key)
        if state is None:
            state = ServiceLifecycleState(
                service_key=service_key,
                server_id=_as_int(server_id),
                client_uuid=(str(client_uuid) if client_uuid else None),
                client_email=(str(client_email).lower() if client_email else None),
                generation=0,
                created_at=now,
                updated_at=now,
            )
            db.session.add(state)
            try:
                db.session.flush()
            except IntegrityError:
                # Another worker created the same service between our SELECT
                # and INSERT. Retry: the next pass finds the row and takes the
                # UPDATE path, which is where the monotonic bump happens.
                db.session.rollback()
                continue
        state.generation = int(state.generation or 0) + 1
        if client_uuid:
            state.client_uuid = str(client_uuid)
        if client_email:
            state.client_email = str(client_email).lower()
        if server_id not in (None, ''):
            state.server_id = _as_int(server_id)
        state.last_lifecycle_change_at = now
        state.last_event_type = str(event_type or 'renewal')[:32]
        state.last_operation_id = (str(operation_id)[:128] if operation_id else None)
        state.last_correlation_id = (str(correlation_id)[:64] if correlation_id else None)
        state.updated_at = now
        return state
    raise RuntimeError('service_generation_contention:%s' % service_key)


def _outbox_row_for_event(event_id: str):
    try:
        return ServiceNotificationOutbox.query.filter_by(event_id=event_id).first()
    except Exception:
        return None


def handle_successful_service_lifecycle_change(
        *, server_id, client_uuid=None, client_email=None, event_type='renewal',
        operation_id=None, correlation_id=None, reason=None, event_id=None,
        invalidate_kinds=None, commit=True, dispatch=True):
    """The one place every successful renewal/extension must call.

    Contract:

    * call it ONLY after the panel write actually succeeded and was read back;
      invalidating before that could suppress a legitimate depletion alert for
      a renewal that later failed;
    * it commits the new durable generation *and* the invalidation-outbox row
      in the same transaction, so a crash or a process restart cannot lose the
      invalidation;
    * the customer's renewal never depends on the SMS gateway: a gateway
      failure only leaves the outbox row ``pending`` for the background retry;
    * an immediate asynchronous attempt is still made right after the commit,
      because shrinking the propagation window matters.

    Returns a dict describing what happened; never raises for gateway trouble.
    """
    now = datetime.utcnow()
    service_key = make_service_key(server_id, client_uuid)
    correlation = str(correlation_id or new_correlation_id())[:64]
    kinds = list(invalidate_kinds) if invalidate_kinds else list(DEPLETION_NOTIFICATION_KINDS)
    change_type = str(event_type or 'renewal').strip().lower() or 'renewal'

    result = {
        'service_key': service_key,
        'generation': None,
        'advanced': False,
        'idempotent': False,
        'event_id': None,
        'outbox_id': None,
        'correlation_id': correlation,
        'invalidate_kinds': kinds,
        'error': None,
    }

    try:
        state = _state_for_key(service_key)
        if state is not None and _is_duplicate_operation(state, operation_id, now):
            # Same lifecycle operation replayed: keep the generation AND the
            # eventId, so a gateway retry answers idempotently instead of
            # applying the invalidation twice.
            generation = int(state.generation or 0)
            result.update({
                'generation': generation,
                'idempotent': True,
                'event_id': lifecycle_event_id(service_key, generation, operation_id),
                'correlation_id': str(state.last_correlation_id or correlation)[:64],
            })
            return result

        state = _advance_generation(
            service_key, server_id=server_id, client_uuid=client_uuid,
            client_email=client_email, event_type=change_type, now=now,
            operation_id=operation_id, correlation_id=correlation,
        )
        if change_type == 'renewal':
            state.last_renewed_at = now
        generation = int(state.generation or 0)
        ev_id = str(event_id or lifecycle_event_id(service_key, generation, operation_id))[:128]

        row = _outbox_row_for_event(ev_id)
        if row is None:
            row = ServiceNotificationOutbox(
                event_id=ev_id,
                service_key=service_key,
                server_id=_as_int(server_id),
                client_uuid=(str(client_uuid) if client_uuid else None),
                generation=generation,
                reason=str(reason or change_type)[:64],
                correlation_id=correlation,
                invalidate_kinds=','.join(kinds)[:255],
                status='pending',
                attempt_count=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
            db.session.add(row)

        if commit:
            db.session.commit()
        else:
            db.session.flush()

        # Read the id back from the row itself. A commit expires the instance by
        # default, and `row.id` after an expiry can raise DetachedInstanceError in
        # a worker whose session was reset; the primary key of a just-committed
        # row is exactly the value the outbox drain needs.
        try:
            outbox_id = int(row.id) if row.id is not None else None
        except Exception:
            outbox_id = None
        if outbox_id is None:
            persisted = _outbox_row_for_event(ev_id)
            outbox_id = getattr(persisted, 'id', None)

        result.update({
            'generation': generation,
            'advanced': True,
            'event_id': ev_id,
            'outbox_id': outbox_id,
        })
    except Exception as exc:
        result['error'] = ('%s:%s' % (type(exc).__name__, exc))[:255]
        logger.exception('[lifecycle] generation commit failed for %s', service_key)
        try:
            db.session.rollback()
        except Exception:
            pass
        return result

    if dispatch:
        dispatch_outbox_row_async(result['outbox_id'])
    return result


def note_service_lifecycle_change(*, server_id, client_uuid=None, client_email=None,
                                 event_type='extension', reason=None, operation_id=None,
                                 correlation_id=None, dispatch=True) -> dict:
    """Best-effort lifecycle note for the non-renewal restoration paths.

    A traffic reset with a new cap, a superadmin expiry/cap edit and the bulk
    add-days / add-volume repair all make an existing depletion reminder wrong,
    but they are not the canonical renewal and have no verified read-back. They
    call this instead of :func:`handle_successful_service_lifecycle_change` so
    the failure mode is explicit: a lifecycle problem is LOGGED and can never
    turn a successful panel change into an error for the operator.

    Callers must only reach this after the panel operation reported success."""
    try:
        return handle_successful_service_lifecycle_change(
            server_id=server_id,
            client_uuid=client_uuid,
            client_email=client_email,
            event_type=event_type,
            operation_id=operation_id,
            correlation_id=correlation_id,
            reason=reason or event_type,
            dispatch=dispatch,
        )
    except Exception as exc:
        logger.exception('[lifecycle] %s note failed for server=%s', event_type, server_id)
        return {'service_key': make_service_key(server_id, client_uuid),
                'generation': None, 'advanced': False, 'error': str(exc)[:255]}


def _backoff_delay(attempt: int) -> int:
    schedule = SERVICE_INVALIDATION_BACKOFF_SECONDS
    if attempt <= 0:
        return schedule[0]
    if attempt > len(schedule):
        return schedule[-1]
    return schedule[attempt - 1]


def _next_attempt(now: datetime, attempt: int) -> datetime:
    return now + timedelta(seconds=int(_backoff_delay(attempt)))


def _mark_outbox_sent(row, response: dict, now: datetime) -> None:
    row.status = 'sent'
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.last_attempt_at = now
    row.response_at = now
    row.updated_at = now
    row.last_error = None
    row.last_status_code = 200
    row.cancelled_pending = _as_int(response.get('cancelledPending'))
    row.revoked_active = _as_int(response.get('revokedActive'))
    row.revoked_inflight = _as_int(response.get('revokedInflight'))
    row.already_terminal = _as_int(response.get('alreadyTerminal'))


def _mark_outbox_failed(row, reason: str, now: datetime, status_code=None) -> None:
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.last_attempt_at = now
    row.updated_at = now
    row.last_error = str(reason or 'invalidation_failed')[:255]
    if status_code is not None:
        try:
            row.last_status_code = int(status_code)
        except (TypeError, ValueError):
            pass
    row.status = 'pending'
    # attempt_count was just incremented, so the FIRST failed attempt (count 1)
    # already schedules the second slot in the ladder.
    row.next_attempt_at = _next_attempt(now, int(row.attempt_count or 1))


def attempt_outbox_event(event_or_id) -> dict:
    """One delivery attempt for one outbox row. Never raises.

    A transient failure (timeout, 429, 5xx, malformed answer) leaves the row
    pending with the next backoff slot; nothing is silently swallowed -- the
    reason and the HTTP status are both persisted on the row."""
    from app import _get_sms_provider_settings, _get_sms_runtime_settings  # deferred
    from panel.jobs import messaging  # deferred: owns the HTTP client

    row = event_or_id
    if not isinstance(row, ServiceNotificationOutbox):
        try:
            row = db.session.get(ServiceNotificationOutbox, int(event_or_id))
        except Exception:
            row = None
    if row is None:
        return {'ok': False, 'terminal': True, 'reason': 'outbox_row_missing'}
    if row.status == 'sent':
        return {'ok': True, 'terminal': True, 'reason': 'already_sent'}

    now = datetime.utcnow()
    payload = build_invalidation_payload(
        service_key=row.service_key,
        generation=int(row.generation or 0),
        reason=row.reason or 'lifecycle_change',
        correlation_id=row.correlation_id or new_correlation_id(),
        event_id=row.event_id,
        kinds=row.kinds() or list(DEPLETION_NOTIFICATION_KINDS),
    )
    cfg = _get_sms_runtime_settings()
    if not (cfg.get('base_url') and cfg.get('api_key')):
        _mark_outbox_failed(row, 'gateway_not_configured', now)
        _commit_quietly()
        return {'ok': False, 'terminal': False, 'reason': 'gateway_not_configured'}

    provider_cfg = _get_sms_provider_settings(_provider_for_service(cfg, row), cfg)
    response = messaging._invalidate_notifications_via_gmweb(payload, provider_cfg)
    status_code = response.get('status_code')
    if response.get('ok'):
        ok_schema, schema_reason = validate_invalidation_response(response.get('body'))
        if ok_schema:
            _mark_outbox_sent(row, response.get('body') or {}, now)
            _commit_quietly()
            _log_invalidated_sends(row, response.get('body') or {}, now)
            return {'ok': True, 'terminal': True, 'reason': None}
        response = dict(response)
        response['reason'] = schema_reason
    reason = response.get('reason') or 'invalidation_failed'
    _mark_outbox_failed(row, reason, now, status_code=status_code)
    _commit_quietly()
    logger.warning(
        '[lifecycle] invalidation retry scheduled for %s (attempt %s): %s',
        row.service_key, row.attempt_count, reason)
    return {'ok': False, 'terminal': False, 'reason': reason}


def _provider_for_service(cfg: dict, row) -> str:
    """The gateway provider recorded for this service's own SMS traffic."""
    try:
        last = (db.session.query(SmsSendLog.gateway_provider)
                .filter(SmsSendLog.service_key == row.service_key)
                .order_by(SmsSendLog.id.desc())
                .limit(1)
                .scalar())
        if last:
            return str(last)
    except Exception:
        pass
    return str(cfg.get('provider') or 'gmweb')


def _commit_quietly() -> None:
    try:
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _log_invalidated_sends(row, body: dict, now: datetime) -> None:
    """Stamp the audit log so 'was this SMS revoked by that renewal?' is answerable."""
    try:
        rows = (SmsSendLog.query
                .filter(SmsSendLog.service_key == row.service_key,
                        SmsSendLog.lifecycle_generation.isnot(None),
                        SmsSendLog.lifecycle_generation < int(row.generation or 0),
                        SmsSendLog.invalidated_at.is_(None))
                .all())
        for entry in rows:
            entry.invalidated_at = now
            entry.invalidation_reason = str(row.reason or 'renewed')[:64]
            entry.lifecycle_event_id = row.event_id
            entry.updated_at = now
        if rows:
            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def dispatch_outbox_row_async(outbox_id) -> None:
    """Fire one immediate delivery attempt off the caller's thread."""
    if not outbox_id:
        return

    def _worker():
        try:
            from app import app  # deferred: Flask instance lives in app.py
            with app.app_context():
                attempt_outbox_event(outbox_id)
        except Exception:
            logger.exception('[lifecycle] immediate invalidation dispatch failed')

    threading.Thread(target=_worker, daemon=True,
                     name='eve-lifecycle-invalidate').start()


def flush_invalidation_outbox(limit: int = 20, now: datetime | None = None) -> dict:
    """Drain due outbox rows. Called by the background worker and by tests."""
    moment = now or datetime.utcnow()
    out = {'due': 0, 'sent': 0, 'retried': 0, 'failed': 0}
    try:
        rows = (ServiceNotificationOutbox.query
                .filter(ServiceNotificationOutbox.status == 'pending')
                .filter((ServiceNotificationOutbox.next_attempt_at.is_(None))
                        | (ServiceNotificationOutbox.next_attempt_at <= moment))
                .order_by(ServiceNotificationOutbox.next_attempt_at.asc(),
                          ServiceNotificationOutbox.id.asc())
                .limit(max(1, int(limit)))
                .all())
    except Exception:
        logger.exception('[lifecycle] outbox read failed')
        return out
    out['due'] = len(rows)
    for row in rows:
        try:
            result = attempt_outbox_event(row)
        except Exception:
            logger.exception('[lifecycle] outbox delivery crashed for %s', row.event_id)
            result = {'ok': False, 'terminal': False}
        if result.get('ok'):
            out['sent'] += 1
        elif result.get('terminal'):
            out['failed'] += 1
        else:
            out['retried'] += 1
    return out


def pending_invalidation_count() -> int:
    """How many lifecycle invalidations the gateway has not confirmed yet."""
    try:
        return int(ServiceNotificationOutbox.query
                   .filter(ServiceNotificationOutbox.status == 'pending')
                   .count() or 0)
    except Exception:
        return 0


def invalidation_outbox_worker(interval_seconds: int = 15) -> None:
    """Background drain loop; survives restarts because the queue is the DB."""
    from app import app  # deferred
    while True:
        try:
            with app.app_context():
                flush_invalidation_outbox(limit=20)
        except Exception:
            logger.exception('[lifecycle] invalidation outbox cycle failed')
        time.sleep(max(1, int(interval_seconds)))


def invalidation_status(service_key: str | None = None, limit: int = 20) -> dict:
    """Operator-facing view: what is pending, what failed, what was revoked."""
    out = {'pending': pending_invalidation_count(), 'events': []}
    try:
        query = ServiceNotificationOutbox.query
        if service_key:
            query = query.filter(ServiceNotificationOutbox.service_key == service_key)
        rows = (query.order_by(ServiceNotificationOutbox.id.desc())
                .limit(max(1, int(limit))).all())
        out['events'] = [row.to_dict() for row in rows]
    except Exception:
        pass
    return out


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_naive(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    return None
