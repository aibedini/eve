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
