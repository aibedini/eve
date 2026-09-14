"""Rollout switch and reconciliation net for the depletion transition pipeline.

Two halves of one policy live here, both deliberately outside the fetcher and the
SMS worker so neither has to import the other:

* MODE. EVE_DEPLETION_EVENT_PIPELINE selects who may SEND a depletion reminder:

  - off    -- the periodic SMS scan is still the only sender (pre-migration
              behaviour, kept as the escape hatch for a bad rollout).
  - shadow -- the pipeline detects transitions and writes outbox rows, but
              records what it WOULD have sent instead of sending it, while the
              scan keeps sending. This is how the new detector is proven against
              production traffic without risking a duplicate message.
  - on     -- the pipeline sends, and the scan only reconciles.

  Whichever mode is selected, exactly one of the two paths is allowed to call the
  gateway for a given logical transition. That is the invariant the flag exists to
  protect: a rollout must never double-text a customer.

* RECONCILIATION. The scan used to be the detector, and that was the bug: it only
  asked "which accounts look depleted RIGHT NOW", so a transition that happened
  between two scans (or while the snapshot was stale) had no event to send. The
  net below is the opposite direction -- it takes the current snapshot, records
  every service it can see into the durable ledger, and lets the ledger emit the
  transitions that were missed while nobody was watching. A transition the fetcher
  already recorded deduplicates on its event id, so the net repairs gaps and never
  duplicates work.
"""
from __future__ import annotations

import logging
import os

from panel.services import lifecycle as lifecycle_service
from panel.services import telemetry_state

logger = logging.getLogger(__name__)

MODES = ('off', 'shadow', 'on')
DEFAULT_MODE = 'on'


def mode() -> str:
    """The configured rollout mode, normalised. Unknown values fail safe to off."""
    raw = (os.environ.get('EVE_DEPLETION_EVENT_PIPELINE') or '').strip().lower()
    if not raw:
        return DEFAULT_MODE
    return raw if raw in MODES else 'off'


def detection_enabled() -> bool:
    """Whether fresh telemetry should be recorded as transitions."""
    return mode() in ('shadow', 'on')


def delivery_enabled() -> bool:
    """Whether the outbox worker may actually call the gateway."""
    return mode() == 'on'


def shadow_mode() -> bool:
    return mode() == 'shadow'


def legacy_sender_active() -> bool:
    """Whether the periodic scan still owns sending (off and shadow)."""
    return mode() in ('off', 'shadow')


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def observations_from_inbounds(inbounds, *, server_id=None):
    """Build (service_key, state, identity) triples from processed client rows.

    The rows are the ones the snapshot holds, i.e. the output of the ONE canonical
    calculator, so a transition derived here cannot disagree with the badge the
    dashboard renders from the same row.
    """
    observations = []
    seen = set()
    for inbound in (inbounds or ()):
        if not isinstance(inbound, dict):
            continue
        sid = _as_int(inbound.get('server_id'), None)
        if sid is None and server_id is not None:
            sid = _as_int(server_id, None)
        if sid is None:
            continue
        for client in (inbound.get('clients') or ()):
            if not isinstance(client, dict):
                continue
            email = str(client.get('email') or '').strip()
            email_l = email.lower()
            if not email_l:
                continue
            key = (sid, email_l)
            if key in seen:
                continue
            seen.add(key)
            state = telemetry_state.observed_dict_from_row(client)
            if not state:
                continue
            service_key = lifecycle_service.service_key_for_client(
                sid, client, email=email)
            identity = {
                'client_uuid': lifecycle_service.resolve_client_uuid(client),
                'client_email': email_l,
            }
            observations.append((service_key, state, identity))
    return observations


def reconcile_inbounds(inbounds, *, source='reconciliation', observed_at=None,
                       server_id=None, commit=True, notify_baseline=True) -> dict:
    """Record every service in a snapshot block into the durable ledger.

    A first sighting normally establishes a silent baseline, but a reconciliation
    pass asks a different question -- "is anything missing?" -- so it may raise an
    event for a service that is ALREADY actionable: an account that was depleted
    before this pipeline existed, or whose transition was lost to an outage. That is
    not a deploy-time storm, because delivery still runs every existing gate
    (enabled trigger, cooldown, quiet hours, daily and hourly budget, reseller and
    opt-out rules, lifecycle generation). The outcome matches what the old scan would
    have sent, with an audit trail it never had.
    """
    observations = observations_from_inbounds(inbounds, server_id=server_id)
    if not observations:
        return {'observed': 0, 'baselines': 0, 'transitions': 0,
                'events_created': 0, 'events_duplicate': 0, 'errors': 0}
    try:
        return telemetry_state.record_observations(
            server_id, observations, observed_at=observed_at, source=source,
            commit=commit, notify_baseline=notify_baseline)
    except Exception:
        logger.warning('[telemetry] reconciliation failed', exc_info=True)
        return {'observed': len(observations), 'baselines': 0, 'transitions': 0,
                'events_created': 0, 'events_duplicate': 0, 'errors': 1}


def reconcile_snapshot(*, source='reconciliation', observed_at=None) -> dict:
    """Reconcile the shared snapshot, one server block at a time."""
    from panel.core.redis_client import GLOBAL_SERVER_DATA
    inbounds = list(GLOBAL_SERVER_DATA.get('inbounds') or [])
    if not inbounds:
        return {'observed': 0, 'baselines': 0, 'transitions': 0,
                'events_created': 0, 'events_duplicate': 0, 'errors': 0,
                'servers': 0}
    totals = {'observed': 0, 'baselines': 0, 'transitions': 0,
              'events_created': 0, 'events_duplicate': 0, 'errors': 0,
              'servers': 0}
    for inbound in inbounds:
        sid = _as_int((inbound or {}).get('server_id'), 0)
        if not sid:
            continue
        totals['servers'] += 1
        result = reconcile_inbounds([inbound], source=source,
                                    observed_at=observed_at, server_id=sid,
                                    commit=True)
        for key in ('observed', 'baselines', 'transitions', 'events_created',
                    'events_duplicate', 'errors'):
            totals[key] += int(result.get(key) or 0)
    return totals


def status() -> dict:
    """Doctor-facing block. Counters only -- never a phone number or a message."""
    from panel.services import telemetry_state as _telemetry
    return {
        'mode': mode(),
        'detection_enabled': detection_enabled(),
        'delivery_enabled': delivery_enabled(),
        'legacy_sender_active': legacy_sender_active(),
        'outbox': _telemetry.metrics(),
    }
