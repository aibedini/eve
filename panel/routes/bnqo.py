"""BNQO control-plane routes: agent API, admin API and dashboard pages.

Wire contract: docs/bnqo/EVE_API_CONTRACT.md (Phase 1, normative).
- Agent API under /api/bnqo/agent/* — bearer token + Ed25519 request signature.
- Admin API under /api/bnqo/* — session auth via login_required.
- Pages under /pulse/links* — login_required, templates rendered by the UI track.
"""
import json
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, jsonify, render_template, request
from sqlalchemy import or_

from panel.extensions import db
from panel.models import (
    BNQO_AGENT_ROLES,
    BNQO_CLOCK_QUALITIES,
    BNQO_DEFAULT_PROFILE,
    BNQO_DIRECTIONS,
    BnqoAgent,
    BnqoEnrollToken,
    BnqoIncident,
    BnqoJob,
    BnqoLink,
    BnqoMeasurement,
    BnqoRollup,
    BnqoRoute,
    BnqoRouteHop,
    BnqoServiceProbe,
)
from panel.routes.common import login_required
from panel.services.bnqo_crypto import (
    decode_pubkey,
    get_cp_pubkey_b64,
    session_seed_hex,
    sign_canonical,
    verify_with_signature,
)

bp = Blueprint('bnqo', __name__)

_AGENT_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_ROUTE_HASH_RE = re.compile(r'^[0-9a-fA-F]{16}$')
MAX_MEASUREMENTS_PER_BATCH = 500
MAX_SERVICE_PROBES_PER_BATCH = 200
MAX_MTR_RESULTS_PER_BATCH = 50
MAX_JOB_ACKS_PER_BATCH = 200
MAX_MTR_HOPS = 64
SERIES_MAX_POINTS = 500
SERIES_RAW_CAP_HOURS = 7 * 24          # raw measurements kept ≤ 7 days per query
SERIES_ROLLUP_THRESHOLD_HOURS = 14 * 24

