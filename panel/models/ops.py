"""Ops/monitoring models: portal, panel API, health, pulse, logs (extracted from app.py)."""
import json
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from panel.extensions import db

class ClientPortalUser(db.Model):
    """End-user portal accounts — login with Iranian mobile + password."""
    __tablename__ = 'client_portal_users'
    id = db.Column(db.Integer, primary_key=True)
    mobile = db.Column(db.String(20), unique=True, nullable=False)   # normalised: +989xxxxxxxxx
    password_hash = db.Column(db.String(255), nullable=False)
    must_change_password = db.Column(db.Boolean, default=True)
    enabled = db.Column(db.Boolean, default=True)
    linked_email = db.Column(db.String(255))                         # x-ui client email (optional)
    display_name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    failed_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)

    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    def is_locked(self) -> bool:
        if self.locked_until and self.locked_until > datetime.utcnow():
            return True
        return False

    def record_failed(self):
        self.failed_attempts = (self.failed_attempts or 0) + 1
        if self.failed_attempts >= 5:
            self.locked_until = datetime.utcnow() + timedelta(minutes=15)

    def reset_failed(self):
        self.failed_attempts = 0
        self.locked_until = None


class ClientOwnership(db.Model):
    __tablename__ = 'client_ownerships'
    id = db.Column(db.Integer, primary_key=True)
    reseller_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id'), nullable=False)
    inbound_id = db.Column(db.Integer, nullable=True)
    client_email = db.Column(db.String(100), nullable=False)
    client_uuid = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    price = db.Column(db.Integer, default=0)
    
    reseller = db.relationship('Admin', backref=db.backref('clients', lazy=True))
    server = db.relationship('Server', backref=db.backref('owned_clients', lazy=True))

class PanelAPI(db.Model):
    __tablename__ = 'panel_apis'
    id = db.Column(db.Integer, primary_key=True)
    panel_type = db.Column(db.String(50), unique=True, nullable=False)  # 'sanaei', 'alireza', etc
    display_name = db.Column(db.String(100))
    login_endpoint = db.Column(db.String(100))
    
    # Inbound endpoints
    inbounds_list = db.Column(db.String(200))
    inbounds_get = db.Column(db.String(200))
    inbounds_add = db.Column(db.String(200))
    inbounds_update = db.Column(db.String(200))
    inbounds_delete = db.Column(db.String(200))
    
    # Client endpoints
    client_add = db.Column(db.String(200))
    client_update = db.Column(db.String(200))
    client_delete = db.Column(db.String(200))
    client_reset_traffic = db.Column(db.String(200))
    client_get_traffic = db.Column(db.String(200))
    
    # Server endpoints
    server_status = db.Column(db.String(200))
    server_restart = db.Column(db.String(200))
    server_stop = db.Column(db.String(200))
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'panel_type': self.panel_type,
            'display_name': self.display_name,
            'login_endpoint': self.login_endpoint,
            'inbounds_list': self.inbounds_list,
            'inbounds_get': self.inbounds_get,
            'client_add': self.client_add,
            'client_reset_traffic': self.client_reset_traffic
        }

def get_panel_api(panel_type):
    """Return PanelAPI config for given panel_type or None."""
    if not panel_type or panel_type == 'auto':
        return None
    return PanelAPI.query.filter_by(panel_type=panel_type).first()


# ---------------------------------------------------------------------------
# HealthLog model – stores health-check events & auto-heal action logs
# ---------------------------------------------------------------------------
class HealthLog(db.Model):
    __tablename__ = 'health_logs'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    level = db.Column(db.String(16), default='info')       # info / warning / error / critical
    category = db.Column(db.String(32), default='general')  # db / server / static / disk / general
    message = db.Column(db.Text, nullable=False)
    action_taken = db.Column(db.Text)                       # description of auto-heal action, if any
    details = db.Column(db.Text)                            # extra JSON payload
    resolved = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat() + 'Z' if self.timestamp else None,
            'level': self.level,
            'category': self.category,
            'message': self.message,
            'action_taken': self.action_taken,
            'details': self.details,
            'resolved': self.resolved,
        }


