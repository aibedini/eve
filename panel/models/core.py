"""Core shared models (extracted from app.py)."""
import json
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from panel.extensions import db
from panel.models._helpers import _format_jalali, _parse_allowed_servers, _server_is_v3  # noqa: F401

class Admin(db.Model):
    __tablename__ = 'admins'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='admin')
    is_superadmin = db.Column(db.Boolean, default=False)
    credit = db.Column(db.Integer, default=0)
    allow_negative_credit = db.Column(db.Boolean, default=False)
    negative_credit_limit = db.Column(db.Integer, default=0)
    allow_free_creation = db.Column(db.Boolean, default=False)
    whatsapp_automation_enabled = db.Column(db.Boolean, default=False)
    allowed_servers = db.Column(db.Text, default='[]')
    enabled = db.Column(db.Boolean, default=True)
    discount_percent = db.Column(db.Integer, default=0)
    custom_cost_per_day = db.Column(db.Integer, nullable=True)
    custom_cost_per_gb = db.Column(db.Integer, nullable=True)
    sub_shown_package_ids = db.Column(db.Text, default='[]')  # admin/global/assigned package IDs this reseller shows on their customers' sub pages
    telegram_id = db.Column(db.String(100), nullable=True)
    support_telegram = db.Column(db.String(100), nullable=True)
    support_whatsapp = db.Column(db.String(64), nullable=True)
    support_sms = db.Column(db.String(64), nullable=True)
    channel_telegram = db.Column(db.Text, nullable=True)
    channel_whatsapp = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    
    transactions = db.relationship('Transaction', backref='admin', lazy=True)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'is_superadmin': self.is_superadmin,
            'credit': self.credit,
            'allow_negative_credit': bool(self.allow_negative_credit),
            'negative_credit_limit': self.negative_credit_limit or 0,
            'allow_free_creation': bool(self.allow_free_creation),
            'whatsapp_automation_enabled': bool(self.whatsapp_automation_enabled),
            'allowed_servers': _parse_allowed_servers(self.allowed_servers),
            'enabled': self.enabled,
            'discount_percent': self.discount_percent,
            'custom_cost_per_day': self.custom_cost_per_day,
            'custom_cost_per_gb': self.custom_cost_per_gb,
            'telegram_id': self.telegram_id,
            'support_telegram': self.support_telegram,
            'support_whatsapp': self.support_whatsapp,
            'support_sms': self.support_sms,
            'channel_telegram': self.channel_telegram,
            'channel_whatsapp': self.channel_whatsapp,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None
        }

class Server(db.Model):
    __tablename__ = 'servers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    host = db.Column(db.String(255), nullable=False)
    username = db.Column(db.String(100), nullable=False)
    password = db.Column(db.String(255), nullable=False)
    enabled = db.Column(db.Boolean, default=True)
    hidden = db.Column(db.Boolean, default=False)   # hidden=True: skip fetch & dashboard, but still backed up
    panel_type = db.Column(db.String(50), default='auto')
    sub_path = db.Column(db.String(50), default='/sub/')
    json_path = db.Column(db.String(50), default='/json/')
    sub_port = db.Column(db.Integer, nullable=True)
    # JSON array of inbound ids in the preferred subscription display order.
    # Unknown/new inbounds are appended after the configured priorities.
    subscription_inbound_order = db.Column(db.Text, nullable=False, default='[]')
    # Optional 3x-ui v3+ API token (Bearer). When absent, capability-detected v3
    # panels use cookie login + CSRF with the same /panel/api/clients/* endpoints.
    api_token = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'host': self.host,
            'username': self.username,
            'enabled': self.enabled,
            'hidden': bool(self.hidden),
            'panel_type': self.panel_type,
            'sub_path': self.sub_path,
            'json_path': self.json_path,
            'sub_port': self.sub_port,
            'subscription_inbound_order': self.subscription_inbound_order or '[]',
            'has_api_token': bool((self.api_token or '').strip()),
            'supports_v3_clients': bool(_server_is_v3(self)),
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class SubAppConfig(db.Model):
    __tablename__ = 'sub_app_configs'
    id = db.Column(db.Integer, primary_key=True)
    app_code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(100))
    os_type = db.Column(db.String(20), default='android')  # android, ios, windows
    is_enabled = db.Column(db.Boolean, default=True)
    title_fa = db.Column(db.String(200))
    description_fa = db.Column(db.Text)
    title_en = db.Column(db.String(200))
    description_en = db.Column(db.Text)
    download_link = db.Column(db.String(500))
    store_link = db.Column(db.String(500))
    tutorial_link = db.Column(db.String(500))
    icon_url = db.Column(db.String(500))
    is_recommended = db.Column(db.Boolean, default=False)
    display_order = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {
            'id': self.id,
            'app_code': self.app_code,
            'name': self.name,
            'os_type': self.os_type or 'android',
            'is_enabled': self.is_enabled,
            'title_fa': self.title_fa,
            'description_fa': self.description_fa,
            'title_en': self.title_en,
            'description_en': self.description_en,
            'download_link': self.download_link,
            'store_link': self.store_link,
            'tutorial_link': self.tutorial_link,
            'icon_url': self.icon_url,
            'is_recommended': self.is_recommended or False,
            'display_order': self.display_order or 0,
        }


