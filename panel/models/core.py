"""Core shared models (extracted from app.py)."""
import json
import re
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from panel.core.finance_privacy import mask_account_like, mask_card_number
from panel.extensions import db
from panel.models._helpers import _format_jalali, _parse_allowed_servers, _server_is_v3  # noqa: F401
from panel.security import EncryptedText


_PHOSPHOR_ICON_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')


def _clean_action_button_icons(buttons):
    if not isinstance(buttons, list):
        return []
    cleaned = []
    for button in buttons:
        if not isinstance(button, dict):
            continue
        item = dict(button)
        icon = str(item.get('icon') or '').strip().lower()
        if icon.startswith('ph-'):
            icon = icon[3:]
        icon = re.sub(r'-+', '-', icon.replace('_', '-').replace(' ', '-')).strip('-')
        if icon and _PHOSPHOR_ICON_RE.fullmatch(icon):
            item['icon'] = icon
        else:
            item.pop('icon', None)
        cleaned.append(item)
    return cleaned


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
    # Per-server transport opt-in. True allows plaintext http:// for THIS panel
    # and skips certificate verification for THIS panel only; every other server
    # keeps full verification. Never a process-wide switch.
    allow_insecure = db.Column(db.Boolean, nullable=False, default=False,
                               server_default=db.text("false"))
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
            # Metadata only: the password itself is never returned to the UI.
            'has_password': bool((self.password or '').strip()),
            'allow_insecure': bool(self.allow_insecure),
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
    action_buttons = db.Column(db.Text, nullable=True)
    icon_url = db.Column(db.String(500))
    is_recommended = db.Column(db.Boolean, default=False)
    display_order = db.Column(db.Integer, default=0)

    def to_dict(self):
        buttons = None
        if self.action_buttons is not None:
            try:
                parsed_buttons = json.loads(self.action_buttons)
                if isinstance(parsed_buttons, list):
                    buttons = _clean_action_button_icons(parsed_buttons)
            except (TypeError, ValueError, json.JSONDecodeError):
                buttons = None

        # Existing installations keep their three legacy actions until the app
        # is saved once with the new button editor.  ``[]`` intentionally means
        # that the administrator wants no actions for this app.
        if buttons is None:
            buttons = []
            if self.download_link:
                buttons.append({
                    'title': 'Download',
                    'url': self.download_link,
                    'palette': 'primary',
                    'label_key': 'download',
                })
            if self.store_link:
                buttons.append({
                    'title': 'Store',
                    'url': self.store_link,
                    'palette': 'green',
                    'label_key': 'store',
                })
            if self.tutorial_link:
                buttons.append({
                    'title': 'Tutorial',
                    'url': self.tutorial_link,
                    'palette': 'purple',
                    'label_key': 'tutorial',
                })

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
            'action_buttons': buttons,
            'icon_url': self.icon_url,
            'is_recommended': self.is_recommended or False,
            'display_order': self.display_order or 0,
        }