# ---------------------------------------------------------------------------
# Eve Pulse models – config health-check runs & per-config probe results
# ---------------------------------------------------------------------------
class PulseRun(db.Model):
    __tablename__ = 'pulse_runs'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id'), nullable=True, index=True)
    server_name = db.Column(db.String(100), nullable=True)      # snapshot in case the server is renamed/removed
    scope = db.Column(db.String(16), default='server')          # server / inbound / config
    inbound_label = db.Column(db.String(255), nullable=True)
    profile = db.Column(db.String(16), default='quick')         # quick / full / custom
    vantage = db.Column(db.String(32), default='local')         # 'local' now; remote agents later
    status = db.Column(db.String(16), default='running')        # queued / running / done / failed
    summary_json = db.Column(db.Text, nullable=True)            # counts: healthy/degraded/down
    triggered_by = db.Column(db.String(32), default='cli')
    params_json = db.Column(db.Text, nullable=True)             # queued-run params: inbound_id/limit/sites
    error = db.Column(db.Text, nullable=True)
    results = db.relationship('PulseResultRecord', backref='run',
                              cascade='all, delete-orphan', lazy=True)

    def summary(self):
        try:
            return json.loads(self.summary_json) if self.summary_json else {}
        except Exception:
            return {}

    def params(self):
        try:
            return json.loads(self.params_json) if self.params_json else {}
        except Exception:
            return {}

    def to_dict(self):
        public_params = self.params()
        manual_configs = public_params.pop('manual_configs', None)
        if isinstance(manual_configs, list):
            public_params['manual_config_count'] = len(manual_configs)
        if isinstance(public_params.get('configs'), list):
            public_params['configs'] = [
                {key: value for key, value in entry.items() if key != 'uri'}
                for entry in public_params['configs'] if isinstance(entry, dict)
            ]
        return {
            'id': self.id,
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
            'finished_at': self.finished_at.isoformat() + 'Z' if self.finished_at else None,
            'server_id': self.server_id,
            'server_name': self.server_name,
            'scope': self.scope,
            'inbound_label': self.inbound_label,
            'profile': self.profile,
            'vantage': self.vantage,
            'status': self.status,
            'summary': self.summary(),
            'triggered_by': self.triggered_by,
            'params': public_params,
            'error': self.error,
            'result_count': len(self.results) if self.results is not None else 0,
        }


class PulseResultRecord(db.Model):
    """One probed config within a PulseRun (named to avoid clashing with
    pulse.ProbeResult when both are imported in the same module)."""
    __tablename__ = 'pulse_results'
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey('pulse_runs.id'), nullable=False, index=True)
    config_label = db.Column(db.String(255), nullable=True)
    uri_scheme = db.Column(db.String(32), nullable=True)
    verdict = db.Column(db.String(16), default='down', index=True)   # healthy / degraded / down
    latency_avg_ms = db.Column(db.Float, nullable=True)
    loss_pct = db.Column(db.Float, nullable=True)
    download_mbps = db.Column(db.Float, nullable=True)
    sites_json = db.Column(db.Text, nullable=True)                   # site-check array
    detail_json = db.Column(db.Text, nullable=True)                  # full ProbeResult.to_dict()
    is_probe = db.Column(db.Boolean, default=False)                  # email contains 'probe'
    error = db.Column(db.Text, nullable=True)

    def to_dict(self):
        try:
            sites = json.loads(self.sites_json) if self.sites_json else []
        except Exception:
            sites = []
        return {
            'id': self.id,
            'run_id': self.run_id,
            'config_label': self.config_label,
            'uri_scheme': self.uri_scheme,
            'verdict': self.verdict,
            'latency_avg_ms': self.latency_avg_ms,
            'loss_pct': self.loss_pct,
            'download_mbps': self.download_mbps,
            'sites': sites,
            'is_probe': bool(self.is_probe),
            'error': self.error,
        }


