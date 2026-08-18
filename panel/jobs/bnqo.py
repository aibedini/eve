"""BNQO status/detection engine, retention rollup, Telegram alerts, worker.

Contract: docs/bnqo/EVE_API_CONTRACT.md §5 (status model) and RFP_DIGEST §8/§9
(detection thresholds, evidence). Runs on a 15 s tick inside the background
process (singleton ``bnqo_scheduler``, see panel/jobs/schedulers.py); tests may
call ``bnqo_scheduler_tick()`` directly with DISABLE_BACKGROUND_THREADS set.
"""
import json
import secrets
import time
from datetime import datetime, timedelta
from statistics import median

from panel.extensions import db
from panel.jobs.messaging import _notification_bot_for_reseller
from panel.models import (
    Admin,
    BNQO_DIRECTIONS,
    BnqoIncident,
    BnqoJob,
    BnqoLink,
    BnqoMeasurement,
    BnqoRollup,
    BnqoRoute,
    BnqoServiceProbe,
)

BNQO_WORKER_POLL_SECONDS = 15
STATUS_WINDOWS = 3                     # rules evaluate the last 3 windows
BASELINE_WINDOWS = 100                 # rtt baseline = median p95 of last 100
AGENT_SILENCE_SECONDS = 180            # agent silent > 3 min ⇒ agent-unhealthy
RAW_RETENTION_DAYS = 14                # raw → hourly rollup past this age
ROLLUP_ROW_BATCH = 2000                # raw rows folded per tick (idempotent)
DIAG_COOLDOWN_MINUTES = 15             # min gap between auto RUN_MTR per link
MTR_JOB_TTL_MINUTES = 10
MTR_JOB_CYCLES = 10

# Most severe last; overall link status = worst per-direction status.
_SEVERITY = [
    'healthy',
    'degraded',
    'clock-unsynchronized',
    'service-failed',
    'probe-blocked',
    'critical',
    'unreachable',
]

# Incident kinds the engine opens AND auto-resolves when the condition clears.
# (route_change incidents stay open until an operator resolves them.)
_STATUS_INCIDENT_KINDS = {
    'unreachable': 'link_down',
    'critical': 'packet_loss',
    'service-failed': 'service_failure',
    'agent-unhealthy': 'agent_offline',
    'telemetry-delayed': 'telemetry_stale',
}
_CRITICAL_STATUSES = ('critical', 'unreachable')


def enqueue_diagnostic_mtr(link, cycles=MTR_JOB_CYCLES, ttl_minutes=MTR_JOB_TTL_MINUTES):
    """Create RUN_MTR jobs for both agents of ``link`` (unsigned rows; the
    signature is added per-fetch in the jobs API). Bumps both agents'
    config_version. Caller commits. Returns the created BnqoJob list."""
    now = datetime.utcnow()
    jobs = []
    for agent, peer in ((link.agent_a, link.agent_b), (link.agent_b, link.agent_a)):
        if agent is None or peer is None or not peer.address:
            continue
        agent.config_version = (agent.config_version or 0) + 1
        job = BnqoJob(
            job_id='job_' + secrets.token_hex(16),
            agent_id=agent.id,
            type='RUN_MTR',
            params_json=json.dumps({
                'link_id': link.id,
                'target': peer.address,
                'cycles': cycles,
            }, ensure_ascii=False),
            expires_at=now + timedelta(minutes=ttl_minutes),
            config_version=agent.config_version,
            status='pending',
        )
        db.session.add(job)
        jobs.append(job)
    return jobs


def bnqo_send_telegram_alert(text):
    """Deliver a BNQO alert to every enabled global admin via the central bot.
    Same delivery machinery as the pulse alerts (panel/jobs/schedulers.py)."""
    from app import _telegram_bot_api_client, app  # deferred: app-level helper, avoids circular import
    if not (text or '').strip():
        return
    bot = _notification_bot_for_reseller(None)
    if bot is None:
        return
    api = _telegram_bot_api_client(bot)
    for admin in Admin.query.filter_by(enabled=True).all():
        role = str(admin.role or '').lower()
        if not (admin.is_superadmin or role in ('admin', 'superadmin')):
            continue
        try:
            chat_id = int(str(admin.telegram_id or '').strip())
            if chat_id <= 0:
                continue
        except (TypeError, ValueError):
            continue
        try:
            api.send_message(chat_id, text)
        except Exception as exc:
            app.logger.warning('[bnqo] alert to admin %s failed: %s', admin.id, exc)


def _recent_windows(link_id, direction, limit, source='udp'):
    return (BnqoMeasurement.query
            .filter_by(link_id=link_id, direction=direction, source=source)
            .order_by(BnqoMeasurement.window_start.desc())
            .limit(limit)
            .all())