class CustomSubscription(db.Model):
    __tablename__ = 'custom_subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    token = db.Column(EncryptedText('subscriptions'), nullable=False)
    token_hash = db.Column(db.String(64), nullable=True, unique=True, index=True)
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
        db.UniqueConstraint(
            'subscription_id', 'uri_hash', name='uq_custom_subscription_uri_hash',
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(
        db.Integer, db.ForeignKey('custom_subscriptions.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    uri = db.Column(EncryptedText('subscriptions'), nullable=False)
    uri_hash = db.Column(db.String(64), nullable=True)
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
    card_number = db.Column(EncryptedText('finance'))
    iban = db.Column(EncryptedText('finance'))
    account_number = db.Column(EncryptedText('finance'))
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # NULL = central card; reseller admin.id = reseller-owned card
    reseller_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    assigned_reseller_ids = db.Column(db.Text, default='[]')  # JSON list of admin IDs

    def masked_card(self):
        return mask_card_number(self.card_number)

    def masked_iban(self):
        return mask_account_like(self.iban)

    def masked_account_number(self):
        return mask_account_like(self.account_number)

    def to_dict(self):
        """Default representation: financial identifiers are always masked."""
        try:
            assigned = json.loads(self.assigned_reseller_ids or '[]')
        except Exception:
            assigned = []
        return {
            'id': self.id,
            'label': self.label,
            'bank_name': self.bank_name,
            'owner_name': self.owner_name,
            'card_number': self.masked_card(),
            'masked_card': self.masked_card(),
            'iban': self.masked_iban(),
            'account_number': self.masked_account_number(),
            'notes': self.notes,
            'is_active': self.is_active,
            'reseller_id': self.reseller_id,
            'assigned_reseller_ids': assigned,
            'revealed': False,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    def to_reveal_dict(self):
        """Full representation, only for the audited, step-up-gated reveal route."""
        payload = self.to_dict()
        payload.update({
            'card_number': self.card_number,
            'iban': self.iban,
            'account_number': self.account_number,
            'revealed': True,
        })
        return payload


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
    action_buttons = db.Column(db.Text, nullable=True)  # JSON list of titled links
    button_columns = db.Column(db.Integer, nullable=False, default=1)
    channel = db.Column(db.String(24), nullable=False, default='subscription', index=True)
    delivery_mode = db.Column(db.String(24), nullable=False, default='all')
    daily_limit = db.Column(db.Integer, nullable=True)
    audience_owner_types = db.Column(
        db.Text, nullable=False, default='["system","unowned"]')
    audience_statuses = db.Column(
        db.Text, nullable=False,
        default='["other","expired","volume_ended","expiring_soon","volume_low"]')
    recipient_estimate = db.Column(db.Text, nullable=True)
    recipient_estimated_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(24), nullable=False, default='draft', index=True)
    total_count = db.Column(db.Integer, nullable=False, default=0)
    sent_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    skipped_count = db.Column(db.Integer, nullable=False, default=0)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

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

        action_buttons = []
        if self.action_buttons:
            try:
                parsed_buttons = json.loads(self.action_buttons)
                if isinstance(parsed_buttons, list):
                    action_buttons = _clean_action_button_icons(parsed_buttons)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        def _audience_list(raw, default):
            try:
                value = json.loads(raw or '')
            except (TypeError, ValueError, json.JSONDecodeError):
                value = default
            return value if isinstance(value, list) and value else list(default)

        recipient_estimate = None
        if self.recipient_estimate:
            try:
                parsed_estimate = json.loads(self.recipient_estimate)
                if isinstance(parsed_estimate, dict):
                    recipient_estimate = parsed_estimate
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        total_count = int(self.total_count or 0)
        sent_count = int(self.sent_count or 0)
        failed_count = int(self.failed_count or 0)
        skipped_count = int(self.skipped_count or 0)
        processed_count = min(total_count, sent_count + failed_count + skipped_count)
        remaining_count = max(0, total_count - processed_count)

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
            'action_buttons': action_buttons,
            'button_columns': 2 if self.button_columns == 2 else 1,
            'channel': self.channel or 'subscription',
            'delivery_mode': self.delivery_mode or 'all',
            'daily_limit': int(self.daily_limit or 0),
            'audience_owner_types': _audience_list(
                self.audience_owner_types, ['system', 'unowned']),
            'audience_statuses': _audience_list(self.audience_statuses, [
                'other', 'expired', 'volume_ended', 'expiring_soon', 'volume_low',
            ]),
            'recipient_estimate': recipient_estimate,
            'recipient_estimated_at': (
                self.recipient_estimated_at.isoformat()
                if self.recipient_estimated_at else None),
            'status': self.status or 'draft',
            'total_count': total_count,
            'sent_count': sent_count,
            'failed_count': failed_count,
            'skipped_count': skipped_count,
            'processed_count': processed_count,
            'remaining_count': remaining_count,
            'progress_percent': round((processed_count / total_count) * 100, 1) if total_count else 0,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
        }


class AnnouncementDelivery(db.Model):
    """Durable, idempotent outbound delivery for an Announcement campaign."""
    __tablename__ = 'announcement_deliveries'
    __table_args__ = (db.UniqueConstraint(
        'announcement_id', 'recipient_key', name='uq_announcement_delivery_recipient'),)

    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey(
        'announcements.id', ondelete='CASCADE'), nullable=False, index=True)
    recipient_key = db.Column(db.String(160), nullable=False)
    recipient = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(255), nullable=True)
    server_id = db.Column(db.Integer, nullable=True)
    inbound_id = db.Column(db.Integer, nullable=True)
    bot_instance_id = db.Column(db.Integer, db.ForeignKey('telegram_bot_instances.id'), nullable=True)
    context_json = db.Column(db.Text, nullable=False, default='{}')
    segment_count = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(24), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    resend_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(500), nullable=True)
    last_error_source = db.Column(db.String(24), nullable=True)
    gateway_request_id = db.Column(db.String(128), nullable=True, index=True)
    gateway_provider = db.Column(db.String(24), nullable=False, default='gmweb', index=True)
    gateway_state = db.Column(db.String(32), nullable=True)
    gateway_stage = db.Column(db.String(64), nullable=True)
    gateway_priority = db.Column(db.String(24), nullable=True)
    gateway_priority_level = db.Column(db.Integer, nullable=True)
    gateway_submitted_once = db.Column(db.Boolean, nullable=True)
    gateway_verification_status = db.Column(db.String(64), nullable=True)
    gateway_sent_to = db.Column(db.String(32), nullable=True)
    next_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    processed_at = db.Column(db.DateTime, nullable=True, index=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    announcement = db.relationship('Announcement', backref=db.backref(
        'deliveries', lazy=True, cascade='all, delete-orphan'))

    def context(self):
        try:
            value = json.loads(self.context_json or '{}')
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}


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
    config_url = db.Column(EncryptedText(), nullable=False)
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


