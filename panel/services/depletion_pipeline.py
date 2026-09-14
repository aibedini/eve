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
from datetime import datetime

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




# ---------------------------------------------------------------------------
# Health: what the doctor page needs to tell "quiet" from "broken"
#
# The pipeline spent an afternoon looking healthy while the detector recorded
# nothing (a wiring bug) and while six of eight panels could not be fetched at all
# (a policy flag that never reached the fetcher). Counters alone did not show it, so
# the surface below adds two things a counter cannot express: LIVENESS (when did
# each stage last actually run) and COVERAGE (is every enabled panel being observed).
# ---------------------------------------------------------------------------

HEARTBEAT_PREFIX = 'eve:depletion:heartbeat:'
LAST_DETECTION_KEY = 'eve:depletion:last_detection'
LAST_DELIVERY_KEY = 'eve:depletion:last_delivery'
LAST_RECONCILIATION_KEY = 'eve:depletion:last_reconciliation'
HEARTBEAT_TTL_SECONDS = 90
#: A panel whose telemetry is older than this cannot be trusted to detect a
#: transition "in real time", so it is reported as stale rather than covered.
PANEL_FRESH_SECONDS = 300
#: Backlog older than this means the outbox is not draining.
OUTBOX_AGE_WARN_SECONDS = 900
#: A reconciliation run that repairs more than this many missed transitions is a
#: signal that live detection is not keeping up (or was down).
RECONCILIATION_BURST_WARN = 25

_heartbeats = {}


def _now_iso():
    return datetime.utcnow().isoformat() + 'Z'


def _mark(key, value=None):
    """Record liveness for one stage in Redis (shared) and in this process."""
    stamp = value or _now_iso()
    _heartbeats[key] = stamp
    client = _redis()
    if client is None:
        return stamp
    try:
        client.set(key, stamp, ex=HEARTBEAT_TTL_SECONDS * 8)
    except Exception:
        pass
    return stamp


def _read_marks():
    marks = dict(_heartbeats)
    client = _redis()
    if client is None:
        return marks
    for key in (LAST_DETECTION_KEY, LAST_DELIVERY_KEY, LAST_RECONCILIATION_KEY,
                HEARTBEAT_PREFIX + 'delivery_worker'):
        try:
            raw = client.get(key)
        except Exception:
            continue
        if raw is None:
            continue
        marks[key] = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
    return marks


def note_detection(at=None):
    return _mark(LAST_DETECTION_KEY, at)


def note_delivery(at=None):
    return _mark(LAST_DELIVERY_KEY, at)


def note_reconciliation(at=None):
    return _mark(LAST_RECONCILIATION_KEY, at)


def note_worker_heartbeat():
    """Called every delivery-worker tick; its absence is the loudest alarm."""
    return _mark(HEARTBEAT_PREFIX + 'delivery_worker')


def _redis():
    try:
        from panel.core.redis_client import get_redis
        return get_redis()
    except Exception:
        return None


def _age_seconds(stamp, now=None):
    if not stamp:
        return None
    moment = now or datetime.utcnow()
    try:
        parsed = datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None
    return max(0.0, (moment - parsed).total_seconds())


def panel_coverage(*, now=None, max_age_seconds=None) -> dict:
    """Per-panel telemetry coverage: enabled, reachable, fresh, refused, stale.

    "Enabled" comes from the database; "reachable" and the error text come from the
    snapshot's server statuses; "fresh" is the age of the panel's telemetry stamp.
    A panel that is enabled but not fresh is NOT covered, and that is the whole
    point: one unreachable panel must not be hidden behind a healthy average.
    """
    from panel.core.redis_client import GLOBAL_SERVER_DATA
    from panel.models import Server
    limit = int(max_age_seconds or PANEL_FRESH_SECONDS)
    moment = now or datetime.utcnow()
    rows = {}
    try:
        enabled = Server.query.filter_by(enabled=True).all()
    except Exception:
        enabled = []
    for server in enabled:
        rows[int(server.id)] = {
            'server_id': int(server.id),
            'name': (server.name or '')[:64],
            'secure_transport': str(server.host or '').lower().startswith('https://'),
            'allow_insecure': bool(getattr(server, 'allow_insecure', False)),
            'reachable': None, 'error': None, 'telemetry_age_seconds': None,
            'fresh': False, 'covered': False,
        }
    statuses = GLOBAL_SERVER_DATA.get('servers_status') or []
    for status in statuses:
        if not isinstance(status, dict):
            continue
        sid = _as_int(status.get('server_id'), None)
        if sid is None or sid not in rows:
            continue
        rows[sid]['reachable'] = bool(status.get('success'))
        error = status.get('error') or status.get('reachable_error') or ''
        rows[sid]['error'] = (str(error)[:160] or None)
    newest = {}
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        sid = _as_int((inbound or {}).get('server_id'), None)
        if sid is None:
            continue
        for client in (inbound.get('clients') or ()):
            stamp = client.get('telemetry_updated_at') or client.get('config_updated_at')
            if not stamp:
                continue
            age = _age_seconds(stamp, now=moment)
            if age is None:
                continue
            if sid not in newest or age < newest[sid]:
                newest[sid] = age
    for sid, row in rows.items():
        age = newest.get(sid)
        row['telemetry_age_seconds'] = (round(age, 1) if age is not None else None)
        row['fresh'] = age is not None and age <= limit
        row['covered'] = bool(row['fresh'] and row['reachable'] is not False)
    enabled_count = len(rows)
    covered = sum(1 for row in rows.values() if row['covered'])
    refused = [row for row in rows.values()
               if (row['error'] or '').startswith('Refusing to send panel credentials')]
    return {
        'enabled': enabled_count,
        'covered': covered,
        'uncovered': enabled_count - covered,
        'refused_transport': len(refused),
        'unreachable': sum(1 for row in rows.values() if row['reachable'] is False),
        'stale': sum(1 for row in rows.values()
                     if row['reachable'] is not False and not row['fresh']),
        'fresh_seconds_limit': limit,
        'panels': [rows[sid] for sid in sorted(rows)],
    }