class CustomSubscription(db.Model):
    __tablename__ = 'custom_subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    token = db.Column(db.String(48), nullable=False, unique=True, index=True)
    tag_prefix = db.Column(db.String(64), nullable=False, default='')
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    update_interval_min = db.Column(db.Integer, nullable=False, default=0)
    sort_order = db.Column(db.Integer, nullable=False, default=0, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    configs = db.relationship(
        'CustomSubscriptionConfig', backref='subscription', lazy=True,
        cascade='all, delete-orphan', passive_deletes=True,
        order_by='CustomSubscriptionConfig.sort_order, CustomSubscriptionConfig.id',
    )

    def to_dict(self, public_url=None, include_configs=True):
        configs = list(self.configs)
        payload = {
            'id': self.id, 'name': self.name, 'token': self.token,
            'tag_prefix': self.tag_prefix or '', 'enabled': bool(self.enabled),
            'update_interval_min': max(0, int(self.update_interval_min or 0)),
            'sort_order': int(self.sort_order or 0),
            'config_count': len(configs),
            'active_config_count': sum(bool(row.enabled) for row in configs),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        if public_url:
            payload['public_url'] = public_url
        if include_configs:
            payload['configs'] = [row.to_dict() for row in configs]
        return payload


class CustomSubscriptionConfig(db.Model):
    __tablename__ = 'custom_subscription_configs'
    __table_args__ = (
        db.UniqueConstraint('subscription_id', 'uri', name='uq_custom_subscription_uri'),
    )
    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(
        db.Integer, db.ForeignKey('custom_subscriptions.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    uri = db.Column(db.Text, nullable=False)
    remark = db.Column(db.String(190), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'subscription_id': self.subscription_id,
            'uri': self.uri, 'remark': self.remark or '',
            'enabled': bool(self.enabled), 'sort_order': int(self.sort_order or 0),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

class FAQ(db.Model):
    __tablename__ = 'faqs'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text)  # HTML content
    image_url = db.Column(db.String(500))
    video_url = db.Column(db.String(500))
    platform = db.Column(db.String(20), default='android')  # android, ios, windows
    is_enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'content': self.content,
            'image_url': self.image_url,
            'video_url': self.video_url,
            'platform': self.platform or 'android',
            'is_enabled': self.is_enabled
        }

class Package(db.Model):
    __tablename__ = 'packages'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    days = db.Column(db.Integer, nullable=False)
    volume = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Integer, nullable=False)
    reseller_price = db.Column(db.Integer, nullable=True)
    enabled = db.Column(db.Boolean, default=True)
    # Extended columns (added via ALTER TABLE migration for existing DBs)
    scope = db.Column(db.String(20), default='global')        # global | assigned | personal
    assigned_reseller_ids = db.Column(db.Text, default='[]')  # JSON list of admin IDs
    created_by = db.Column(db.Integer, nullable=True)
    display_order = db.Column(db.Integer, default=0)
    show_on_sub = db.Column(db.Boolean, default=False)  # show this package on customer subscription page
    is_trial = db.Column(db.Boolean, nullable=False, default=False)  # free trial package, bot policy-gated
    show_on_create = db.Column(db.Boolean, nullable=False, default=True)  # show on creation surfaces (panel add-client, bot purchase)
    show_on_renew = db.Column(db.Boolean, nullable=False, default=True)  # show on renewal surfaces (panel renew, bot renewal, sub page)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        import json as _j
        try:
            assigned = _j.loads(self.assigned_reseller_ids or '[]')
        except Exception:
            assigned = []
        return {
            'id': self.id,
            'name': self.name,
            'days': self.days,
            'volume': self.volume,
            'price': self.price,
            'reseller_price': self.reseller_price,
            'enabled': self.enabled,
            'scope': self.scope or 'global',
            'assigned_reseller_ids': assigned,
            'created_by': self.created_by,
            'display_order': self.display_order or 0,
            'show_on_sub': bool(self.show_on_sub),
            'is_trial': bool(self.is_trial),
            'show_on_create': bool(self.show_on_create if self.show_on_create is not None else True),
            'show_on_renew': bool(self.show_on_renew if self.show_on_renew is not None else True),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class PriceTier(db.Model):
    """Dynamic pricing rule: applies when volume_gb/days fall within the defined range."""
    __tablename__ = 'price_tiers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    # Conditions — None means no constraint on that dimension
    min_volume_gb = db.Column(db.Float, nullable=True)   # volume >= this
    max_volume_gb = db.Column(db.Float, nullable=True)   # volume < this (exclusive)
    min_days = db.Column(db.Integer, nullable=True)
    max_days = db.Column(db.Integer, nullable=True)
    # Rate overrides (None = fall through to system default)
    cost_per_gb = db.Column(db.Integer, nullable=True)
    cost_per_day = db.Column(db.Integer, nullable=True)
    # Scope: None = global; reseller_id is legacy single-reseller scope.
    # assigned_reseller_ids stores a JSON list for multi-reseller rules.
    reseller_id = db.Column(db.Integer, nullable=True, index=True)
    assigned_reseller_ids = db.Column(db.Text, default='[]')
    server_id = db.Column(db.Integer, nullable=True, index=True)
    priority = db.Column(db.Integer, default=0)  # higher = evaluated first
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        try:
            assigned = json.loads(self.assigned_reseller_ids or '[]')
        except Exception:
            assigned = []
        if self.reseller_id and self.reseller_id not in assigned:
            assigned.append(self.reseller_id)
        return {
            'id': self.id,
            'name': self.name,
            'min_volume_gb': self.min_volume_gb,
            'max_volume_gb': self.max_volume_gb,
            'min_days': self.min_days,
            'max_days': self.max_days,
            'cost_per_gb': self.cost_per_gb,
            'cost_per_day': self.cost_per_day,
            'reseller_id': self.reseller_id,
            'assigned_reseller_ids': assigned,
            'server_id': self.server_id,
            'priority': self.priority,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class SystemConfig(db.Model):
    __tablename__ = 'system_configs'
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text)


RECEIPT_STATUS_PENDING = 'pending'
RECEIPT_STATUS_AUTO_PENDING = 'auto_pending'
RECEIPT_STATUS_APPROVED = 'approved'
RECEIPT_STATUS_AUTO_APPROVED = 'auto_approved'
RECEIPT_STATUS_REJECTED = 'rejected'


class BankCard(db.Model):
    __tablename__ = 'bank_cards'
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(120), nullable=False)
    bank_name = db.Column(db.String(120))
    owner_name = db.Column(db.String(120))
    card_number = db.Column(db.String(32))
    iban = db.Column(db.String(34))
    account_number = db.Column(db.String(64))
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # NULL = central card; reseller admin.id = reseller-owned card
    reseller_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    assigned_reseller_ids = db.Column(db.Text, default='[]')  # JSON list of admin IDs

    def masked_card(self):
        if not self.card_number:
            return None
        cleaned = ''.join(filter(str.isdigit, self.card_number))
        if len(cleaned) <= 4:
            return cleaned
        return f"{'*' * (len(cleaned) - 4)}{cleaned[-4:]}"

    def to_dict(self):
        try:
            assigned = json.loads(self.assigned_reseller_ids or '[]')
        except Exception:
            assigned = []
        return {
            'id': self.id,
            'label': self.label,
            'bank_name': self.bank_name,
            'owner_name': self.owner_name,
            'card_number': self.card_number,
            'masked_card': self.masked_card(),
            'iban': self.iban,
            'account_number': self.account_number,
            'notes': self.notes,
            'is_active': self.is_active,
            'reseller_id': self.reseller_id,
            'assigned_reseller_ids': assigned,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class NotificationTemplate(db.Model):
    __tablename__ = 'notification_templates'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    type = db.Column(db.String(50), default='client_created')
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # NULL = global; reseller admin.id = reseller-specific (takes priority over global)
    owner_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True, index=True)

    def to_dict(self):
        owner_username = None
        if self.owner_id:
            try:
                _owner = db.session.get(Admin, self.owner_id)
                owner_username = _owner.username if _owner else None
            except Exception:
                pass
        return {
            'id': self.id,
            'name': self.name,
            'content': self.content,
            'type': self.type,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'owner_id': self.owner_id,
            'owner_username': owner_username,
            'scope': 'reseller' if self.owner_id else 'global',
        }


class RenewTemplate(db.Model):
    __tablename__ = 'renew_templates'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'content': self.content,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


announcement_servers = db.Table(
    'announcement_servers',
    db.Column('announcement_id', db.Integer, db.ForeignKey('announcements.id'), primary_key=True),
    db.Column('server_id', db.Integer, db.ForeignKey('servers.id'), primary_key=True),
)


class Announcement(db.Model):
    __tablename__ = 'announcements'
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.Text, nullable=False)
    all_servers = db.Column(db.Boolean, default=True)
    # Reseller-style targeting rules (same shape as Admin.allowed_servers):
    # '*' OR JSON list of {server_id: int, inbounds: '*'|[int,...]}
    targets = db.Column(db.Text)
    start_at = db.Column(db.DateTime, nullable=False)
    end_at = db.Column(db.DateTime, nullable=False)
    created_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    hide_from_resellers = db.Column(db.Boolean, default=False)  # when True, not shown on reseller-owned accounts' sub pages
    is_popup = db.Column(db.Boolean, default=False)  # when True, shown as a modal popup when the sub page opens
    button_text = db.Column(db.String(120))          # popup dismiss-button label (optional)

    servers = db.relationship('Server', secondary=announcement_servers, lazy='subquery')

    def to_dict(self):
        server_ids = []
        server_names = []
        try:
            for s in (self.servers or []):
                server_ids.append(s.id)
                server_names.append(s.name)
        except Exception:
            pass

        now_utc = datetime.utcnow()
        is_active = False
        try:
            is_active = bool(self.start_at and self.end_at and self.start_at <= now_utc <= self.end_at)
        except Exception:
            is_active = False

        return {
            'id': self.id,
            'message': self.message,
            'all_servers': bool(self.all_servers),
            'targets': self.targets or ('*' if self.all_servers else ''),
            'server_ids': server_ids,
            'server_names': server_names,
            'start_at': self.start_at.isoformat() if self.start_at else None,
            'end_at': self.end_at.isoformat() if self.end_at else None,
            'start_at_jalali': _format_jalali(self.start_at) if self.start_at else None,
            'end_at_jalali': _format_jalali(self.end_at) if self.end_at else None,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'created_at_jalali': _format_jalali(self.created_at) if self.created_at else None,
            'is_active': is_active,
            'hide_from_resellers': bool(self.hide_from_resellers),
            'is_popup': bool(self.is_popup),
            'button_text': self.button_text or '',
        }


class OnlineChatScript(db.Model):
    __tablename__ = 'online_chat_scripts'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    script_code = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=False)
    created_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        preview = (self.script_code or '').strip().replace('\n', ' ')
        if len(preview) > 160:
            preview = preview[:160] + '...'
        return {
            'id': self.id,
            'name': self.name,
            'script_code': self.script_code,
            'preview': preview,
            'is_active': bool(self.is_active),
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class BackupConfig(db.Model):
    __tablename__ = 'backup_configs'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='SET NULL'), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    config_url = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, nullable=False, default='')
    is_enabled = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    server = db.relationship('Server', backref=db.backref('backup_configs', passive_deletes=True), foreign_keys=[server_id])

    DEFAULT_DESCRIPTION = (
        'این کانفیگ پشتیبانه. اگه کانفیگ اصلیت کار نمیکنه، '
        'این رو کپی کن و توی برنامه VPN بزن Import from clipboard.\n\n'
        'This is a backup config. If your main connection isn\'t working, '
        'copy this and import it in your VPN app.'
    )

    def to_dict(self):
        return {
            'id': self.id,
            'server_id': self.server_id,
            'server_name': self.server.name if self.server else None,
            'title': self.title,
            'config_url': self.config_url,
            'description': self.description,
            'is_enabled': bool(self.is_enabled),
            'sort_order': self.sort_order,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class SystemSetting(db.Model):
    __tablename__ = 'system_settings'
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Text)