# Business-event vocabulary (RenewalEvent v2). Kept next to the model so the
# analytics layer, the collector and the tests share one contract.
RENEWAL_EVENT_TYPES = (
    'renewal',
    'quota_topup',
    'traffic_reset',
    'package_change',
    'expiry_extension',
    'inferred_reset',
)
RENEWAL_EVENT_SOURCES = (
    'explicit_renew',
    'admin_reset',
    'telegram_renew',
    'api_mutation',
    'counter_reset',
    'inferred',
    'migration',
)
# Only a verified event of one of these types starts a recommendation cycle.
# A quota top-up adds volume to the current cycle; it does not restart it.
CYCLE_BOUNDARY_EVENT_TYPES = ('renewal', 'package_change')


class RenewalEvent(db.Model):
    """Business fact: what happened to a customer's commercial cycle (v2).

    The three layers are kept apart on purpose:

    * telemetry (``UsageCounterState`` / ``UsageHourly`` / ``UsageDaily``) answers
      "how much traffic was used?";
    * this table answers "when did the paid cycle restart, and on what terms?";
    * analytics (``panel/services/usage_intelligence``) combines both and never
      guesses one from the other.

    A raw counter decrease is therefore **not** a renewal: the collector records it as
    ``event_type='inferred_reset'`` / ``source='counter_reset'`` / ``verified=False``.
    Only a row with ``verified=True`` and an ``event_type`` in
    ``CYCLE_BOUNDARY_EVENT_TYPES`` may start a recommendation cycle - verified becomes
    true only after the panel write has been read back and matched.
    """
    __tablename__ = 'renewal_events'
    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'), nullable=False, index=True)
    sub_id = db.Column(db.String(128), nullable=False, index=True)
    client_uuid = db.Column(db.String(64), nullable=True)
    client_email_snapshot = db.Column(db.String(255), nullable=True)
    # Fail-safe defaults: a row that forgets to pass an event_type becomes an
    # unverified telemetry reset, never an authoritative cycle boundary.
    event_type = db.Column(db.String(32), nullable=False,
                           default='inferred_reset', server_default='inferred_reset')
    source = db.Column(db.String(32), nullable=False,
                       default='inferred', server_default='inferred')
    renewed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    volume_bytes = db.Column(db.BigInteger, nullable=True)
    days = db.Column(db.Integer, nullable=True)
    previous_volume_limit_bytes = db.Column(db.BigInteger, nullable=True)
    new_volume_limit_bytes = db.Column(db.BigInteger, nullable=True)
    previous_remaining_bytes = db.Column(db.BigInteger, nullable=True)
    carried_over_bytes = db.Column(db.BigInteger, nullable=True)
    granted_volume_bytes = db.Column(db.BigInteger, nullable=True)
    previous_expiry_at = db.Column(db.DateTime, nullable=True)
    new_expiry_at = db.Column(db.DateTime, nullable=True)
    traffic_reset = db.Column(db.Boolean, nullable=False,
                              default=False, server_default=db.text('false'))
    is_unlimited_volume = db.Column(db.Boolean, default=False)
    is_unlimited_time = db.Column(db.Boolean, default=False)
    operation_id = db.Column(db.String(64), nullable=True, index=True)
    verified = db.Column(db.Boolean, nullable=False,
                         default=False, server_default=db.text('false'))
    verified_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_renewal_events_server_sub', 'server_id', 'sub_id'),
        # Latest authoritative boundary per account: an indexed lookup, never a scan.
        db.Index('ix_renewal_events_server_sub_renewed', 'server_id', 'sub_id', 'renewed_at'),
        db.Index('ix_renewal_events_server_sub_verified_renewed',
                 'server_id', 'sub_id', 'verified', 'renewed_at'),
        # One event per operation and type: a retried renewal cannot open a second cycle.
        db.UniqueConstraint('operation_id', 'event_type',
                            name='uq_renewal_events_operation_type'),
    )

    @property
    def is_cycle_boundary(self) -> bool:
        """True when analytics may treat this event as the start of a new cycle."""
        return bool(self.verified) and self.event_type in CYCLE_BOUNDARY_EVENT_TYPES

    def to_dict(self, *, redact: bool = True) -> dict:
        """Analytics/debug view; the email snapshot is PII and stays out by default."""
        return {
            'id': self.id,
            'server_id': self.server_id,
            'sub_id': self.sub_id,
            'client_uuid': self.client_uuid if not redact else None,
            'client_email': None if redact else self.client_email_snapshot,
            'event_type': self.event_type,
            'source': self.source,
            'renewed_at': self.renewed_at.isoformat() if self.renewed_at else None,
            'volume_bytes': self.volume_bytes,
            'days': self.days,
            'previous_volume_limit_bytes': self.previous_volume_limit_bytes,
            'new_volume_limit_bytes': self.new_volume_limit_bytes,
            'previous_remaining_bytes': self.previous_remaining_bytes,
            'carried_over_bytes': self.carried_over_bytes,
            'granted_volume_bytes': self.granted_volume_bytes,
            'previous_expiry_at': self.previous_expiry_at.isoformat() if self.previous_expiry_at else None,
            'new_expiry_at': self.new_expiry_at.isoformat() if self.new_expiry_at else None,
            'traffic_reset': bool(self.traffic_reset),
            'is_unlimited_volume': bool(self.is_unlimited_volume),
            'is_unlimited_time': bool(self.is_unlimited_time),
            'operation_id': self.operation_id,
            'verified': bool(self.verified),
            'verified_at': self.verified_at.isoformat() if self.verified_at else None,
        }