class PulseSettings(db.Model):
    """Singleton row: scheduled-probe and alert configuration for Eve Pulse."""
    __tablename__ = 'pulse_settings'
    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False)
    interval_minutes = db.Column(db.Integer, default=60)
    server_id = db.Column(db.Integer, nullable=True)         # null = all enabled servers
    inbound_id = db.Column(db.Integer, nullable=True)        # null = whole server(s)
    profile = db.Column(db.String(16), default='quick')      # quick / full
    probe_limit = db.Column(db.Integer, default=10)
    sites_json = db.Column(db.Text, nullable=True)           # [{name,url,expect_substring}]
    alert_on_down = db.Column(db.Boolean, default=True)
    alert_on_degraded = db.Column(db.Boolean, default=False)
    last_run_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def sites(self):
        try:
            value = json.loads(self.sites_json) if self.sites_json else []
            return value if isinstance(value, list) else []
        except Exception:
            return []

    def to_dict(self):
        return {
            'enabled': bool(self.enabled),
            'interval_minutes': self.interval_minutes or 60,
            'server_id': self.server_id,
            'inbound_id': self.inbound_id,
            'profile': self.profile or 'quick',
            'probe_limit': self.probe_limit or 10,
            'sites': self.sites(),
            'alert_on_down': bool(self.alert_on_down),
            'alert_on_degraded': bool(self.alert_on_degraded),
            'last_run_at': self.last_run_at.isoformat() + 'Z' if self.last_run_at else None,
        }


class PulseTemplate(db.Model):
    """Reusable, ordered Pulse test plan selected explicitly by an admin."""
    __tablename__ = 'pulse_templates'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    targets_json = db.Column(db.Text, nullable=False, default='[]')
    profile = db.Column(db.String(16), default='quick')
    vantage = db.Column(db.String(64), default='local')
    sites_json = db.Column(db.Text, nullable=True)
    download_bytes = db.Column(db.Integer, default=10_000_000)
    upload_bytes = db.Column(db.Integer, default=2_000_000)
    schedule_enabled = db.Column(db.Boolean, default=False)
    interval_minutes = db.Column(db.Integer, default=60)
    last_run_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def targets(self):
        try:
            value = json.loads(self.targets_json or '[]')
            return value if isinstance(value, list) else []
        except Exception:
            return []

    def sites(self):
        try:
            value = json.loads(self.sites_json or '[]')
            return value if isinstance(value, list) else []
        except Exception:
            return []

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'targets': self.targets(),
            'profile': self.profile or 'quick',
            'vantage': self.vantage or 'local',
            'sites': self.sites(),
            'download_bytes': self.download_bytes or 10_000_000,
            'upload_bytes': self.upload_bytes or 2_000_000,
            'schedule_enabled': bool(self.schedule_enabled),
            'interval_minutes': self.interval_minutes or 60,
            'last_run_at': self.last_run_at.isoformat() + 'Z' if self.last_run_at else None,
        }


def get_pulse_settings(create=True):
    """Return the singleton PulseSettings row (created with defaults if missing)."""
    row = PulseSettings.query.order_by(PulseSettings.id.asc()).first()
    if row is None and create:
        row = PulseSettings()
        db.session.add(row)
        db.session.commit()
    return row