_SERIES_METRIC_COLUMNS = {
    'loss': 'loss_pct',
    'rtt': 'rtt_p95_ms',
    'jitter': 'jitter_ms',
    'owd': 'owd_ms',
}
_SERIES_ROLLUP_COLUMNS = {
    'loss': 'loss_avg',
    'rtt': 'rtt_p95',
    'jitter': 'jitter_avg',
    'owd': None,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _err(code, message, status=400):
    return jsonify({'error': {'code': code, 'message': message}}), status


def _iso(dt):
    return dt.isoformat() + 'Z' if dt else None


def _parse_ts(value):
    """Parse a UTC ISO-8601 timestamp ('Z' suffix) into a naive UTC datetime."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        from datetime import timezone
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


def _req_int(value, lo, hi):
    if not _is_int(value) or not (lo <= value <= hi):
        raise ValueError('expected integer')
    return value


def _opt_int(value, lo, hi, default=None):
    if value is None:
        return default
    return _req_int(value, lo, hi)


def _req_float(value, lo, hi):
    if not _is_num(value) or not (lo <= float(value) <= hi):
        raise ValueError('expected number')
    return float(value)


def _opt_float(value, lo, hi):
    if value is None:
        return None
    return _req_float(value, lo, hi)


def _req_str(value, max_len, field='value'):
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f'invalid {field}')
    return value


def _opt_str(value, max_len):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise ValueError('invalid string')
    return value


def _agent_links(agent):
    return (BnqoLink.query
            .filter(BnqoLink.enabled.is_(True),
                    or_(BnqoLink.agent_a_id == agent.id,
                        BnqoLink.agent_b_id == agent.id))
            .all())


def _agent_owns_link(agent, link_id):
    link = db.session.get(BnqoLink, link_id) if _is_int(link_id) else None
    if link is None:
        return None
    if agent.id not in (link.agent_a_id, link.agent_b_id):
        return None
    return link


def _link_peer(link, agent):
    return link.agent_b if link.agent_a_id == agent.id else link.agent_a


# ---------------------------------------------------------------------------
# Agent authentication — Bearer token + Ed25519 signature (contract §1)
# ---------------------------------------------------------------------------
def _bnqo_agent_required(view):
    """Enforce bearer token + X-BNQO-Timestamp + X-BNQO-Signature.

    Signature is Ed25519 over ``"<timestamp>\n" + raw body`` verified with the
    agent's stored public key; timestamp skew must be ≤ 300 s. On success the
    agent row is passed as the first view argument and its
    last_seen_at / last_ip are refreshed.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization') or ''
        token = auth[7:].strip() if auth.startswith('Bearer ') else ''
        agent = BnqoAgent.query.filter_by(token=token).first() if token else None
        if agent is None or not agent.enabled:
            return _err('invalid_agent_token', 'invalid agent token', 401)
        timestamp = request.headers.get('X-BNQO-Timestamp') or ''
        signature = request.headers.get('X-BNQO-Signature') or ''
        if not timestamp or not signature:
            return _err('missing_signature',
                        'X-BNQO-Timestamp and X-BNQO-Signature headers are required', 401)
        try:
            ts = int(timestamp.strip())
        except ValueError:
            return _err('timestamp_skew', 'invalid X-BNQO-Timestamp', 401)
        import time as _time
        if abs(int(_time.time()) - ts) > 300:
            return _err('timestamp_skew', 'timestamp skew exceeds 300 seconds', 401)
        body = request.get_data() or b''
        if not verify_with_signature(agent.pubkey, timestamp, signature, body):
            return _err('invalid_signature', 'invalid request signature', 401)
        agent.last_seen_at = datetime.utcnow()
        agent.last_ip = request.remote_addr
        db.session.commit()
        return view(agent, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Agent API (contract §2)
# ---------------------------------------------------------------------------
@bp.route('/api/bnqo/agent/enroll', methods=['POST'])
def bnqo_agent_enroll():
    """One-time-token enrollment (contract §2.1). Unauthenticated."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err('invalid_payload', 'JSON object body required', 400)
    token_value = data.get('enroll_token')
    if not isinstance(token_value, str) or not token_value:
        return _err('enroll_token_invalid', 'enroll_token is required', 404)
    enroll = BnqoEnrollToken.query.filter_by(token=token_value.strip()[:64]).first()
    if enroll is None:
        return _err('enroll_token_invalid', 'unknown enroll token', 404)
    now = datetime.utcnow()
    if enroll.used_at is not None:
        return _err('enroll_token_used', 'enroll token already used', 409)
    if enroll.expires_at and enroll.expires_at < now:
        return _err('enroll_token_expired', 'enroll token expired', 410)

    try:
        name = _req_str(data.get('name'), 64, 'name')
        if not _AGENT_NAME_RE.match(name):
            raise ValueError('bad name')
        # Token role is authoritative; the request role is advisory (§2.1).
        role = enroll.role if enroll.role in BNQO_AGENT_ROLES else data.get('role')
        if role not in BNQO_AGENT_ROLES:
            raise ValueError('bad role')
        pubkey = _req_str(data.get('pubkey'), 64, 'pubkey')
        if decode_pubkey(pubkey) is None:
            raise ValueError('bad pubkey')
        address = _opt_str(data.get('address'), 64) or (request.remote_addr or None)
        port = _req_int(data.get('port'), 1, 65535)
        version = _opt_str(data.get('version'), 32)
    except ValueError as exc:
        return _err('invalid_payload', f'invalid enrollment payload: {exc}', 400)

    if BnqoAgent.query.filter_by(name=name).first() is not None:
        return _err('agent_name_taken', 'an agent with this name already exists', 409)

    agent = BnqoAgent(
        name=name,
        role=role,
        address=address,
        port=port,
        token=secrets.token_hex(32),
        pubkey=pubkey,
        enabled=True,
        version=version,
        config_version=1,
        last_seen_at=now,
        last_ip=request.remote_addr,
    )
    db.session.add(agent)
    db.session.flush()  # assign agent.id before marking the token used
    # Single use: the token is invalidated atomically with the enrollment.
    enroll.used_at = now
    enroll.used_by_agent_id = agent.id
    db.session.commit()
    return jsonify({
        'agent_id': agent.id,
        'agent_token': agent.token,
        'cp_pubkey': get_cp_pubkey_b64(),
        'config_version': agent.config_version,
    })


@bp.route('/api/bnqo/agent/config')
@_bnqo_agent_required
def bnqo_agent_config(agent):
    """Signed config for the calling agent only (contract §2.2)."""
    links = []
    for link in _agent_links(agent):
        peer = _link_peer(link, agent)
        is_a = link.agent_a_id == agent.id
        links.append({
            'link_id': link.id,
            'name': link.name,
            'peer': {
                'name': peer.name if peer else None,
                'address': peer.address if peer else None,
                'port': peer.port if peer else None,
            },
            # Direction this agent probes in, from the link's A→B perspective.
            'direction': 'a_to_b' if is_a else 'b_to_a',
            # Same seed for both agents of the link → identical HKDF keys.
            'session_seed': session_seed_hex(link.id),
            'profile': link.profile(),
        })
    payload = {
        'config_version': agent.config_version or 0,
        'agent': {'name': agent.name, 'role': agent.role},
        'links': links,
    }
    payload['signature'] = sign_canonical(payload)
    return jsonify(payload)


@bp.route('/api/bnqo/agent/jobs')
@_bnqo_agent_required
def bnqo_agent_jobs(agent):
    """Pending, unexpired jobs for this agent, each individually signed (§2.3)."""
    now = datetime.utcnow()
    jobs = (BnqoJob.query
            .filter(BnqoJob.agent_id == agent.id,
                    BnqoJob.status == 'pending',
                    BnqoJob.expires_at > now)
            .order_by(BnqoJob.created_at.asc(), BnqoJob.id.asc())
            .limit(100)
            .all())
    out = []
    for job in jobs:
        item = {
            'job_id': job.job_id,
            'type': job.type,
            'params': job.params(),
            'expires_at': _iso(job.expires_at),
            'config_version': job.config_version or 0,
        }
        item['signature'] = sign_canonical(item)
        out.append(item)
    return jsonify({'jobs': out})


def _validate_measurement(entry, agent):
    """Validate one UDP measurement window; returns (link, row kwargs)."""
    if not isinstance(entry, dict):
        raise ValueError('measurement must be an object')
    link = _agent_owns_link(agent, entry.get('link_id'))
    if link is None:
        raise PermissionError('link not assigned to this agent')
    direction = entry.get('direction')
    if direction not in BNQO_DIRECTIONS:
        raise ValueError('invalid direction')
    window_start = _parse_ts(entry.get('window_start'))
    window_end = _parse_ts(entry.get('window_end'))
    if window_start is None or window_end is None or window_end < window_start:
        raise ValueError('invalid window')
    clock_quality = entry.get('clock_quality')
    if clock_quality is not None and clock_quality not in BNQO_CLOCK_QUALITIES:
        raise ValueError('invalid clock_quality')
    return link, {
        'direction': direction,
        'source': 'udp',
        'window_start': window_start,
        'window_end': window_end,
        'sent': _req_int(entry.get('sent'), 0, 10 ** 9),
        'received': _req_int(entry.get('received'), 0, 10 ** 9),
        'loss_pct': _req_float(entry.get('loss_pct'), 0.0, 100.0),
        'rtt_min_ms': _opt_float(entry.get('rtt_min_ms'), 0.0, 10 ** 7),
        'rtt_avg_ms': _opt_float(entry.get('rtt_avg_ms'), 0.0, 10 ** 7),
        'rtt_p95_ms': _opt_float(entry.get('rtt_p95_ms'), 0.0, 10 ** 7),
        'rtt_max_ms': _opt_float(entry.get('rtt_max_ms'), 0.0, 10 ** 7),
        'owd_ms': _opt_float(entry.get('owd_ms'), -10 ** 7, 10 ** 7),
        'clock_quality': clock_quality,
        'jitter_ms': _opt_float(entry.get('jitter_ms'), 0.0, 10 ** 7),
        'reordered': _opt_int(entry.get('reordered'), 0, 10 ** 9, 0),
        'duplicated': _opt_int(entry.get('duplicated'), 0, 10 ** 9, 0),
        'corrupted': _opt_int(entry.get('corrupted'), 0, 10 ** 9, 0),
        'burst_max': _opt_int(entry.get('burst_max'), 0, 10 ** 9, 0),
    }


def _validate_icmp(entry, agent, fallback_window):
    """Validate one ICMP summary; stored as a measurement with source='icmp'."""
    if not isinstance(entry, dict):
        raise ValueError('icmp entry must be an object')
    link = _agent_owns_link(agent, entry.get('link_id'))
    if link is None:
        raise PermissionError('link not assigned to this agent')
    direction = entry.get('direction')
    if direction not in BNQO_DIRECTIONS:
        raise ValueError('invalid direction')
    return link, {
        'direction': direction,
        'source': 'icmp',
        'window_start': fallback_window,
        'window_end': fallback_window,
        'sent': _req_int(entry.get('sent'), 0, 10 ** 9),
        'received': _req_int(entry.get('received'), 0, 10 ** 9),
        'loss_pct': _req_float(entry.get('loss_pct'), 0.0, 100.0),
        'rtt_avg_ms': _opt_float(entry.get('rtt_avg_ms'), 0.0, 10 ** 7),
        'rtt_p95_ms': _opt_float(entry.get('rtt_p95_ms'), 0.0, 10 ** 7),
    }


def _validate_service_probe(entry, agent):
    if not isinstance(entry, dict):
        raise ValueError('service probe must be an object')
    link = _agent_owns_link(agent, entry.get('link_id'))
    if link is None:
        raise PermissionError('link not assigned to this agent')
    ok = entry.get('ok')
    if not isinstance(ok, bool):
        raise ValueError('invalid ok flag')
    return link, {
        'target_name': _req_str(entry.get('target_name'), 64, 'target_name'),
        'ok': ok,
        'tcp_ms': _opt_float(entry.get('tcp_ms'), 0.0, 10 ** 7),
        'tls_ms': _opt_float(entry.get('tls_ms'), 0.0, 10 ** 7),
        'http_status': _opt_int(entry.get('http_status'), 0, 999),
        'error_class': _opt_str(entry.get('error_class'), 64),
    }


def _validate_host_snapshot(host):
    """Host metrics: a flat JSON object of scalars, capped in size."""
    if host is None:
        return None
    if not isinstance(host, dict) or len(host) > 64:
        raise ValueError('invalid host snapshot')
    clean = {}
    for key, value in host.items():
        if not isinstance(key, str) or len(key) > 48:
            raise ValueError('invalid host snapshot key')
        if value is None or _is_num(value) or isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, str) and len(value) <= 128:
            clean[key] = value
        else:
            raise ValueError('invalid host snapshot value')
    return json.dumps(clean, ensure_ascii=False)


@bp.route('/api/bnqo/agent/report', methods=['POST'])
@_bnqo_agent_required
def bnqo_agent_report(agent):
    """Ingest one telemetry batch; idempotent on agent_seq (contract §2.4)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _err('invalid_payload', 'JSON object body required', 400)
    agent_seq = data.get('agent_seq')
    if not _is_int(agent_seq) or agent_seq < 0:
        return _err('invalid_payload', 'agent_seq must be a non-negative integer', 400)
    # Idempotency watermark: replay of an already-seen seq stores nothing.
    if agent_seq <= (agent.last_seq or 0):
        return jsonify({'accepted': True, 'agent_seq': agent_seq, 'duplicate': True})

    measurements = data.get('measurements') or []
    icmp = data.get('icmp') or []
    service_probes = data.get('service_probes') or []
    mtr_results = data.get('mtr_results') or []
    job_acks = data.get('job_acks') or []
    if not all(isinstance(items, list) for items in
               (measurements, icmp, service_probes, mtr_results, job_acks)):
        return _err('invalid_payload', 'batch sections must be arrays', 400)
    if (len(measurements) > MAX_MEASUREMENTS_PER_BATCH
            or len(icmp) > MAX_MEASUREMENTS_PER_BATCH
            or len(service_probes) > MAX_SERVICE_PROBES_PER_BATCH
            or len(mtr_results) > MAX_MTR_RESULTS_PER_BATCH
            or len(job_acks) > MAX_JOB_ACKS_PER_BATCH):
        return _err('batch_too_large', 'batch section exceeds size limit', 413)

    now = datetime.utcnow()
    sent_at = _parse_ts(data.get('sent_at')) or now
    try:
        # Secure-UDP measurement windows.
        latest_data_at = None
        for entry in measurements:
            link, row = _validate_measurement(entry, agent)
            db.session.add(BnqoMeasurement(link_id=link.id, **row))
            if latest_data_at is None or row['window_end'] > latest_data_at:
                latest_data_at = row['window_end']
        # ICMP summaries → measurement rows with source='icmp'.
        for entry in icmp:
            link, row = _validate_icmp(entry, agent, sent_at)
            db.session.add(BnqoMeasurement(link_id=link.id, **row))
        # Service-target probes.
        for entry in service_probes:
            link, row = _validate_service_probe(entry, agent)
            db.session.add(BnqoServiceProbe(link_id=link.id, **row))
        # Latest host-metrics snapshot lives on the agent row.
        host_json = _validate_host_snapshot(data.get('host'))
        # MTR diagnostic results → route + hops.
        for entry in mtr_results:
            _persist_mtr_result(agent, entry, now)
        # Job acknowledgements.
        for entry in job_acks:
            _apply_job_ack(agent, entry, now)
    except PermissionError as exc:
        db.session.rollback()
        return _err('link_not_assigned', str(exc), 403)
    except ValueError as exc:
        db.session.rollback()
        return _err('invalid_payload', str(exc), 400)

    if host_json is not None:
        agent.host_json = host_json
    agent.last_seq = agent_seq
    if latest_data_at is not None:
        for link in _agent_links(agent):
            if link.last_data_at is None or latest_data_at > link.last_data_at:
                link.last_data_at = latest_data_at
    db.session.commit()
    return jsonify({'accepted': True, 'agent_seq': agent_seq, 'duplicate': False})


def _persist_mtr_result(agent, entry, now):
    if not isinstance(entry, dict):
        raise ValueError('mtr result must be an object')
    link = _agent_owns_link(agent, entry.get('link_id'))
    if link is None:
        raise PermissionError('link not assigned to this agent')
    direction = entry.get('direction')
    if direction not in BNQO_DIRECTIONS:
        raise ValueError('invalid direction')
    route_hash = entry.get('route_hash')
    if route_hash is not None and not _ROUTE_HASH_RE.match(str(route_hash)):
        raise ValueError('invalid route_hash')
    destination_reached = entry.get('destination_reached')
    if not isinstance(destination_reached, bool):
        raise ValueError('invalid destination_reached')
    job_id = _opt_str(entry.get('job_id'), 64)
    hops = entry.get('hops') or []
    if not isinstance(hops, list) or len(hops) > MAX_MTR_HOPS:
        raise ValueError('invalid hops')
    route = BnqoRoute(
        link_id=link.id,
        direction=direction,
        route_hash=str(route_hash).lower() if route_hash else None,
        destination_reached=destination_reached,
        job_id=job_id,
    )
    db.session.add(route)
    db.session.flush()
    for hop in hops:
        if not isinstance(hop, dict):
            raise ValueError('invalid hop')
        db.session.add(BnqoRouteHop(
            route_id=route.id,
            hop_number=_req_int(hop.get('hop'), 1, MAX_MTR_HOPS),
            address=_opt_str(hop.get('address'), 64),
            loss_pct=_opt_float(hop.get('loss_pct'), 0.0, 100.0),
            rtt_avg_ms=_opt_float(hop.get('rtt_avg_ms'), 0.0, 10 ** 7),
        ))
    # The result itself acknowledges the job.
    if job_id:
        job = BnqoJob.query.filter_by(job_id=job_id, agent_id=agent.id).first()
        if job is not None and job.status == 'pending':
            job.status = 'acked'
            job.result_received_at = now


def _apply_job_ack(agent, entry, now):
    if not isinstance(entry, dict):
        raise ValueError('job ack must be an object')
    job_id = _req_str(entry.get('job_id'), 40, 'job_id')
    status = entry.get('status')
    if status not in ('done', 'failed'):
        raise ValueError('invalid ack status')
    error_class = _opt_str(entry.get('error_class'), 64)
    job = BnqoJob.query.filter_by(job_id=job_id, agent_id=agent.id).first()
    if job is None:
        return  # ack for an unknown/expired job: ignore, do not fail the batch
    job.status = 'failed' if status == 'failed' else 'acked'
    job.error_class = error_class
    job.result_received_at = now


# ---------------------------------------------------------------------------
# Admin API (contract §3) — session auth
# ---------------------------------------------------------------------------
@bp.route('/api/bnqo/agents')
@login_required
def bnqo_admin_agents():
    agents = BnqoAgent.query.order_by(BnqoAgent.id.asc()).all()
    return jsonify({'agents': [
        {key: value for key, value in agent.to_dict().items()
         if key in ('id', 'name', 'role', 'address', 'port', 'enabled',
                    'version', 'last_seen_at', 'last_ip', 'config_version')}
        for agent in agents
    ]})


@bp.route('/api/bnqo/enroll-tokens', methods=['POST'])
@login_required
def bnqo_admin_enroll_token_create():
    data = request.get_json(silent=True) or {}
    role = data.get('role', 'outside')
    if role not in BNQO_AGENT_ROLES:
        return _err('invalid_payload', 'role must be iran, outside or relay', 400)
    try:
        ttl_minutes = int(data.get('ttl_minutes', 30))
    except (TypeError, ValueError):
        return _err('invalid_payload', 'ttl_minutes must be an integer', 400)
    ttl_minutes = max(1, min(24 * 60, ttl_minutes))
    token = BnqoEnrollToken(
        token=secrets.token_hex(32),
        role=role,
        expires_at=datetime.utcnow() + timedelta(minutes=ttl_minutes),
    )
    db.session.add(token)
    db.session.commit()
    origin = request.url_root.rstrip('/')
    install_command = (
        f'curl -fsSL {origin}/static/app-files/bnqo/install.sh -o /tmp/bnqo-install.sh'
        f' && sudo BNQO_EVE_URL={origin} BNQO_ENROLL_TOKEN={token.token}'
        ' bash /tmp/bnqo-install.sh'
    )
    return jsonify({
        'token': token.token,
        'expires_at': _iso(token.expires_at),
        'install_command': install_command,
    })


@bp.route('/api/bnqo/agents/<int:agent_id>/revoke', methods=['POST'])
@login_required
def bnqo_admin_agent_revoke(agent_id):
    agent = db.session.get(BnqoAgent, agent_id)
    if agent is None:
        return _err('agent_not_found', 'agent not found', 404)
    agent.enabled = False
    db.session.commit()
    return jsonify({'agent': agent.to_dict()})


def _validate_profile(profile):
    """Merge a caller-supplied profile over the server defaults (§2.2)."""
    if profile is None:
        return dict(BNQO_DEFAULT_PROFILE)
    if not isinstance(profile, dict):
        raise ValueError('profile must be an object')
    merged = dict(BNQO_DEFAULT_PROFILE)
    if 'interval_ms' in profile:
        merged['interval_ms'] = _req_int(profile['interval_ms'], 20, 60_000)
    if 'packet_size' in profile:
        merged['packet_size'] = _req_int(profile['packet_size'], 64, 1500)
    if 'window_sec' in profile:
        merged['window_sec'] = _req_int(profile['window_sec'], 5, 3600)
    if 'icmp_enabled' in profile:
        if not isinstance(profile['icmp_enabled'], bool):
            raise ValueError('icmp_enabled must be boolean')
        merged['icmp_enabled'] = profile['icmp_enabled']
    if 'icmp_count' in profile:
        merged['icmp_count'] = _req_int(profile['icmp_count'], 1, 100)
    if 'icmp_interval_sec' in profile:
        merged['icmp_interval_sec'] = _req_int(profile['icmp_interval_sec'], 5, 3600)
    if 'service_targets' in profile:
        targets = profile['service_targets']
        if not isinstance(targets, list) or len(targets) > 32:
            raise ValueError('invalid service_targets')
        clean_targets = []
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError('invalid service target')
            clean_targets.append({
                'name': _req_str(target.get('name'), 64, 'target name'),
                'host': _req_str(target.get('host'), 255, 'target host'),
                'port': _req_int(target.get('port'), 1, 65535),
                'tls': bool(target.get('tls')),
                'interval_sec': _req_int(target.get('interval_sec', 30), 5, 3600),
            })
        merged['service_targets'] = clean_targets
    return merged


def _bump_agents_config_version(*agents):
    for agent in agents:
        if agent is not None:
            agent.config_version = (agent.config_version or 0) + 1


@bp.route('/api/bnqo/links')
@login_required
def bnqo_admin_links():
    links = BnqoLink.query.order_by(BnqoLink.id.asc()).all()
    return jsonify({'links': [link.to_dict() for link in links]})


@bp.route('/api/bnqo/links', methods=['POST'])
@login_required
def bnqo_admin_link_create():
    data = request.get_json(silent=True) or {}
    try:
        name = _req_str(data.get('name'), 100, 'name')
        agent_a_id = _req_int(data.get('agent_a_id'), 1, 2 ** 31 - 1)
        agent_b_id = _req_int(data.get('agent_b_id'), 1, 2 ** 31 - 1)
        profile = _validate_profile(data.get('profile'))
    except ValueError as exc:
        return _err('invalid_payload', str(exc), 400)
    if agent_a_id == agent_b_id:
        return _err('invalid_payload', 'agent_a_id and agent_b_id must differ', 400)
    agent_a = db.session.get(BnqoAgent, agent_a_id)
    agent_b = db.session.get(BnqoAgent, agent_b_id)
    if agent_a is None or agent_b is None:
        return _err('agent_not_found', 'both agents must exist', 404)
    link = BnqoLink(
        name=name,
        agent_a_id=agent_a_id,
        agent_b_id=agent_b_id,
        profile_json=json.dumps(profile, ensure_ascii=False),
        status='unknown',
        enabled=True,
    )
    db.session.add(link)
    _bump_agents_config_version(agent_a, agent_b)
    db.session.commit()
    return jsonify({'link': link.to_dict()})


@bp.route('/api/bnqo/links/<int:link_id>', methods=['PATCH'])
@login_required
def bnqo_admin_link_update(link_id):
    link = db.session.get(BnqoLink, link_id)
    if link is None:
        return _err('link_not_found', 'link not found', 404)
    data = request.get_json(silent=True) or {}
    try:
        if 'name' in data:
            link.name = _req_str(data.get('name'), 100, 'name')
        if 'profile' in data:
            link.profile_json = json.dumps(
                _validate_profile(data.get('profile')), ensure_ascii=False)
        if 'enabled' in data:
            if not isinstance(data['enabled'], bool):
                raise ValueError('enabled must be boolean')
            link.enabled = data['enabled']
    except ValueError as exc:
        return _err('invalid_payload', str(exc), 400)
    _bump_agents_config_version(link.agent_a, link.agent_b)
    db.session.commit()
    return jsonify({'link': link.to_dict()})


@bp.route('/api/bnqo/links/<int:link_id>', methods=['DELETE'])
@login_required
def bnqo_admin_link_delete(link_id):
    link = db.session.get(BnqoLink, link_id)
    if link is None:
        return _err('link_not_found', 'link not found', 404)
    _bump_agents_config_version(link.agent_a, link.agent_b)
    db.session.delete(link)
    db.session.commit()
    return jsonify({'deleted': True, 'link_id': link_id})


@bp.route('/api/bnqo/links/<int:link_id>/diagnose', methods=['POST'])
@login_required
def bnqo_admin_link_diagnose(link_id):
    """Enqueue signed RUN_MTR jobs for both agents of the link (contract §3)."""
    from panel.jobs.bnqo import enqueue_diagnostic_mtr
    link = db.session.get(BnqoLink, link_id)
    if link is None:
        return _err('link_not_found', 'link not found', 404)
    jobs = enqueue_diagnostic_mtr(link)
    db.session.commit()
    return jsonify({'job_ids': [job.job_id for job in jobs]})


def _downsample(points, cap=SERIES_MAX_POINTS):
    """Bucket-average [(t, value)] down to at most ``cap`` points."""
    if len(points) <= cap:
        return points
    chunk = -(-len(points) // cap)  # ceil
    out = []
    for start in range(0, len(points), chunk):
        group = points[start:start + chunk]
        values = [value for _t, value in group if value is not None]
        out.append((group[0][0], sum(values) / len(values) if values else None))
    return out


@bp.route('/api/bnqo/links/<int:link_id>/series')
@login_required
def bnqo_admin_link_series(link_id):
    """Time series for one metric/direction; rollups beyond 14 days (§3)."""
    link = db.session.get(BnqoLink, link_id)
    if link is None:
        return _err('link_not_found', 'link not found', 404)
    metric = request.args.get('metric', 'loss')
    if metric not in _SERIES_METRIC_COLUMNS:
        return _err('invalid_payload', 'metric must be loss, rtt, jitter or owd', 400)
    direction = request.args.get('direction', 'a_to_b')
    if direction not in BNQO_DIRECTIONS:
        return _err('invalid_payload', 'invalid direction', 400)
    try:
        hours = float(request.args.get('hours', 6))
    except (TypeError, ValueError):
        return _err('invalid_payload', 'hours must be a number', 400)
    if not (0 < hours <= 24 * 365):
        return _err('invalid_payload', 'hours out of range', 400)
    now = datetime.utcnow()
    since = now - timedelta(hours=hours)

    points = []
    if hours > SERIES_ROLLUP_THRESHOLD_HOURS:
        column = _SERIES_ROLLUP_COLUMNS[metric]
        if column is not None:
            rows = (BnqoRollup.query
                    .filter(BnqoRollup.link_id == link.id,
                            BnqoRollup.direction == direction,
                            BnqoRollup.hour >= since)
                    .order_by(BnqoRollup.hour.asc())
                    .all())
            points = [(row.hour, getattr(row, column)) for row in rows]
    else:
        hours = min(hours, SERIES_RAW_CAP_HOURS)
        since = now - timedelta(hours=hours)
        column = _SERIES_METRIC_COLUMNS[metric]
        rows = (BnqoMeasurement.query
                .filter(BnqoMeasurement.link_id == link.id,
                        BnqoMeasurement.direction == direction,
                        BnqoMeasurement.source == 'udp',
                        BnqoMeasurement.window_start >= since)
                .order_by(BnqoMeasurement.window_start.asc())
                .all())
        points = [(row.window_start, getattr(row, column)) for row in rows]
    points = _downsample(points)
    return jsonify({'points': [{'t': _iso(t), 'value': value} for t, value in points]})


@bp.route('/api/bnqo/links/<int:link_id>/routes')
@login_required
def bnqo_admin_link_routes(link_id):
    """Latest MTR route per direction (contract §3)."""
    link = db.session.get(BnqoLink, link_id)
    if link is None:
        return _err('link_not_found', 'link not found', 404)
    routes = []
    for direction in BNQO_DIRECTIONS:
        route = (BnqoRoute.query
                 .filter(BnqoRoute.link_id == link.id,
                         BnqoRoute.direction == direction)
                 .order_by(BnqoRoute.created_at.desc(), BnqoRoute.id.desc())
                 .first())
        if route is None:
            continue
        routes.append({
            'direction': direction,
            'route_hash': route.route_hash,
            'destination_reached': bool(route.destination_reached),
            'created_at': _iso(route.created_at),
            'hops': [hop.to_dict() for hop in route.hops],
        })
    return jsonify({'routes': routes})


@bp.route('/api/bnqo/incidents')
@login_required
def bnqo_admin_incidents():
    query = BnqoIncident.query
    status = request.args.get('status')
    if status in ('open', 'ack', 'resolved'):
        query = query.filter(BnqoIncident.status == status)
    incidents = query.order_by(BnqoIncident.opened_at.desc()).limit(500).all()
    link_names = {link.id: link.name for link in
                  BnqoLink.query.filter(BnqoLink.id.in_({i.link_id for i in incidents})).all()} \
        if incidents else {}
    return jsonify({'incidents': [
        incident.to_dict(link_name=link_names.get(incident.link_id))
        for incident in incidents
    ]})


@bp.route('/api/bnqo/incidents/<int:incident_id>/ack', methods=['POST'])
@login_required
def bnqo_admin_incident_ack(incident_id):
    incident = db.session.get(BnqoIncident, incident_id)
    if incident is None:
        return _err('incident_not_found', 'incident not found', 404)
    if incident.status == 'open':
        incident.status = 'ack'
        db.session.commit()
    return jsonify({'incident': incident.to_dict()})


@bp.route('/api/bnqo/incidents/<int:incident_id>/resolve', methods=['POST'])
@login_required
def bnqo_admin_incident_resolve(incident_id):
    incident = db.session.get(BnqoIncident, incident_id)
    if incident is None:
        return _err('incident_not_found', 'incident not found', 404)
    if incident.status != 'resolved':
        incident.status = 'resolved'
        incident.resolved_at = datetime.utcnow()
        db.session.commit()
    return jsonify({'incident': incident.to_dict()})


# ---------------------------------------------------------------------------
# Pages (contract §4) — templates are delivered by the UI track
# ---------------------------------------------------------------------------
@bp.route('/pulse/links')
@login_required
def bnqo_links_page():
    return render_template('bnqo_links.html')


@bp.route('/pulse/links/<int:link_id>')
@login_required
def bnqo_link_detail_page(link_id):
    link = db.session.get(BnqoLink, link_id)
    if link is None:
        abort(404)
    return render_template('bnqo_link_detail.html', link_id=link_id)