class SystemMigration(db.Model):
    """Durable progress ledger for long-running, resumable data migrations."""
    __tablename__ = 'system_migrations'
    id = db.Column(db.Integer, primary_key=True)
    migration_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    status = db.Column(db.String(24), nullable=False, default='pending', index=True)
    phase = db.Column(db.String(64), nullable=True)
    cursor_json = db.Column(db.Text, nullable=True)
    processed_rows = db.Column(db.BigInteger, nullable=False, default=0)
    total_rows = db.Column(db.BigInteger, nullable=True)
    details_json = db.Column(db.Text, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)


class VolumeRulePreset(db.Model):
    """Saved Volume Filter rule sets so users can reload them instead of
    re-entering rules every time."""
    __tablename__ = 'volume_rule_presets'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    rules = db.Column(db.Text, nullable=False)  # JSON list of rule dicts
    owner_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        try:
            rules = json.loads(self.rules or '[]')
        except Exception:
            rules = []
        return {
            'id': self.id,
            'name': self.name,
            'rules': rules,
            'owner_id': self.owner_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class UsageCounterState(db.Model):
    """Latest observed counter per account; updated in place, never appended."""
    __tablename__ = 'usage_counter_state'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'), nullable=False)
    sub_id = db.Column(db.String(128), nullable=False)
    inbound_tag = db.Column(db.String(256), nullable=True)
    upload_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    download_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    total_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    remaining_bytes = db.Column(db.BigInteger, nullable=True)
    volume_limit_bytes = db.Column(db.BigInteger, nullable=True)
    observed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.UniqueConstraint('server_id', 'sub_id', name='uq_usage_counter_server_sub'),
    )


