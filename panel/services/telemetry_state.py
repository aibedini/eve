"""Turn fresh X-UI telemetry into durable state transitions and notifications.

Why this module exists
----------------------
Depletion used to be discovered by a periodic SMS scan reading the shared
snapshot. That made the scan the *detector*, so a customer whose traffic ran out
between two scans could sit at "2 GB remaining" on the dashboard while X-UI
already reported "Volume Ended" -- and the reminder that should have fired at the
transition did not exist yet. Two races followed from that shape:

* the dashboard showed a quota the panel had already spent;
* the reminder for the transition was created (if at all) by a later scan, from a
  snapshot that might be newer than the renewal that had already invalidated it.

This module makes fresh telemetry the detector and the scan the repair net:

    fresh panel read
        -> canonical state (panel.services.client_state, ONE calculator)
        -> compare against the durable ledger
        -> record the new observation + emit one notification event per transition

Correctness properties enforced here, each with a test:

* IDENTITY: everything is keyed by the canonical ``eve:<server_id>:<client_uuid>``
  from panel/services/lifecycle.py. A phone number is never an identity.
* DEDUPLICATION is a DATABASE constraint, not a check-then-insert: two workers that
  observe the same transition both try to insert the same ``event_id`` and the
  loser adopts the winner row. Repeated polls of an unchanged ``ended`` service
  produce exactly one event.
* BASELINE, not transition: the first observation of a service records a baseline
  and emits nothing, so introducing this table cannot produce a deploy-time SMS
  storm. The bounded reconciliation scan picks up currently-actionable accounts
  under the existing caps.
* NO N+1: one batched read of the existing ledger rows for the service keys a
  server returned, one in-memory comparison, one bulk write.

State semantics are deliberately NOT re-derived here. A transition is "the value
``normalize_client_state`` returns changed", which is the same function the
dashboard, the subscription page and the mutation responses use, so the surfaces
cannot disagree about what a raw panel response means.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import datetime, timedelta

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import (
    NOTIFICATION_BACKOFF_SECONDS,
    SERVICE_STATE_TO_NOTIFICATION_KIND,
    ServiceNotificationEvent,
    ServiceObservedState,
)
from panel.services import client_state as client_state_service
from panel.services import lifecycle as lifecycle_service

logger = logging.getLogger(__name__)

#: States that are worth telling the customer about, and therefore may open an
#: outbox row. Anything else updates the ledger silently.
NOTIFIABLE_STATES = tuple(SERVICE_STATE_TO_NOTIFICATION_KIND.keys())

#: Canonical service state -> the SMS-monitor vocabulary the messaging layer (and
#: its per-state triggers, cooldowns, templates and manual-review flags) is written
#: in. Those settings were configured per SMS state long before this pipeline
#: existed, so a reminder must be delivered under the same name the operator
#: configured -- a pipeline that translated the state and then silently skipped the
#: trigger check would be worse than the bug it replaces.
SERVICE_STATE_TO_SMS_STATE = {
    'volume_low': 'low_volume',
    'volume_ended': 'ended',
    'expired': 'expired',
    'expiring_soon': 'near_expiry',
}


def sms_state_for(service_state) -> str | None:
    """The SMS-monitor state name for a canonical service state (or None)."""
    return SERVICE_STATE_TO_SMS_STATE.get(str(service_state or ''))


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_naive(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def transition_event_id(service_key: str, state: str, state_version: int) -> str:
    """Deterministic id for one transition: the deduplication barrier.

    The same transition observed twice -- by two pollers, or by the same poller
    after a retry -- produces the same id, so the UNIQUE constraint on
    ``event_id`` turns a race into a no-op instead of a duplicate SMS.
    """
    raw = "%s|%s|%d" % (service_key, state, int(state_version or 0))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
    return "st:%s" % digest


def idempotency_key_for(event) -> str:
    """Stable gateway key for one event; every retry reuses it."""
    key = (event.idempotency_key or "").strip()
    if key:
        return key
    return "depletion-%s" % event.event_id


def observed_dict_from_row(row) -> dict:
    """The comparable state carried by one processed/cached client row.

    Runs the row through the canonical calculator so a transition is computed from
    exactly the values every other surface shows.
    """
    if not isinstance(row, dict):
        return {}
    try:
        state = client_state_service.normalize_client_state(row=row)
    except Exception:
        return {}
    if not isinstance(state, dict) or not state:
        return {}
    return {
        "service_state": state.get("service_state"),
        "service_state_tag": state.get("service_state_tag"),
        "remaining_bytes": _as_int(state.get("remaining_bytes")),
        "total_bytes": _as_int(state.get("total_bytes")),
        "expiry_time": _as_int(state.get("expiry_time")),
        "telemetry_updated_at": _as_naive(state.get("telemetry_updated_at")),
        "config_updated_at": _as_naive(state.get("config_updated_at")),
    }


#: Fields a version is computed from. A moved byte counter is deliberately NOT one
#: of them: the canonical state already folds every threshold that matters (a
#: remaining-bytes change that crosses the low-volume line changes service_state),
#: so versioning raw traffic would make every poll a transition, inflate the ledger
#: and defeat the event deduplication key that depends on the version.
VERSION_FIELDS = ("service_state", "total_bytes", "expiry_time")

#: How often an UNCHANGED observation is still written back. The ledger needs a
#: recent "we looked and it was still fine" stamp, but not one row write per client
#: per poll: at a two-second cadence and a thousand clients that would be hundreds
#: of writes a second to say nothing new.
LEDGER_REFRESH_SECONDS = 60


def is_material_change(previous, current) -> bool:
    """Whether a fresh observation differs enough to bump the state version.

    The version tracks what a reminder is ABOUT: the canonical service state, the
    quota it was measured against, and the expiry. So "ended, still ended" is one
    version while "2 GB left -> 0 GB left" is a new one -- and the second case is
    exactly the reported bug, because 0 GB left is what "volume_ended" means.
    """
    if previous is None:
        return True
    for field in VERSION_FIELDS:
        if previous.get(field) != current.get(field):
            return True
    return False


def _existing_states(service_keys) -> dict:
    """One batched read of the ledger for the keys a server just returned."""
    wanted = [str(key) for key in (service_keys or []) if key]
    if not wanted:
        return {}
    rows = (ServiceObservedState.query
            .filter(ServiceObservedState.service_key.in_(wanted))
            .all())
    return {row.service_key: row for row in rows}


def record_observations(server_id, observations, *, observed_at=None,
                        source="transition", commit=True,
                        notify_baseline=False) -> dict:
    """Reconcile one fresh read of one server against the durable ledger.

    ``observations`` is an iterable of ``(service_key, state_dict, identity)`` where
    ``state_dict`` comes from :func:`observed_dict_from_row` and ``identity`` is an
    optional dict with ``client_uuid`` / ``client_email``.

    Returns counters only -- the caller publishes the snapshot regardless, because
    dashboard freshness must never depend on SMS bookkeeping succeeding.

    notify_baseline is what separates the two callers, and the difference is
    deliberate. A fresh read establishes a silent baseline (the default), so
    introducing the ledger cannot text every already-expired account in the install
    at once. The RECONCILIATION pass sets it, because "make sure nothing was missed"
    has to include the account that was already depleted when the pipeline started --
    and the caps, cooldowns, quiet hours and lifecycle fences that already bounded
    the old scan still bound what that produces.
    """
    moment = observed_at or datetime.utcnow()
    prepared = []
    for entry in observations or ():
        try:
            service_key, state, identity = entry
        except (TypeError, ValueError):
            continue
        if not service_key or not state:
            continue
        prepared.append((str(service_key), state, identity or {}))

    result = {"observed": len(prepared), "baselines": 0, "transitions": 0,
              "events_created": 0, "events_duplicate": 0, "errors": 0}
    if not prepared:
        return result

    try:
        existing = _existing_states([key for key, _state, _ident in prepared])
    except Exception:
        # A ledger read failure must not be mistaken for "nothing observed". The
        # snapshot still publishes and the reconciliation scan repairs later.
        result["errors"] += 1
        return result

    for service_key, state, identity in prepared:
        row = existing.get(service_key)
        try:
            if row is None:
                # FIRST EVER observation: a baseline, never a transition. This is
                # what keeps introducing the ledger from texting every expired
                # account in the install at deploy time.
                row = ServiceObservedState(
                    service_key=service_key,
                    server_id=_as_int(server_id, 0) or 0,
                    client_uuid=(str(identity.get("client_uuid"))
                                 if identity.get("client_uuid") else None),
                    client_email=(str(identity.get("client_email")).lower()
                                  if identity.get("client_email") else None),
                    last_state=state.get("service_state"),
                    last_state_tag=state.get("service_state_tag"),
                    last_remaining_bytes=state.get("remaining_bytes"),
                    last_total_bytes=state.get("total_bytes"),
                    last_expiry_ms=state.get("expiry_time"),
                    last_observed_at=moment,
                    last_telemetry_updated_at=state.get("telemetry_updated_at"),
                    state_version=0,
                    created_at=moment,
                    updated_at=moment,
                )
                db.session.add(row)
                existing[service_key] = row
                result["baselines"] += 1
                if notify_baseline and state.get("service_state") in NOTIFIABLE_STATES:
                    created = _open_event(
                        service_key=service_key, server_id=server_id,
                        identity=identity, state=state, previous_state=None,
                        state_version=0, moment=moment, source=source,
                    )
                    if created:
                        result["events_created"] += 1
                    else:
                        result["events_duplicate"] += 1
                continue

            previous = {
                "service_state": row.last_state,
                "remaining_bytes": row.last_remaining_bytes,
                "total_bytes": row.last_total_bytes,
                "expiry_time": row.last_expiry_ms,
            }
            if not is_material_change(previous, state):
                # Traffic moved but the state did not: refresh the observation stamp
                # only (and only when it is actually stale), and do NOT create a
                # version or an event. This is the write floor that keeps a poll loop
                # from rewriting every row of the install to say nothing new.
                age = None
                if row.last_observed_at is not None:
                    age = (moment - row.last_observed_at).total_seconds()
                if age is not None and age < LEDGER_REFRESH_SECONDS:
                    continue
                row.last_observed_at = moment
                row.last_remaining_bytes = state.get("remaining_bytes")
                row.last_telemetry_updated_at = (
                    state.get("telemetry_updated_at") or row.last_telemetry_updated_at)
                row.updated_at = moment
                continue

            row.state_version = int(row.state_version or 0) + 1
            row.last_state = state.get("service_state")
            row.last_state_tag = state.get("state_tag") or state.get("service_state_tag")
            row.last_remaining_bytes = state.get("remaining_bytes")
            row.last_total_bytes = state.get("total_bytes")
            row.last_expiry_ms = state.get("expiry_time")
            row.last_observed_at = moment
            row.last_telemetry_updated_at = (
                state.get("telemetry_updated_at") or row.last_telemetry_updated_at)
            row.updated_at = moment
            result["transitions"] += 1

            new_state = state.get("service_state")
            if new_state not in NOTIFIABLE_STATES:
                continue
            created = _open_event(
                service_key=service_key, server_id=server_id, identity=identity,
                state=state, previous_state=previous.get("service_state"),
                state_version=int(row.state_version or 0), moment=moment,
                source=source,
            )
            if created:
                result["events_created"] += 1
            else:
                result["events_duplicate"] += 1
        except IntegrityError:
            # A concurrent worker inserted the identical event/row first: its row
            # is the truth and ours is redundant, not an error.
            db.session.rollback()
            result["events_duplicate"] += 1
        except Exception:
            # One bad row must not take the batch down, and a pending transaction
            # must not be left open (the caller continues to publish the snapshot).
            # A rollback discards this batch's uncommitted rows; the next cycle and
            # the reconciliation scan re-derive them, and no event is lost because
            # events are only created from a committed version.
            result["errors"] += 1
            logger.warning("[telemetry] observation failed for %s", service_key,
                           exc_info=True)
            _safe_rollback()
            break

    if commit:
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            result["events_duplicate"] += 1
        except Exception:
            db.session.rollback()
            result["errors"] += 1
            logger.warning("[telemetry] commit failed for server %s", server_id,
                           exc_info=True)
    return result


def _open_event(*, service_key, server_id, identity, state, previous_state,
                state_version, moment, source) -> bool:
    """Insert the outbox row for one transition; False when it already exists.

    The generation is read (never advanced) here and may be None for a service that
    has no lifecycle row yet; the delivery worker establishes the durable baseline
    through the canonical helper before it sends, so no path can post a null
    generation to the gateway.
    """
    new_state = state.get("service_state")
    kind = SERVICE_STATE_TO_NOTIFICATION_KIND.get(new_state)
    if not kind:
        return False
    try:
        generation = lifecycle_service.generation_state(service_key).get("generation")
    except Exception:
        generation = None
    event_id = transition_event_id(service_key, new_state, state_version)
    if ServiceNotificationEvent.query.filter_by(event_id=event_id).first():
        return False
    event = ServiceNotificationEvent(
        event_id=event_id,
        service_key=service_key,
        server_id=_as_int(server_id, 0) or 0,
        client_uuid=(str(identity.get("client_uuid"))
                     if identity.get("client_uuid") else None),
        client_email=(str(identity.get("client_email")).lower()
                      if identity.get("client_email") else None),
        state=new_state,
        previous_state=(previous_state or None),
        notification_kind=kind,
        state_version=int(state_version or 0),
        lifecycle_generation=int(generation or 0),
        observed_at=moment,
        telemetry_updated_at=state.get("telemetry_updated_at"),
        source=str(source or "transition")[:24],
        status="pending",
        attempt_count=0,
        next_attempt_at=moment,
        idempotency_key=("depletion-%s" % event_id)[:160],
        created_at=moment,
        updated_at=moment,
    )
    db.session.add(event)
    return True

# ---------------------------------------------------------------------------
# Delivery: the outbox side of the pipeline
#
# The fetch path only ever INSERTS an event. Everything expensive, fallible or
# policy-dependent -- resolving a phone number, quiet hours, the daily and hourly
# budget, the gateway call -- happens at delivery time, in a worker. That split is
# what lets the detector stay fast and never block a dashboard refresh, and it is
# why a transition that lands during quiet hours survives as a row instead of
# being dropped by a "do not send now" early return.
# ---------------------------------------------------------------------------

#: Statuses a worker may claim. LEASE_STATUS is the lease: a row in that state is
#: owned by a worker and claim_events() never hands it to a second one.
CLAIMABLE_STATUSES = ('pending', 'retry')
LEASE_STATUS = 'sending'
MAX_ATTEMPTS = len(NOTIFICATION_BACKOFF_SECONDS) + 1
#: A leased row older than this belongs to a crashed worker, not to work in
#: progress; reclaiming it is what makes the outbox crash-safe.
LEASE_TIMEOUT_SECONDS = 900


def _safe_rollback() -> None:
    try:
        db.session.rollback()
    except Exception:
        pass


def _as_owner(name=None) -> str:
    base = name or "pid%d:%s" % (os.getpid(), threading.current_thread().name)
    return str(base)[:64]


def event_by_id(event_id):
    if not event_id:
        return None
    return ServiceNotificationEvent.query.filter_by(event_id=str(event_id)).first()


def claim_events(limit: int = 5, *, owner=None, now=None) -> list:
    """Lease up to 'limit' due events. Two workers never receive the same row.

    The claim is one UPDATE guarded by the status it is moving out of, so a
    concurrent claimer's attempt simply does not match and it moves on to the next
    due row. PostgreSQL adds FOR UPDATE SKIP LOCKED, so two workers do not even
    queue behind each other; SQLite serialises writers, which produces the same
    one-winner outcome in the single-writer case the tests run.
    """
    moment = now or datetime.utcnow()
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 5
    owner_id = _as_owner(owner)
    try:
        due_query = (db.session.query(ServiceNotificationEvent.id)
                     .filter(ServiceNotificationEvent.status.in_(CLAIMABLE_STATUSES))
                     .filter(ServiceNotificationEvent.attempt_count < MAX_ATTEMPTS)
                     .filter(or_(ServiceNotificationEvent.next_attempt_at.is_(None),
                                 ServiceNotificationEvent.next_attempt_at <= moment))
                     .order_by(ServiceNotificationEvent.next_attempt_at,
                               ServiceNotificationEvent.id)
                     .limit(limit))
        if db.engine.dialect.name == 'postgresql':
            # Same rows, but locked: a second worker skips them instead of blocking.
            due_query = due_query.with_for_update(skip_locked=True)
        due_ids = [row[0] for row in due_query.all()]
        if not due_ids:
            _safe_rollback()
            return []
        claimed = (db.session.query(ServiceNotificationEvent)
                   .filter(ServiceNotificationEvent.id.in_(due_ids))
                   .filter(ServiceNotificationEvent.status.in_(CLAIMABLE_STATUSES))
                   .update({
                       'status': LEASE_STATUS,
                       'claimed_by': owner_id,
                       'claimed_at': moment,
                       'attempt_count': ServiceNotificationEvent.attempt_count + 1,
                       'last_attempt_at': moment,
                       'updated_at': moment,
                   }, synchronize_session=False))
        db.session.commit()
    except Exception:
        _safe_rollback()
        logger.warning("[telemetry] claim failed", exc_info=True)
        return []
    if not claimed:
        return []
    return (ServiceNotificationEvent.query
            # populate_existing: the claim UPDATE above ran with
            # synchronize_session=False, so the identity map still holds the
            # pre-claim attempt_count. The retry ladder reads that counter, and a
            # stale value would retry forever instead of reaching its last rung.
            .populate_existing()
            .filter(ServiceNotificationEvent.id.in_(due_ids))
            .filter(ServiceNotificationEvent.claimed_by == owner_id)
            .order_by(ServiceNotificationEvent.next_attempt_at,
                      ServiceNotificationEvent.id)
            .all())


def reclaim_expired_leases(*, now=None) -> int:
    """Return a crashed worker's leases to the queue as retries."""
    moment = now or datetime.utcnow()
    cutoff = moment - timedelta(seconds=LEASE_TIMEOUT_SECONDS)
    try:
        rows = (ServiceNotificationEvent.query
                .filter(ServiceNotificationEvent.status == LEASE_STATUS)
                .filter(or_(ServiceNotificationEvent.claimed_at.is_(None),
                            ServiceNotificationEvent.claimed_at <= cutoff))
                .all())
        for row in rows:
            row.status = 'retry'
            row.next_attempt_at = moment
            row.last_error = 'lease_expired'
            row.updated_at = moment
            row.claimed_by = None
            row.claimed_at = None
        if rows:
            db.session.commit()
        return len(rows)
    except Exception:
        _safe_rollback()
        return 0


