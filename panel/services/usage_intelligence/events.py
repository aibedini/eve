"""Business events: the authoritative record of what happened to a paid cycle.

This module is the only writer of ``RenewalEvent``. It exists to make one rule
impossible to get wrong (RFP sections 3-8):

    a raw counter movement is telemetry, an explicit verified mutation is a fact.

So:

* :func:`record_verified_renewal` is called by the mutation path *after* the panel write
  was read back and matched. It records ``verified=True`` and therefore can start a new
  recommendation cycle.
* :func:`record_inferred_reset` is called by the usage collector when it merely observes
  a counter decrease. It records ``event_type='inferred_reset'``,
  ``source='counter_reset'``, ``verified=False`` and can never be a cycle boundary - and
  it is skipped entirely when an explicit renewal was recorded moments ago, so one real
  renewal does not become two events.
* Both are idempotent on ``operation_id``: the ``(operation_id, event_type)`` unique
  constraint is the backstop, and a caller's retry gets the existing row back instead of
  an error.
"""
from datetime import datetime, timedelta

from panel.extensions import db
from panel.models import CYCLE_BOUNDARY_EVENT_TYPES, RenewalEvent


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_naive(value):
    """Panel/DB timestamps are naive UTC throughout the project."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    return None


def _ms_to_naive(value):
    """Convert a panel expiry (ms since epoch; 0 unlimited, negative not-started)."""
    ms = _as_int(value)
    if not ms or ms <= 0:
        return None
    try:
        return datetime.utcfromtimestamp(ms / 1000.0)
    except (OverflowError, OSError, ValueError):
        return None


def _existing(operation_id, event_type):
    if not operation_id:
        return None
    try:
        return RenewalEvent.query.filter_by(
            operation_id=str(operation_id), event_type=str(event_type)).first()
    except Exception:
        return None


def record_renewal_event(*, server_id, sub_id, event_type='inferred_reset', source='inferred',
                         renewed_at=None, operation_id=None, client_uuid=None,
                         client_email=None, days=None, volume_bytes=None,
                         previous_volume_limit_bytes=None, new_volume_limit_bytes=None,
                         previous_remaining_bytes=None, carried_over_bytes=None,
                         granted_volume_bytes=None, previous_expiry_at=None,
                         new_expiry_at=None, traffic_reset=False,
                         is_unlimited_volume=False, is_unlimited_time=False,
                         verified=False, verified_at=None):
    """Insert one business event; returns the row (existing one on an idempotent retry).

    The caller owns the surrounding transaction: nothing is committed here (and no
    SAVEPOINT is used, because the pysqlite driver's savepoint handling can commit the
    outer transaction), so an event recorded for a renewal whose transaction later fails
    disappears with it (RFP section 49). The ``(operation_id, event_type)`` unique
    constraint is the backstop for a concurrent duplicate: it fails that caller's commit
    instead of silently opening a second cycle.
    """
    sid = _as_int(server_id)
    account = str(sub_id or '').strip()
    if sid is None or not account:
        return None
    event_type = str(event_type or 'inferred_reset')
    existing = _existing(operation_id, event_type)
    if existing is not None:
        return existing

    moment = renewed_at or datetime.utcnow()
    event = RenewalEvent(
        server_id=sid,
        sub_id=account,
        client_uuid=(str(client_uuid) if client_uuid else None),
        client_email_snapshot=(str(client_email) if client_email else None),
        event_type=event_type,
        source=str(source or 'inferred'),
        renewed_at=moment,
        volume_bytes=_as_int(volume_bytes),
        days=_as_int(days),
        previous_volume_limit_bytes=_as_int(previous_volume_limit_bytes),
        new_volume_limit_bytes=_as_int(new_volume_limit_bytes),
        previous_remaining_bytes=_as_int(previous_remaining_bytes),
        carried_over_bytes=_as_int(carried_over_bytes),
        granted_volume_bytes=_as_int(granted_volume_bytes),
        previous_expiry_at=_as_naive(previous_expiry_at),
        new_expiry_at=_as_naive(new_expiry_at),
        traffic_reset=bool(traffic_reset),
        is_unlimited_volume=bool(is_unlimited_volume),
        is_unlimited_time=bool(is_unlimited_time),
        operation_id=(str(operation_id) if operation_id else None),
        verified=bool(verified),
        verified_at=(verified_at or moment) if verified else None,
        created_at=moment,
    )
    try:
        db.session.add(event)
    except Exception:
        return None
    return event


def record_verified_renewal(*, server_id, sub_id, operation_id=None, email=None,
                            client_uuid=None, source='explicit_renew', event_type='renewal',
                            days=None, previous_volume_limit_bytes=0,
                            new_volume_limit_bytes=0, previous_remaining_bytes=None,
                            granted_volume_bytes=None, traffic_reset=False,
                            previous_expiry_ms=0, new_expiry_ms=0, renewed_at=None):
    """Record the renewal EVE just performed, after its read-back verified the write.

    ``granted_volume_bytes`` is what the customer bought; ``carried_over_bytes`` is the
    unused volume that survived the renewal into the new cap. They are stored apart on
    purpose (RFP section 9): a 50GB purchase on top of 10GB of leftover yields a 60GB
    available quota but a 50GB purchase, and analytics must never treat the difference as
    a bigger package.
    """
    previous_limit = _as_int(previous_volume_limit_bytes, 0) or 0
    new_limit = _as_int(new_volume_limit_bytes, 0) or 0
    previous_remaining = _as_int(previous_remaining_bytes)
    if previous_remaining is None:
        previous_remaining = previous_limit if previous_limit > 0 else None

    if traffic_reset:
        carried = 0
    else:
        carried = max(0, previous_remaining or 0)
    granted = _as_int(granted_volume_bytes)
    if granted is None:
        # No explicit purchase was supplied: the whole new cap is what was granted.
        granted = 0 if new_limit == 0 else max(0, new_limit - (previous_limit if not traffic_reset else 0))

    return record_renewal_event(
        server_id=server_id,
        sub_id=sub_id,
        event_type=event_type,
        source=source,
        operation_id=operation_id,
        client_uuid=client_uuid,
        client_email=email,
        renewed_at=renewed_at,
        days=days,
        volume_bytes=granted,
        previous_volume_limit_bytes=previous_limit,
        new_volume_limit_bytes=new_limit,
        previous_remaining_bytes=previous_remaining,
        carried_over_bytes=carried,
        granted_volume_bytes=granted,
        previous_expiry_at=_ms_to_naive(previous_expiry_ms),
        new_expiry_at=_ms_to_naive(new_expiry_ms),
        traffic_reset=bool(traffic_reset),
        is_unlimited_volume=(new_limit == 0),
        is_unlimited_time=(_as_int(new_expiry_ms, 0) or 0) == 0,
        verified=True,
        verified_at=renewed_at or datetime.utcnow(),
    )


def record_inferred_reset(*, server_id, sub_id, operation_id=None, client_uuid=None,
                          client_email=None, volume_bytes=None, new_expiry_at=None,
                          is_unlimited_volume=False, is_unlimited_time=False,
                          dedup_window_minutes=5, renewed_at=None):
    """Record that a counter decrease was observed - telemetry, not a renewal.

    Skipped when a verified cycle boundary already exists inside the dedup window, so an
    explicit renewal followed by the collector noticing the reset stays one event.
    """
    if has_recent_cycle_boundary(server_id, sub_id,
                                 within_minutes=dedup_window_minutes, now=renewed_at):
        return None
    return record_renewal_event(
        server_id=server_id,
        sub_id=sub_id,
        event_type='inferred_reset',
        source='counter_reset',
        operation_id=operation_id,
        client_uuid=client_uuid,
        client_email=client_email,
        renewed_at=renewed_at,
        volume_bytes=volume_bytes,
        new_expiry_at=new_expiry_at,
        is_unlimited_volume=bool(is_unlimited_volume),
        is_unlimited_time=bool(is_unlimited_time),
        verified=False,
    )


def latest_cycle_boundary(server_id, sub_id):
    """The newest authoritative boundary, or None. One indexed lookup (RFP section 31)."""
    sid = _as_int(server_id)
    account = str(sub_id or '').strip()
    if sid is None or not account:
        return None
    try:
        return (RenewalEvent.query
                .filter(RenewalEvent.server_id == sid,
                        RenewalEvent.sub_id == account,
                        RenewalEvent.verified.is_(True),
                        RenewalEvent.event_type.in_(CYCLE_BOUNDARY_EVENT_TYPES))
                .order_by(RenewalEvent.renewed_at.desc(), RenewalEvent.id.desc())
                .first())
    except Exception:
        return None


def has_recent_cycle_boundary(server_id, sub_id, *, within_minutes=5, now=None):
    """True when a verified boundary was recorded inside the dedup window."""
    moment = now or datetime.utcnow()
    try:
        threshold = moment - timedelta(minutes=max(0, int(within_minutes)))
    except (TypeError, ValueError):
        return False
    sid = _as_int(server_id)
    account = str(sub_id or '').strip()
    if sid is None or not account:
        return False
    try:
        return (RenewalEvent.query
                .filter(RenewalEvent.server_id == sid,
                        RenewalEvent.sub_id == account,
                        RenewalEvent.verified.is_(True),
                        RenewalEvent.event_type.in_(CYCLE_BOUNDARY_EVENT_TYPES),
                        RenewalEvent.renewed_at >= threshold)
                .first()) is not None
    except Exception:
        return False