class PulseAgent(db.Model):
    """A remote vantage host running pulse_agent.py outside Iran.

    Agents authenticate to the pull/push API with a random bearer token that
    is shown exactly once at creation time.
    """
    __tablename__ = 'pulse_agents'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    name = db.Column(db.String(64), unique=True, nullable=False)
    token = db.Column(db.String(64), nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    last_ip = db.Column(db.String(64), nullable=True)

    def to_dict(self, include_token=False):
        payload = {
            'id': self.id,
            'name': self.name,
            'enabled': bool(self.enabled),
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
            'last_seen_at': self.last_seen_at.isoformat() + 'Z' if self.last_seen_at else None,
            'last_ip': self.last_ip,
        }
        if include_token:
            payload['token'] = self.token
        return payload


class MonitorMessageLog(db.Model):
    __tablename__ = 'monitor_message_log'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    server_id = db.Column(db.Integer, nullable=False)
    channel = db.Column(db.String(16), nullable=False)  # 'sms' or 'whatsapp'
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    sent_by = db.Column(db.Integer)  # admin id


class AuditLog(db.Model):
    """Durable audit trail for sensitive panel and bot actions."""
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    actor_type = db.Column(db.String(16), nullable=False, default='system')  # admin | system | customer
    actor_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True, index=True)
    action = db.Column(db.String(64), nullable=False, index=True)
    target_type = db.Column(db.String(32), nullable=True)
    target_id = db.Column(db.String(64), nullable=True)
    meta_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class WhatsappBotLog(db.Model):
    """Dedup log for the OpenWA near-depletion bot — one row per send so a
    cooldown window can be enforced per (email, server_id, event)."""
    __tablename__ = 'whatsapp_bot_log'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    server_id = db.Column(db.Integer, nullable=False)
    event = db.Column(db.String(32), nullable=False, default='depletion')
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class SmsSendLog(db.Model):
    """Human-facing audit trail for the automated, state-based SMS scan. One row
    per processed recipient (sent / failed / skipped) so the operator can see who
    was messaged, for which monitor state, and why a send was or wasn't made.
    Dedup/cooldown itself is enforced separately via WhatsappBotLog."""
    __tablename__ = 'sms_send_log'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    server_id = db.Column(db.Integer, nullable=False, default=0)
    server_name = db.Column(db.String(255))
    state = db.Column(db.String(32), nullable=False, index=True)  # near_expiry|low_volume|expired|ended
    recipient = db.Column(db.String(32))               # masked mobile (e.g. 0912***4643)
    status = db.Column(db.String(16), nullable=False)  # queued | sent | failed | skipped
    reason = db.Column(db.String(255))                 # failure/skip detail
    job_id = db.Column(db.String(64), index=True)
    request_id = db.Column(db.String(128), index=True)
    gateway_job_id = db.Column(db.String(64))
    status_url = db.Column(db.String(512))
    gateway_state = db.Column(db.String(32))
    stage = db.Column(db.String(64))
    terminal = db.Column(db.Boolean)
    successful = db.Column(db.Boolean)
    gateway_current_at = db.Column(db.String(64))
    gateway_sent_at = db.Column(db.String(64))
    segment_count = db.Column(db.Integer)
    message_encoding = db.Column(db.String(16))
    unit_count = db.Column(db.Integer)
    character_count = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'server_id': self.server_id,
            'server_name': self.server_name,
            'state': self.state,
            'recipient': self.recipient,
            'status': self.status,
            'reason': self.reason,
            'job_id': self.job_id,
            'request_id': self.request_id,
            'gateway_job_id': self.gateway_job_id,
            'gateway_state': self.gateway_state,
            'stage': self.stage,
            'terminal': self.terminal,
            'successful': self.successful,
            'gateway_current_at': self.gateway_current_at,
            'gateway_sent_at': self.gateway_sent_at,
            'segment_count': self.segment_count,
            'message_encoding': self.message_encoding,
            'unit_count': self.unit_count,
            'character_count': self.character_count,
            # Stored as naive UTC (datetime.utcnow). Emit an explicit 'Z' so the
            # browser parses it as UTC and can convert to the viewer's timezone
            # (Asia/Tehran) instead of mis-reading it as local time.
            'created_at': (self.created_at.isoformat() + 'Z') if self.created_at else None,
            'updated_at': (self.updated_at.isoformat() + 'Z') if self.updated_at else None,
        }


class PendingSms(db.Model):
    """Transactional create/renew SMS that arrived during quiet hours. Parked here
    and flushed once the quiet window ends, so a confirmation is never lost nor
    sent at 3am. The depletion scan re-queues itself naturally, so only the
    one-shot transactional sends need this."""
    __tablename__ = 'pending_sms'
    id = db.Column(db.Integer, primary_key=True)
    recipient = db.Column(db.String(32), nullable=False)   # full mobile to send to
    text = db.Column(db.Text, nullable=False)
    event_name = db.Column(db.String(32), nullable=False)  # 'renew' | 'created'
    email = db.Column(db.String(255))
    server_id = db.Column(db.Integer, default=0)
    server_name = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


# ---------------------------------------------------------------------------
# BNQO models – bidirectional network-quality monitoring control plane.
# Wire contract: docs/bnqo/EVE_API_CONTRACT.md (Phase 1, normative).
# ---------------------------------------------------------------------------
BNQO_AGENT_ROLES = ('iran', 'outside', 'relay')
BNQO_DIRECTIONS = ('a_to_b', 'b_to_a')
BNQO_CLOCK_QUALITIES = ('good', 'low', 'invalid', 'unknown')

BNQO_DEFAULT_PROFILE = {
    'interval_ms': 200,
    'packet_size': 256,
    'window_sec': 30,
    'icmp_enabled': True,
    'icmp_count': 5,
    'icmp_interval_sec': 30,
    'service_targets': [],
}