def mark_sent(event, *, response=None, correlation_id=None, gateway_request_id=None,
              sent_at=None) -> None:
    moment = sent_at or datetime.utcnow()
    event.status = 'sent'
    event.sent_at = moment
    event.updated_at = moment
    event.last_error = None
    event.claimed_by = None
    event.claimed_at = None
    if correlation_id:
        event.correlation_id = str(correlation_id)[:64]
    if gateway_request_id:
        event.gateway_request_id = str(gateway_request_id)[:128]
    if isinstance(response, dict):
        event.last_status_code = _as_int(response.get('status_code'))
    db.session.commit()


def mark_skipped(event, reason, *, retry_in=None, status_code=None) -> None:
    """Close an event that must not be sent, or defer it without losing it.

    'retry_in' is a DEFERRAL, not a decision: quiet hours and an exhausted daily or
    hourly budget move the event to the moment it may be sent instead of dropping
    the transition that created it.
    """
    moment = datetime.utcnow()
    event.last_error = str(reason or 'skipped')[:255]
    event.last_status_code = _as_int(status_code)
    event.updated_at = moment
    event.claimed_by = None
    event.claimed_at = None
    if retry_in is not None:
        delay = max(1.0, float(retry_in))
        event.status = 'retry'
        event.next_attempt_at = moment + timedelta(seconds=delay)
    else:
        event.status = 'skipped'
        event.next_attempt_at = None
    db.session.commit()