def _latest_icmp(link_id, direction):
    return (BnqoMeasurement.query
            .filter_by(link_id=link_id, direction=direction, source='icmp')
            .order_by(BnqoMeasurement.window_start.desc())
            .first())


def _service_targets_failing(link, now, window_sec):
    """Latest probe per configured target; returns the failing target names."""
    profile = link.profile()
    targets = [t.get('name') for t in profile.get('service_targets') or [] if t.get('name')]
    if not targets:
        return []
    cutoff = now - timedelta(seconds=max(2 * window_sec, 120))
    failing = []
    for name in targets:
        probe = (BnqoServiceProbe.query
                 .filter_by(link_id=link.id, target_name=name)
                 .order_by(BnqoServiceProbe.created_at.desc())
                 .first())
        if probe is not None and not probe.ok and probe.created_at >= cutoff:
            failing.append(name)
    return failing


def _direction_status(link, direction, now):
    """Evaluate one direction over the last STATUS_WINDOWS windows (§5 rules).

    Returns (status, evidence-dict).
    """
    rows = _recent_windows(link.id, direction, STATUS_WINDOWS)
    if not rows:
        return 'unknown', {'direction': direction, 'reason': 'no_data'}

    losses = [float(row.loss_pct or 0.0) for row in rows]
    latest = rows[0]
    avg_loss = sum(losses) / len(losses)
    evidence = {
        'direction': direction,
        'windows': len(rows),
        'loss_pct_latest': losses[0],
        'loss_pct_avg': round(avg_loss, 3),
    }

    # Complete loss: unreachable — unless ICMP still answers (probe-blocked).
    if losses[0] >= 100.0:
        icmp = _latest_icmp(link.id, direction)
        icmp_alive = (icmp is not None and (icmp.loss_pct or 0.0) < 100.0
                      and icmp.window_start >= now - timedelta(seconds=3 * link.profile()['window_sec']))
        if icmp_alive:
            evidence['icmp'] = {'loss_pct': icmp.loss_pct, 'rtt_avg_ms': icmp.rtt_avg_ms}
            return 'probe-blocked', evidence
        return 'unreachable', evidence

    # UDP path is alive; a failing service target means the network is fine
    # but the destination service is not.
    if avg_loss < 5.0:
        failing = _service_targets_failing(link, now, link.profile()['window_sec'])
        if failing:
            evidence['failing_targets'] = failing
            return 'service-failed', evidence

    # Repeated micro-outages within the evaluation window.
    if sum(1 for loss in losses if loss >= 100.0) >= 2:
        evidence['micro_outages'] = sum(1 for loss in losses if loss >= 100.0)
        return 'critical', evidence
    if avg_loss >= 5.0:
        return 'critical', evidence

    # Clock sync quality: OWD is untrustworthy with invalid clocks.
    if any(row.clock_quality == 'invalid' for row in rows):
        evidence['clock_quality'] = 'invalid'
        return 'clock-unsynchronized', evidence

    # Latency regression vs baseline (median rtt_p95 of the last 100 windows).
    baseline_rows = _recent_windows(link.id, direction, BASELINE_WINDOWS)
    baseline_values = [row.rtt_p95_ms for row in baseline_rows if row.rtt_p95_ms is not None]
    p95_values = [row.rtt_p95_ms for row in rows if row.rtt_p95_ms is not None]
    if len(baseline_values) >= 5 and p95_values:
        baseline = median(baseline_values)
        current_p95 = sum(p95_values) / len(p95_values)
        evidence['rtt_p95_current'] = round(current_p95, 3)
        evidence['rtt_p95_baseline'] = round(baseline, 3)
        if current_p95 > baseline * 1.5:
            evidence['latency_regression'] = True
            return 'degraded', evidence

    if avg_loss >= 1.0:
        return 'degraded', evidence
    return 'healthy', evidence


def _worst(statuses):
    return max(statuses, key=lambda s: _SEVERITY.index(s) if s in _SEVERITY else -1)


def _open_incident(link, kind, direction, evidence, now, alert=True, dedup_open=True):
    """Open an incident unless an identical open/ack one exists; returns it."""
    existing = None
    if dedup_open:
        existing = (BnqoIncident.query
                    .filter_by(link_id=link.id, kind=kind, direction=direction)
                    .filter(BnqoIncident.status.in_(['open', 'ack']))
                    .first())
    if existing is not None:
        existing.evidence_json = json.dumps(evidence, ensure_ascii=False)
        return existing
    incident = BnqoIncident(
        link_id=link.id,
        direction=direction,
        kind=kind,
        status='open',
        evidence_json=json.dumps(evidence, ensure_ascii=False),
        opened_at=now,
    )
    db.session.add(incident)
    db.session.flush()
    if alert:
        lines = [
            f'🛰 BNQO incident #{incident.id} — {link.name}',
            f'kind: {kind}' + (f' | direction: {direction}' if direction else ''),
        ]
        for key in ('loss_pct_latest', 'loss_pct_avg', 'rtt_p95_current',
                    'rtt_p95_baseline', 'failing_targets', 'reason', 'agent'):
            if key in evidence:
                lines.append(f'{key}: {evidence[key]}')
        try:
            bnqo_send_telegram_alert('\n'.join(lines))
        except Exception as exc:
            app.logger.warning('[bnqo] telegram alert failed: %s', exc)
    return incident