class BnqoAgent(db.Model):
    """A host running bnqo-agent, enrolled via a one-time enroll token.

    Authenticates to the agent API with a random bearer token plus an Ed25519
    request signature (contract §1). ``last_seq`` is the idempotency watermark
    for report batches.
    """
    __tablename__ = 'bnqo_agents'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    name = db.Column(db.String(64), unique=True, nullable=False)
    role = db.Column(db.String(16), nullable=False, default='outside')  # iran / outside / relay
    address = db.Column(db.String(64), nullable=True)
    port = db.Column(db.Integer, nullable=True)
    token = db.Column(db.String(64), nullable=False)
    pubkey = db.Column(db.String(64), nullable=False)                    # base64 raw 32-byte Ed25519
    enabled = db.Column(db.Boolean, default=True)
    version = db.Column(db.String(32), nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    last_ip = db.Column(db.String(64), nullable=True)
    config_version = db.Column(db.Integer, default=0)
    last_seq = db.Column(db.Integer, default=0)                          # report idempotency watermark
    host_json = db.Column(db.Text, nullable=True)                        # latest host-metrics snapshot

    def host(self):
        try:
            return json.loads(self.host_json) if self.host_json else {}
        except Exception:
            return {}

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'role': self.role,
            'address': self.address,
            'port': self.port,
            'enabled': bool(self.enabled),
            'version': self.version,
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
            'last_seen_at': self.last_seen_at.isoformat() + 'Z' if self.last_seen_at else None,
            'last_ip': self.last_ip,
            'config_version': self.config_version or 0,
        }


class BnqoEnrollToken(db.Model):
    """One-time agent enrollment token (contract §2.1)."""
    __tablename__ = 'bnqo_enroll_tokens'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    token = db.Column(db.String(64), unique=True, nullable=False)
    role = db.Column(db.String(16), nullable=False, default='outside')
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    used_by_agent_id = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'token': self.token,
            'role': self.role,
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
            'expires_at': self.expires_at.isoformat() + 'Z' if self.expires_at else None,
            'used_at': self.used_at.isoformat() + 'Z' if self.used_at else None,
            'used_by_agent_id': self.used_by_agent_id,
        }


class BnqoLink(db.Model):
    """A monitored path between two agents (A ↔ B). Directions in all
    measurements are from the link's A→B perspective (contract §2.2)."""
    __tablename__ = 'bnqo_links'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    name = db.Column(db.String(100), nullable=False)
    agent_a_id = db.Column(db.Integer, db.ForeignKey('bnqo_agents.id'), nullable=False)
    agent_b_id = db.Column(db.Integer, db.ForeignKey('bnqo_agents.id'), nullable=False)
    profile_json = db.Column(db.Text, nullable=True)                     # null → server defaults
    status = db.Column(db.String(32), default='unknown')
    status_json = db.Column(db.Text, nullable=True)                      # per-direction detail/evidence
    enabled = db.Column(db.Boolean, default=True)
    last_data_at = db.Column(db.DateTime, nullable=True)

    agent_a = db.relationship('BnqoAgent', foreign_keys=[agent_a_id], lazy=True)
    agent_b = db.relationship('BnqoAgent', foreign_keys=[agent_b_id], lazy=True)

    def profile(self):
        merged = dict(BNQO_DEFAULT_PROFILE)
        try:
            stored = json.loads(self.profile_json) if self.profile_json else {}
        except Exception:
            stored = {}
        if isinstance(stored, dict):
            merged.update(stored)
        return merged

    def status_detail(self):
        try:
            return json.loads(self.status_json) if self.status_json else {}
        except Exception:
            return {}

    def to_dict(self):
        def _agent_brief(agent):
            if agent is None:
                return None
            return {'id': agent.id, 'name': agent.name, 'role': agent.role}
        return {
            'id': self.id,
            'name': self.name,
            'agent_a': _agent_brief(self.agent_a),
            'agent_b': _agent_brief(self.agent_b),
            'enabled': bool(self.enabled),
            'status': self.status or 'unknown',
            'status_detail': self.status_detail(),
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
            'last_data_at': self.last_data_at.isoformat() + 'Z' if self.last_data_at else None,
        }