def warnings(*, coverage=None, outbox=None, now=None, marks=None) -> list:
    """Operator-facing warnings. Each one is a named condition, never a guess."""
    coverage = coverage if coverage is not None else panel_coverage(now=now)
    outbox = outbox if outbox is not None else telemetry_state.metrics(now=now)
    marks = marks if marks is not None else _read_marks()
    warning_list = []
    if mode() == 'off':
        warning_list.append({
            'code': 'pipeline_off',
            'detail': 'the transition detector is disabled; only the periodic scan sends'})
    heartbeat_age = _age_seconds(marks.get(HEARTBEAT_PREFIX + 'delivery_worker'), now=now)
    if heartbeat_age is None or heartbeat_age > HEARTBEAT_TTL_SECONDS * 2:
        warning_list.append({
            'code': 'worker_heartbeat_missing',
            'detail': 'no delivery-worker tick within %ss' % (HEARTBEAT_TTL_SECONDS * 2)})
    if outbox.get('available') is False:
        warning_list.append({'code': 'outbox_unavailable',
                             'detail': 'the notification ledger could not be read'})
    else:
        age = outbox.get('oldest_pending_age_seconds')
        if age is not None and age > OUTBOX_AGE_WARN_SECONDS:
            warning_list.append({
                'code': 'outbox_backlog_age',
                'detail': 'oldest pending notification is %.0fs old' % age})
        if outbox.get('overdue'):
            warning_list.append({
                'code': 'outbox_overdue',
                'detail': '%s notification(s) are due now and waiting' % outbox['overdue']})
        if outbox.get('failed_terminal'):
            warning_list.append({
                'code': 'notification_retry_exhausted',
                'detail': '%s notification(s) exhausted the retry ladder'
                          % outbox['failed_terminal']})
    if _redis() is None:
        warning_list.append({
            'code': 'redis_unavailable',
            'detail': 'shared watch marks and fetch tickets fall back to per-process state'})
    if coverage.get('refused_transport'):
        warning_list.append({
            'code': 'panel_transport_refused',
            'detail': '%s panel(s) are refused by the transport policy and cannot be observed'
                      % coverage['refused_transport']})
    if coverage.get('unreachable'):
        warning_list.append({
            'code': 'panel_unreachable',
            'detail': '%s panel(s) failed their last fetch' % coverage['unreachable']})
    if coverage.get('stale'):
        warning_list.append({
            'code': 'panel_telemetry_stale',
            'detail': '%s panel(s) have telemetry older than %ss'
                      % (coverage['stale'], coverage['fresh_seconds_limit'])})
    detected_age = _age_seconds(marks.get(LAST_DETECTION_KEY), now=now)
    if detected_age is not None and detected_age > 3600 and not coverage.get('uncovered'):
        warning_list.append({
            'code': 'no_recent_detection',
            'detail': 'no transition observed in the last hour'})
    return warning_list


def health(*, now=None) -> dict:
    """The whole surface: state, coverage, liveness and warnings, PII-free."""
    outbox = telemetry_state.metrics(now=now)
    coverage = panel_coverage(now=now)
    marks = _read_marks()
    warning_list = warnings(coverage=coverage, outbox=outbox, now=now, marks=marks)
    if not outbox.get('available'):
        state = 'error'
    elif any(w['code'] in ('worker_heartbeat_missing', 'outbox_unavailable',
                           'panel_transport_refused', 'notification_retry_exhausted')
             for w in warning_list):
        state = 'degraded'
    elif warning_list:
        state = 'warning'
    else:
        state = 'ok'
    moment = now or datetime.utcnow()
    return {
        'state': state,
        'mode': mode(),
        'detection_enabled': detection_enabled(),
        'delivery_enabled': delivery_enabled(),
        'legacy_sender_active': legacy_sender_active(),
        'outbox': outbox,
        'coverage': coverage,
        'liveness': {
            'worker_heartbeat_at': marks.get(HEARTBEAT_PREFIX + 'delivery_worker'),
            'worker_heartbeat_age_seconds': _age_seconds(
                marks.get(HEARTBEAT_PREFIX + 'delivery_worker'), now=moment),
            'last_detection_at': marks.get(LAST_DETECTION_KEY),
            'last_detection_age_seconds': _age_seconds(marks.get(LAST_DETECTION_KEY), now=moment),
            'last_delivery_at': marks.get(LAST_DELIVERY_KEY),
            'last_delivery_age_seconds': _age_seconds(marks.get(LAST_DELIVERY_KEY), now=moment),
            'last_reconciliation_at': marks.get(LAST_RECONCILIATION_KEY),
            'last_reconciliation_age_seconds': _age_seconds(
                marks.get(LAST_RECONCILIATION_KEY), now=moment),
        },
        'warnings': warning_list,
    }


def status() -> dict:
    """Doctor-facing block. Counters, ages and panel coverage -- never customer data."""
    return health()