def mark_retry(event, reason, *, now=None) -> int:
    """Bounded exponential retry; returns the delay in seconds (0 == terminal)."""
    moment = now or datetime.utcnow()
    attempt = int(event.attempt_count or 1)
    if attempt >= MAX_ATTEMPTS:
        event.status = 'failed_terminal'
        event.next_attempt_at = None
    else:
        index = min(max(0, attempt - 1), len(NOTIFICATION_BACKOFF_SECONDS) - 1)
        event.status = 'retry'
        event.next_attempt_at = moment + timedelta(
            seconds=NOTIFICATION_BACKOFF_SECONDS[index])
    event.last_error = str(reason or 'retry')[:255]
    event.updated_at = moment
    event.claimed_by = None
    event.claimed_at = None
    db.session.commit()
    if event.status == 'failed_terminal':
        return 0
    return int(max(0.0, (event.next_attempt_at - moment).total_seconds()))


def mark_superseded(event, reason, *, now=None) -> None:
    moment = now or datetime.utcnow()
    event.status = 'superseded'
    event.superseded_reason = str(reason or 'superseded')[:64]
    event.superseded_at = moment
    event.updated_at = moment
    event.next_attempt_at = None
    event.claimed_by = None
    event.claimed_at = None
    db.session.commit()


def supersede_pending(service_key, reason, *, exclude_event_id=None,
                      max_generation=None, now=None) -> int:
    """Retire queued notifications for a service whose lifecycle has moved on.

    Called from the renewal path: the moment a service is renewed, every pending
    reminder about the OLD lifecycle is obsolete. The delivery worker re-reads the
    generation before it sends (the second fence); this is the first, and it also
    retires a row a worker has already leased.
    """
    moment = now or datetime.utcnow()
    query = (db.session.query(ServiceNotificationEvent)
             .filter(ServiceNotificationEvent.service_key == str(service_key))
             .filter(ServiceNotificationEvent.status.in_(
                 CLAIMABLE_STATUSES + (LEASE_STATUS,))))
    if exclude_event_id:
        query = query.filter(ServiceNotificationEvent.event_id != str(exclude_event_id))
    if max_generation is not None:
        query = query.filter(
            ServiceNotificationEvent.lifecycle_generation <= int(max_generation))
    try:
        rows = query.all()
        for row in rows:
            row.status = 'superseded'
            row.superseded_reason = str(reason or 'lifecycle_advanced')[:64]
            row.superseded_at = moment
            row.updated_at = moment
            row.next_attempt_at = None
            row.claimed_by = None
            row.claimed_at = None
        if rows:
            db.session.commit()
        return len(rows)
    except Exception:
        _safe_rollback()
        logger.warning("[telemetry] supersede failed for %s", service_key, exc_info=True)
        return 0