class BnqoMeasurement(db.Model):
    """One measurement window per link+direction (contract §2.4).

    ``source`` distinguishes the secure-UDP engine windows ('udp') from ICMP
    summary rows ('icmp'); the status engine needs the split for the
    probe-blocked rule (UDP dead but ICMP alive).
    """
    __tablename__ = 'bnqo_measurements'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    link_id = db.Column(db.Integer, db.ForeignKey('bnqo_links.id'), nullable=False, index=True)
    direction = db.Column(db.String(8), nullable=False)                  # a_to_b / b_to_a
    source = db.Column(db.String(8), nullable=False, default='udp')      # udp / icmp
    window_start = db.Column(db.DateTime, nullable=False)
    window_end = db.Column(db.DateTime, nullable=False)
    sent = db.Column(db.Integer, default=0)
    received = db.Column(db.Integer, default=0)
    loss_pct = db.Column(db.Float, default=0.0)
    rtt_min_ms = db.Column(db.Float, nullable=True)
    rtt_avg_ms = db.Column(db.Float, nullable=True)
    rtt_p95_ms = db.Column(db.Float, nullable=True)
    rtt_max_ms = db.Column(db.Float, nullable=True)
    owd_ms = db.Column(db.Float, nullable=True)
    clock_quality = db.Column(db.String(16), nullable=True)
    jitter_ms = db.Column(db.Float, nullable=True)
    reordered = db.Column(db.Integer, default=0)
    duplicated = db.Column(db.Integer, default=0)
    corrupted = db.Column(db.Integer, default=0)
    burst_max = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.Index('ix_bnqo_measurements_link_window', 'link_id', 'window_start'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'link_id': self.link_id,
            'direction': self.direction,
            'source': self.source,
            'window_start': self.window_start.isoformat() + 'Z' if self.window_start else None,
            'window_end': self.window_end.isoformat() + 'Z' if self.window_end else None,
            'sent': self.sent,
            'received': self.received,
            'loss_pct': self.loss_pct,
            'rtt_min_ms': self.rtt_min_ms,
            'rtt_avg_ms': self.rtt_avg_ms,
            'rtt_p95_ms': self.rtt_p95_ms,
            'rtt_max_ms': self.rtt_max_ms,
            'owd_ms': self.owd_ms,
            'clock_quality': self.clock_quality,
            'jitter_ms': self.jitter_ms,
            'reordered': self.reordered,
            'duplicated': self.duplicated,
            'corrupted': self.corrupted,
            'burst_max': self.burst_max,
        }


class BnqoServiceProbe(db.Model):
    """One TCP/TLS/HTTP service-target probe result (contract §2.4)."""
    __tablename__ = 'bnqo_service_probes'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    link_id = db.Column(db.Integer, db.ForeignKey('bnqo_links.id'), nullable=False, index=True)
    target_name = db.Column(db.String(64), nullable=False)
    ok = db.Column(db.Boolean, default=False)
    tcp_ms = db.Column(db.Float, nullable=True)
    tls_ms = db.Column(db.Float, nullable=True)
    http_status = db.Column(db.Integer, nullable=True)
    error_class = db.Column(db.String(64), nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'link_id': self.link_id,
            'target_name': self.target_name,
            'ok': bool(self.ok),
            'tcp_ms': self.tcp_ms,
            'tls_ms': self.tls_ms,
            'http_status': self.http_status,
            'error_class': self.error_class,
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
        }


class BnqoRoute(db.Model):
    """One MTR run for a link+direction; hops in BnqoRouteHop."""
    __tablename__ = 'bnqo_routes'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    link_id = db.Column(db.Integer, db.ForeignKey('bnqo_links.id'), nullable=False, index=True)
    direction = db.Column(db.String(8), nullable=False)
    route_hash = db.Column(db.String(16), nullable=True, index=True)
    destination_reached = db.Column(db.Boolean, default=False)
    job_id = db.Column(db.String(64), nullable=True)

    hops = db.relationship('BnqoRouteHop', backref='route',
                           cascade='all, delete-orphan', lazy=True,
                           order_by='BnqoRouteHop.hop_number')

    def to_dict(self):
        return {
            'id': self.id,
            'link_id': self.link_id,
            'direction': self.direction,
            'route_hash': self.route_hash,
            'destination_reached': bool(self.destination_reached),
            'job_id': self.job_id,
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
            'hops': [hop.to_dict() for hop in self.hops],
        }