class UsageHourly(db.Model):
    """Mutable hourly rollup, retained for only 48 hours."""
    __tablename__ = 'usage_hourly'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'), nullable=False)
    sub_id = db.Column(db.String(128), nullable=False)
    inbound_tag = db.Column(db.String(256), nullable=True)
    bucket_at = db.Column(db.DateTime, nullable=False, index=True)
    upload_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    download_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    remaining_bytes = db.Column(db.BigInteger, nullable=True)
    volume_limit_bytes = db.Column(db.BigInteger, nullable=True)
    sample_count = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('server_id', 'sub_id', 'bucket_at', name='uq_usage_hourly_server_sub_bucket'),
    )


class UsageDaily(db.Model):
    """One compact account-level row per Tehran day, retained for one year."""
    __tablename__ = 'usage_daily'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'), nullable=False)
    sub_id = db.Column(db.String(128), nullable=False)
    inbound_tag = db.Column(db.String(256), nullable=True)
    usage_date = db.Column(db.Date, nullable=False, index=True)
    upload_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    download_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    opening_upload_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    opening_download_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    closing_upload_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    closing_download_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    remaining_bytes = db.Column(db.BigInteger, nullable=True)
    volume_limit_bytes = db.Column(db.BigInteger, nullable=True)
    sample_count = db.Column(db.Integer, nullable=False, default=0)
    first_observed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_observed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('server_id', 'sub_id', 'usage_date', name='uq_usage_daily_server_sub_date'),
        db.Index('ix_usage_daily_sub_date', 'sub_id', 'usage_date'),
    )

    @property
    def total_bytes(self):
        return int(self.closing_upload_bytes or 0) + int(self.closing_download_bytes or 0)


class RenewalEvent(db.Model):
    """Detected renewal event for a subscription."""
    __tablename__ = 'renewal_events'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'), nullable=False, index=True)
    sub_id = db.Column(db.String(128), nullable=False, index=True)
    renewed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    volume_bytes = db.Column(db.BigInteger, nullable=True)
    days = db.Column(db.Integer, nullable=True)
    is_unlimited_volume = db.Column(db.Boolean, default=False)
    is_unlimited_time = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.Index('ix_renewal_events_server_sub', 'server_id', 'sub_id'),
    )