def _recent_diag_job_exists(link, now):
    """15-min auto-diagnostics cooldown: any RUN_MTR for this link recently?"""
    cutoff = now - timedelta(minutes=DIAG_COOLDOWN_MINUTES)
    jobs = (BnqoJob.query
            .filter(BnqoJob.agent_id.in_([link.agent_a_id, link.agent_b_id]),
                    BnqoJob.type == 'RUN_MTR',
                    BnqoJob.created_at >= cutoff)
            .all())
    return any(job.params().get('link_id') == link.id for job in jobs)


def _evaluate_link(link, now):
    """Recompute one link's status/status_detail and reconcile its incidents."""
    profile = link.profile()
    window_sec = max(5, int(profile.get('window_sec') or 30))
    detail = {'checked_at': now.isoformat() + 'Z'}
    active = set()  # {(kind, direction)} conditions currently true

    # Agent liveness: either agent silent > 3 min ⇒ agent-unhealthy.
    silent = [agent for agent in (link.agent_a, link.agent_b)
              if agent is None or agent.last_seen_at is None
              or (now - agent.last_seen_at).total_seconds() > AGENT_SILENCE_SECONDS]
    if silent:
        detail['agent'] = {'silent': [agent.name if agent else '?' for agent in silent]}
        status = 'agent-unhealthy'
        active.add(('agent_offline', None))
        evidence = {'reason': 'agent_silent',
                    'agent': ', '.join(agent.name if agent else '?' for agent in silent)}
        active_evidence = {('agent_offline', None): evidence}
        direction_details = {}
    else:
        last_data_at = link.last_data_at
        age = (now - last_data_at).total_seconds() if last_data_at else None
        if age is None or age > 3 * window_sec:
            # No data in the last 3 windows ⇒ unknown (never healthy).
            status = 'unknown'
            detail['freshness'] = {'last_data_age_sec': age, 'reason': 'no_recent_data'}
            direction_details = {}
            active_evidence = {}
        elif age > 2 * window_sec:
            status = 'telemetry-delayed'
            active.add(('telemetry_stale', None))
            active_evidence = {('telemetry_stale', None):
                               {'reason': 'telemetry_stale', 'last_data_age_sec': round(age, 1)}}
            direction_details = {}
        else:
            direction_details = {}
            active_evidence = {}
            statuses = []
            for direction in BNQO_DIRECTIONS:
                dir_status, evidence = _direction_status(link, direction, now)
                statuses.append(dir_status)
                direction_details[direction] = {'status': dir_status, 'evidence': evidence}
                kind = _STATUS_INCIDENT_KINDS.get(dir_status)
                if kind is not None:
                    active.add((kind, direction))
                    active_evidence[(kind, direction)] = evidence
                elif dir_status == 'degraded' and evidence.get('latency_regression'):
                    active.add(('latency_regression', direction))
                    active_evidence[('latency_regression', direction)] = evidence
            status = _worst(statuses)
            detail['freshness'] = {'last_data_age_sec': round(age, 1)}

    for direction, dir_detail in direction_details.items():
        detail[direction] = dir_detail
    link.status = status
    link.status_json = json.dumps(detail, ensure_ascii=False)

    # Open incidents for new conditions; auto-diagnostics on critical ones.
    for kind, direction in sorted(active):
        had_open = (BnqoIncident.query
                    .filter_by(link_id=link.id, kind=kind, direction=direction)
                    .filter(BnqoIncident.status.in_(['open', 'ack']))
                    .first()) is not None
        incident = _open_incident(link, kind, direction,
                                  active_evidence.get((kind, direction), {}), now)
        if (not had_open and incident.status == 'open'
                and kind in ('link_down', 'packet_loss')
                and not _recent_diag_job_exists(link, now)):
            enqueue_diagnostic_mtr(link)

    # Auto-resolve engine-managed incidents whose condition cleared.
    managed_kinds = set(_STATUS_INCIDENT_KINDS.values()) | {'latency_regression'}
    open_incidents = (BnqoIncident.query
                      .filter(BnqoIncident.link_id == link.id,
                              BnqoIncident.status.in_(['open', 'ack']),
                              BnqoIncident.kind.in_(managed_kinds))
                      .all())
    for incident in open_incidents:
        if (incident.kind, incident.direction) not in active:
            incident.status = 'resolved'
            incident.resolved_at = now