def pending_events(service_key=None, *, limit=20) -> list:
    query = (db.session.query(ServiceNotificationEvent)
             .filter(ServiceNotificationEvent.status.in_(
                 CLAIMABLE_STATUSES + (LEASE_STATUS,))))
    if service_key:
        query = query.filter(ServiceNotificationEvent.service_key == str(service_key))
    return (query.order_by(ServiceNotificationEvent.next_attempt_at,
                           ServiceNotificationEvent.id)
            .limit(max(1, int(limit))).all())


def backlog(*, limit=50):
    """Events whose delivery is due now -- what the reconciliation pass repairs."""
    moment = datetime.utcnow()
    return (db.session.query(ServiceNotificationEvent)
            .filter(ServiceNotificationEvent.status.in_(CLAIMABLE_STATUSES))
            .filter(or_(ServiceNotificationEvent.next_attempt_at.is_(None),
                        ServiceNotificationEvent.next_attempt_at <= moment))
            .order_by(ServiceNotificationEvent.next_attempt_at,
                      ServiceNotificationEvent.id)
            .limit(max(1, int(limit))).all())


def metrics(*, now=None) -> dict:
    """PII-free counters for the doctor page: never a phone number or a message."""
    moment = now or datetime.utcnow()
    try:
        rows = (db.session.query(ServiceNotificationEvent.status,
                                 func.count(ServiceNotificationEvent.id))
                .group_by(ServiceNotificationEvent.status).all())
        by_status = {str(status): int(count) for status, count in rows}
        oldest = (db.session.query(func.min(ServiceNotificationEvent.created_at))
                  .filter(ServiceNotificationEvent.status.in_(
                      CLAIMABLE_STATUSES + (LEASE_STATUS,)))
                  .scalar())
        observed = db.session.query(func.count(ServiceObservedState.id)).scalar() or 0
        overdue = (db.session.query(func.count(ServiceNotificationEvent.id))
                   .filter(ServiceNotificationEvent.status.in_(CLAIMABLE_STATUSES))
                   .filter(or_(ServiceNotificationEvent.next_attempt_at.is_(None),
                               ServiceNotificationEvent.next_attempt_at <= moment))
                   .scalar() or 0)
    except Exception:
        _safe_rollback()
        return {'available': False}
    age = None
    if oldest is not None:
        age = max(0.0, (moment - oldest).total_seconds())
    # Everything an operator needs to tell "quiet" from "broken", and nothing that
    # could identify a customer: counts, ages and a status histogram.
    return {
        'available': True,
        'observed_services': int(observed),
        'by_status': by_status,
        'pending': sum(by_status.get(key, 0)
                       for key in CLAIMABLE_STATUSES + (LEASE_STATUS,)),
        'pending_only': int(by_status.get('pending', 0)),
        'retry': int(by_status.get('retry', 0)),
        'leased': int(by_status.get(LEASE_STATUS, 0)),
        'sent': int(by_status.get('sent', 0)),
        'skipped': int(by_status.get('skipped', 0)),
        'superseded': int(by_status.get('superseded', 0)),
        'shadowed': int(by_status.get('shadowed', 0)),
        'failed_terminal': int(by_status.get('failed_terminal', 0)),
        'oldest_pending_age_seconds': (round(age, 1) if age is not None else None),
        'overdue': int(overdue),
        'retry_exhausted': int(by_status.get('failed_terminal', 0)),
        'last_detected_at': _iso(_scalar(
            db.session.query(func.max(ServiceNotificationEvent.created_at)))),
        'last_sent_at': _iso(_scalar(
            db.session.query(func.max(ServiceNotificationEvent.sent_at)))),
        'last_superseded_at': _iso(_scalar(
            db.session.query(func.max(ServiceNotificationEvent.superseded_at)))),
        'last_reconciliation_at': _iso(_scalar(
            db.session.query(func.max(ServiceNotificationEvent.created_at))
            .filter(ServiceNotificationEvent.source == 'reconciliation'))),
        'max_attempts': MAX_ATTEMPTS,
        'backoff_seconds': list(NOTIFICATION_BACKOFF_SECONDS),
    }


def _scalar(query):
    try:
        return query.scalar()
    except Exception:
        _safe_rollback()
        return None


def _iso(value):
    return (value.isoformat() + 'Z') if isinstance(value, datetime) else None