class BnqoRouteHop(db.Model):
    __tablename__ = 'bnqo_route_hops'
    id = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey('bnqo_routes.id'), nullable=False, index=True)
    hop_number = db.Column(db.Integer, nullable=False)
    address = db.Column(db.String(64), nullable=True)
    loss_pct = db.Column(db.Float, nullable=True)
    rtt_avg_ms = db.Column(db.Float, nullable=True)

    def to_dict(self):
        return {
            'hop': self.hop_number,
            'address': self.address,
            'loss_pct': self.loss_pct,
            'rtt_avg_ms': self.rtt_avg_ms,
        }


class BnqoIncident(db.Model):
    """Detection-engine incident with evidence (contract §5, RFP §9)."""
    __tablename__ = 'bnqo_incidents'
    id = db.Column(db.Integer, primary_key=True)
    link_id = db.Column(db.Integer, db.ForeignKey('bnqo_links.id'), nullable=False, index=True)
    direction = db.Column(db.String(8), nullable=True)                   # null = link-level
    kind = db.Column(db.String(48), nullable=False)
    status = db.Column(db.String(16), default='open')                    # open / ack / resolved
    evidence_json = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    resolved_at = db.Column(db.DateTime, nullable=True)

    def evidence(self):
        try:
            return json.loads(self.evidence_json) if self.evidence_json else {}
        except Exception:
            return {}

    def to_dict(self, link_name=None):
        return {
            'id': self.id,
            'link_id': self.link_id,
            'link_name': link_name,
            'direction': self.direction,
            'kind': self.kind,
            'status': self.status or 'open',
            'evidence': self.evidence(),
            'opened_at': self.opened_at.isoformat() + 'Z' if self.opened_at else None,
            'resolved_at': self.resolved_at.isoformat() + 'Z' if self.resolved_at else None,
        }


class BnqoRollup(db.Model):
    """Hourly aggregate of raw measurements past the raw-retention window."""
    __tablename__ = 'bnqo_rollups_hourly'
    id = db.Column(db.Integer, primary_key=True)
    link_id = db.Column(db.Integer, db.ForeignKey('bnqo_links.id'), nullable=False, index=True)
    direction = db.Column(db.String(8), nullable=False)
    hour = db.Column(db.DateTime, nullable=False, index=True)
    samples = db.Column(db.Integer, default=0)
    loss_avg = db.Column(db.Float, nullable=True)
    rtt_p95 = db.Column(db.Float, nullable=True)
    jitter_avg = db.Column(db.Float, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('link_id', 'direction', 'hour', name='uq_bnqo_rollup_link_dir_hour'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'link_id': self.link_id,
            'direction': self.direction,
            'hour': self.hour.isoformat() + 'Z' if self.hour else None,
            'samples': self.samples,
            'loss_avg': self.loss_avg,
            'rtt_p95': self.rtt_p95,
            'jitter_avg': self.jitter_avg,
        }


class BnqoJob(db.Model):
    """Typed, signed remote job for one agent (contract §2.3, RFP §12)."""
    __tablename__ = 'bnqo_jobs'
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    job_id = db.Column(db.String(40), unique=True, nullable=False)
    agent_id = db.Column(db.Integer, db.ForeignKey('bnqo_agents.id'), nullable=False, index=True)
    type = db.Column(db.String(32), nullable=False)
    params_json = db.Column(db.Text, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    config_version = db.Column(db.Integer, default=0)
    status = db.Column(db.String(16), default='pending')                 # pending / acked / failed
    error_class = db.Column(db.String(64), nullable=True)
    result_received_at = db.Column(db.DateTime, nullable=True)

    def params(self):
        try:
            return json.loads(self.params_json) if self.params_json else {}
        except Exception:
            return {}

    def to_dict(self):
        return {
            'id': self.id,
            'job_id': self.job_id,
            'agent_id': self.agent_id,
            'type': self.type,
            'params': self.params(),
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
            'expires_at': self.expires_at.isoformat() + 'Z' if self.expires_at else None,
            'config_version': self.config_version or 0,
            'status': self.status or 'pending',
            'error_class': self.error_class,
            'result_received_at': self.result_received_at.isoformat() + 'Z' if self.result_received_at else None,
        }