def _first_differing_hop(old_route, new_route):
    old_hops = {hop.hop_number: hop.address for hop in old_route.hops}
    for hop in new_route.hops:
        if old_hops.get(hop.hop_number) != hop.address:
            return hop.hop_number
    return None


def _detect_route_changes(now):
    """Open a route_change incident when the newest MTR route_hash differs from
    the previous one for the same link+direction (RFP §5-006)."""
    link_ids = [row.link_id for row in
                BnqoRoute.query.with_entities(BnqoRoute.link_id).distinct()]
    for link_id in link_ids:
        link = db.session.get(BnqoLink, link_id)
        if link is None:
            continue
        for direction in BNQO_DIRECTIONS:
            routes = (BnqoRoute.query
                      .filter_by(link_id=link_id, direction=direction)
                      .order_by(BnqoRoute.id.desc())
                      .limit(2)
                      .all())
            if len(routes) < 2:
                continue
            new_route, old_route = routes[0], routes[1]
            if not new_route.route_hash or new_route.route_hash == old_route.route_hash:
                continue
            # Dedup: one incident per observed new route_hash.
            dup = (BnqoIncident.query
                   .filter_by(link_id=link_id, kind='route_change', direction=direction)
                   .filter(BnqoIncident.evidence_json.contains(new_route.route_hash))
                   .first())
            if dup is not None:
                continue
            evidence = {
                'direction': direction,
                'route_hash_old': old_route.route_hash,
                'route_hash_new': new_route.route_hash,
                'first_differing_hop': _first_differing_hop(old_route, new_route),
                'destination_reached': bool(new_route.destination_reached),
            }
            _open_incident(link, 'route_change', direction, evidence, now, dedup_open=False)


def _rollup_old_measurements(now):
    """Fold raw measurements older than RAW_RETENTION_DAYS into hourly rollups,
    then delete the raw rows. Batched and idempotent: an existing rollup row
    is merged (weighted) so re-runs and split batches stay correct."""
    cutoff = now - timedelta(days=RAW_RETENTION_DAYS)
    rows = (BnqoMeasurement.query
            .filter(BnqoMeasurement.window_start < cutoff)
            .order_by(BnqoMeasurement.window_start.asc())
            .limit(ROLLUP_ROW_BATCH)
            .all())
    if not rows:
        return
    groups = {}
    for row in rows:
        hour = row.window_start.replace(minute=0, second=0, microsecond=0)
        groups.setdefault((row.link_id, row.direction, hour), []).append(row)
    for (link_id, direction, hour), group in groups.items():
        rollup = (BnqoRollup.query
                  .filter_by(link_id=link_id, direction=direction, hour=hour)
                  .first())
        losses = [float(r.loss_pct or 0.0) for r in group]
        p95s = [r.rtt_p95_ms for r in group if r.rtt_p95_ms is not None]
        jitters = [r.jitter_ms for r in group if r.jitter_ms is not None]
        n = len(group)
        if rollup is None:
            rollup = BnqoRollup(link_id=link_id, direction=direction, hour=hour, samples=0)
            db.session.add(rollup)
        total = (rollup.samples or 0) + n
        rollup.loss_avg = (((rollup.loss_avg or 0.0) * (rollup.samples or 0) + sum(losses)) / total)
        if p95s:
            rollup.rtt_p95 = max(rollup.rtt_p95 or 0.0, max(p95s))
        if jitters:
            prev_jitter_total = (rollup.jitter_avg or 0.0) * (rollup.samples or 0)
            rollup.jitter_avg = (prev_jitter_total + sum(jitters)) / total
        rollup.samples = total
        for row in group:
            db.session.delete(row)


def bnqo_scheduler_tick(now=None):
    """One engine pass: link statuses, incidents, route changes, rollup."""
    from app import app  # deferred: app-level helper, avoids circular import
    now = now or datetime.utcnow()
    for link in BnqoLink.query.filter_by(enabled=True).all():
        try:
            _evaluate_link(link, now)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            app.logger.warning('[bnqo] link %s evaluation failed: %s', link.id, exc)
    try:
        _detect_route_changes(now)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.warning('[bnqo] route-change detection failed: %s', exc)
    try:
        _rollup_old_measurements(now)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.warning(f'[bnqo] rollup failed: {exc}')


def bnqo_scheduler_worker():
    """Long-running loop: BNQO status engine + retention rollup."""
    from app import app  # deferred: app-level helper, avoids circular import
    while True:
        try:
            with app.app_context():
                bnqo_scheduler_tick()
        except Exception as exc:
            app.logger.warning('[bnqo] scheduler tick failed: %s', exc)
        time.sleep(BNQO_WORKER_POLL_SECONDS)
