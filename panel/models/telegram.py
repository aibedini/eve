"""Telegram bot domain models (extracted from app.py)."""
import json
from datetime import datetime, timedelta

from panel.extensions import db

class TelegramBotInstance(db.Model):
    """Tenant-scoped interactive Telegram bot configuration."""
    __tablename__ = 'telegram_bot_instances'
    id = db.Column(db.Integer, primary_key=True)
    scope_key = db.Column(db.String(80), nullable=False, unique=True, index=True)
    owner_type = db.Column(db.String(20), nullable=False, default='system', index=True)
    owner_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True, index=True)
    display_name = db.Column(db.String(120), nullable=False, default='Eve Central Bot')
    token_encrypted = db.Column(db.Text, nullable=True)
    bot_user_id = db.Column(db.BigInteger, nullable=True, index=True)
    bot_username = db.Column(db.String(64), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    test_mode = db.Column(db.Boolean, nullable=False, default=True)
    enabled_languages_json = db.Column(db.Text, nullable=False, default='["fa","en"]')
    copy_overrides_json = db.Column(db.Text, nullable=False, default='')
    default_language = db.Column(db.String(12), nullable=False, default='fa')
    connection_mode = db.Column(db.String(24), nullable=False, default='proxy_first')
    transport_mode = db.Column(db.String(24), nullable=False, default='polling')
    support_group_enabled = db.Column(db.Boolean, nullable=False, default=False)
    support_group_chat_id = db.Column(db.BigInteger, nullable=True)
    support_group_topics = db.Column(db.Boolean, nullable=False, default=True)
    support_sla_minutes = db.Column(db.Integer, nullable=False, default=60)
    support_sla_warning_percent = db.Column(db.Integer, nullable=False, default=80)
    support_escalation_minutes = db.Column(db.Integer, nullable=False, default=30)
    required_channels_json = db.Column(db.Text, nullable=False, default='[]')
    require_membership_on_start = db.Column(db.Boolean, nullable=False, default=False)
    require_membership_on_delivery = db.Column(db.Boolean, nullable=False, default=False)
    phone_allow_international = db.Column(db.Boolean, nullable=False, default=False)
    last_test_status = db.Column(db.String(24), nullable=True)
    last_test_route = db.Column(db.String(120), nullable=True)
    last_test_latency_ms = db.Column(db.Integer, nullable=True)
    last_test_error = db.Column(db.Text, nullable=True)
    last_test_at = db.Column(db.DateTime, nullable=True)
    archived_at = db.Column(db.DateTime, nullable=True, index=True)
    archived_by_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def enabled_languages(self):
        try:
            values = json.loads(self.enabled_languages_json or '[]')
        except (TypeError, ValueError):
            values = []
        return [lang for lang in ('fa', 'en') if lang in values] or ['fa']

    def required_channels(self):
        try:
            values = json.loads(self.required_channels_json or '[]')
        except (TypeError, ValueError):
            values = []
        return values if isinstance(values, list) else []

    def to_safe_dict(self):
        return {
            'id': self.id,
            'scope_key': self.scope_key,
            'owner_type': self.owner_type,
            'owner_admin_id': self.owner_admin_id,
            'display_name': self.display_name,
            'token_configured': bool(self.token_encrypted),
            'bot_user_id': self.bot_user_id,
            'bot_username': self.bot_username,
            'enabled': bool(self.enabled),
            'test_mode': bool(self.test_mode),
            'enabled_languages': self.enabled_languages(),
            'default_language': self.default_language,
            'connection_mode': self.connection_mode,
            'transport_mode': self.transport_mode,
            'support_group_enabled': bool(self.support_group_enabled),
            'support_group_chat_id': self.support_group_chat_id,
            'support_group_topics': bool(self.support_group_topics),
            'support_sla_minutes': max(0, int(self.support_sla_minutes or 0)),
            'support_sla_warning_percent': max(1, min(99, int(self.support_sla_warning_percent or 80))),
            'support_escalation_minutes': max(0, int(self.support_escalation_minutes or 0)),
            'required_channels': self.required_channels(),
            'require_membership_on_start': bool(self.require_membership_on_start),
            'require_membership_on_delivery': bool(self.require_membership_on_delivery),
            'phone_allow_international': bool(self.phone_allow_international),
            'last_test_status': self.last_test_status,
            'last_test_route': self.last_test_route,
            'last_test_latency_ms': self.last_test_latency_ms,
            'last_test_error': self.last_test_error,
            'last_test_at': self.last_test_at.isoformat() if self.last_test_at else None,
            'archived': self.archived_at is not None,
            'archived_at': self.archived_at.isoformat() if self.archived_at else None,
        }


class TelegramBotRuntime(db.Model):
    """Durable polling cursor and health state for one Telegram bot."""
    __tablename__ = 'telegram_bot_runtimes'
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True,
    )
    next_update_id = db.Column(db.BigInteger, nullable=False, default=0)
    status = db.Column(db.String(24), nullable=False, default='stopped', index=True)
    worker_id = db.Column(db.String(64), nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)
    last_heartbeat_at = db.Column(db.DateTime, nullable=True)
    last_update_at = db.Column(db.DateTime, nullable=True)
    last_route = db.Column(db.String(120), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    failed_update_id = db.Column(db.BigInteger, nullable=True)
    failed_update_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = db.relationship('TelegramBotInstance', backref=db.backref(
        'runtime', uselist=False, cascade='all, delete-orphan',
    ))

    def to_safe_dict(self):
        heartbeat = self.last_heartbeat_at
        status = self.status
        if status == 'running' and (not heartbeat or heartbeat < datetime.utcnow() - timedelta(seconds=90)):
            status = 'stale'
        return {
            'status': status,
            'next_update_id': int(self.next_update_id or 0),
            'worker_id': self.worker_id,
            'failed_update_count': int(self.failed_update_count or 0),
            'last_heartbeat_at': heartbeat.isoformat() if heartbeat else None,
            'last_update_at': self.last_update_at.isoformat() if self.last_update_at else None,
            'last_route': self.last_route,
            'last_error': self.last_error,
        }


class TelegramBotTestUser(db.Model):
    """A Telegram user explicitly allowed while a bot is in test mode."""
    __tablename__ = 'telegram_bot_test_users'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'telegram_user_id', name='uq_telegram_bot_test_user'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    label = db.Column(db.String(120), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = db.relationship('TelegramBotInstance', backref=db.backref(
        'test_users', lazy=True, cascade='all, delete-orphan',
    ))

    def to_safe_dict(self):
        return {
            'id': self.id,
            'telegram_user_id': int(self.telegram_user_id),
            'label': self.label,
            'enabled': bool(self.enabled),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class TelegramBotUserState(db.Model):
    """Per-bot onboarding state without changing the global Telegram identity."""
    __tablename__ = 'telegram_bot_user_states'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'telegram_user_id', name='uq_telegram_bot_user_state'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    language = db.Column(db.String(12), nullable=False, default='fa')
    step = db.Column(db.String(32), nullable=False, default='new', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = db.relationship('TelegramBotInstance', backref=db.backref(
        'user_states', lazy=True, cascade='all, delete-orphan',
    ))


class TelegramBotStartEvent(db.Model):
    """One /start invocation, classified at the time it was received."""
    __tablename__ = 'telegram_bot_start_events'
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    is_new_user = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class TelegramAnnouncement(db.Model):
    """Durable, resumable Telegram broadcast with an immutable audience filter."""
    __tablename__ = 'telegram_announcements'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    message_text = db.Column(db.Text, nullable=False)
    filters_json = db.Column(db.Text, nullable=False, default='{}')
    status = db.Column(db.String(24), nullable=False, default='draft', index=True)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False, index=True)
    source_bot_instance_id = db.Column(db.Integer, db.ForeignKey('telegram_bot_instances.id'), nullable=True)
    total_count = db.Column(db.Integer, nullable=False, default=0)
    sent_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    blocked_count = db.Column(db.Integer, nullable=False, default=0)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def filters(self):
        try:
            value = json.loads(self.filters_json or '{}')
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def to_safe_dict(self):
        return {
            'id': self.id, 'title': self.title, 'message_text': self.message_text,
            'filters': self.filters(), 'status': self.status,
            'created_by_admin_id': self.created_by_admin_id,
            'source_bot_instance_id': self.source_bot_instance_id,
            'total_count': int(self.total_count or 0), 'sent_count': int(self.sent_count or 0),
            'failed_count': int(self.failed_count or 0), 'blocked_count': int(self.blocked_count or 0),
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class TelegramAnnouncementDelivery(db.Model):
    """One idempotent delivery record per campaign, bot, and Telegram user."""
    __tablename__ = 'telegram_announcement_deliveries'
    __table_args__ = (db.UniqueConstraint(
        'announcement_id', 'bot_instance_id', 'telegram_user_id',
        name='uq_telegram_announcement_delivery'),)
    id = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey(
        'telegram_announcements.id', ondelete='CASCADE'), nullable=False, index=True)
    bot_instance_id = db.Column(db.Integer, db.ForeignKey(
        'telegram_bot_instances.id', ondelete='CASCADE'), nullable=False, index=True)
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    chat_id = db.Column(db.BigInteger, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer_accounts.id'), nullable=True, index=True)
    status = db.Column(db.String(24), nullable=False, default='pending', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(500), nullable=True)
    next_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    announcement = db.relationship('TelegramAnnouncement', backref=db.backref(
        'deliveries', lazy=True, cascade='all, delete-orphan'))


class TelegramOwnershipSession(db.Model):
    """Ephemeral selection state while a Telegram user proves one service."""
    __tablename__ = 'telegram_ownership_sessions'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'telegram_user_id', name='uq_telegram_ownership_session'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    claim_id = db.Column(db.Integer, db.ForeignKey('ownership_claims.id', ondelete='CASCADE'), nullable=False)
    selected_item_id = db.Column(
        db.Integer, db.ForeignKey('ownership_claim_items.id', ondelete='CASCADE'), nullable=True,
    )
    failed_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TelegramServiceSession(db.Model):
    """Current service/action selected by a Telegram user."""
    __tablename__ = 'telegram_service_sessions'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'telegram_user_id', name='uq_telegram_service_session'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    service_ownership_id = db.Column(
        db.Integer, db.ForeignKey('service_ownerships.id', ondelete='CASCADE'),
        nullable=True, index=True,
    )
    action = db.Column(db.String(32), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TelegramServiceRequest(db.Model):
    """Durable manual renewal/support request created from Telegram."""
    __tablename__ = 'telegram_service_requests'
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey('customer_accounts.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    service_ownership_id = db.Column(
        db.Integer, db.ForeignKey('service_ownerships.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    request_type = db.Column(db.String(24), nullable=False, index=True)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id', ondelete='SET NULL'), nullable=True)
    amount = db.Column(db.BigInteger, nullable=True)
    original_amount = db.Column(db.BigInteger, nullable=True)
    discount_amount = db.Column(db.BigInteger, nullable=True)
    promo_code = db.Column(db.String(64), nullable=True)
    note = db.Column(db.Text, nullable=True)
    bank_card_id = db.Column(db.Integer, db.ForeignKey('bank_cards.id', ondelete='SET NULL'), nullable=True)
    receipt_file_id = db.Column(db.Text, nullable=True)
    receipt_file_kind = db.Column(db.String(16), nullable=True)
    receipt_file_unique_id = db.Column(db.String(160), nullable=True, index=True)
    duplicate_receipt = db.Column(db.Boolean, nullable=False, default=False)
    payment_method = db.Column(db.String(16), nullable=False, default='card')
    status = db.Column(db.String(24), nullable=False, default='pending', index=True)
    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    assigned_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True, index=True)
    support_priority = db.Column(db.String(16), nullable=False, default='normal', index=True)
    first_response_at = db.Column(db.DateTime, nullable=True)
    sla_warning_message_id = db.Column(db.Integer, nullable=True)
    sla_escalated_message_id = db.Column(db.Integer, nullable=True)
    sla_warning_at = db.Column(db.DateTime, nullable=True)
    sla_escalated_at = db.Column(db.DateTime, nullable=True)
    support_group_chat_id = db.Column(db.BigInteger, nullable=True)
    support_message_thread_id = db.Column(db.BigInteger, nullable=True)
    support_group_message_id = db.Column(db.BigInteger, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    ownership = db.relationship('ServiceOwnership')
    package = db.relationship('Package')
    bot = db.relationship('TelegramBotInstance')
    assigned_admin = db.relationship('Admin', foreign_keys=[assigned_admin_id])
    bank_card = db.relationship('BankCard')


class TelegramServiceRequestMessage(db.Model):
    """Durable operator/customer conversation attached to a Telegram support request."""
    __tablename__ = 'telegram_service_request_messages'
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(
        db.Integer, db.ForeignKey('telegram_service_requests.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    sender_type = db.Column(db.String(16), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id', ondelete='SET NULL'), nullable=True)
    message = db.Column(db.Text, nullable=False, default='')
    attachment_kind = db.Column(db.String(24), nullable=True)
    attachment_file_id = db.Column(db.Text, nullable=True)
    attachment_file_unique_id = db.Column(db.String(255), nullable=True)
    attachment_name = db.Column(db.String(255), nullable=True)
    attachment_mime = db.Column(db.String(127), nullable=True)
    attachment_size = db.Column(db.BigInteger, nullable=True)
    source_chat_id = db.Column(db.BigInteger, nullable=True)
    source_message_id = db.Column(db.BigInteger, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    admin = db.relationship('Admin')
    request = db.relationship('TelegramServiceRequest', backref=db.backref(
        'messages', lazy=True, cascade='all, delete-orphan', order_by='TelegramServiceRequestMessage.created_at',
    ))


class TelegramPurchaseSession(db.Model):
    """Durable in-progress server/package selection for a Telegram purchase."""
    __tablename__ = 'telegram_purchase_sessions'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'telegram_user_id', name='uq_telegram_purchase_session'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'), nullable=True)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id', ondelete='SET NULL'), nullable=True)
    bank_card_id = db.Column(db.Integer, db.ForeignKey('bank_cards.id', ondelete='SET NULL'), nullable=True)
    quoted_amount = db.Column(db.Integer, nullable=True)  # price frozen at payment time (Wave D promo base)
    promo_id = db.Column(db.Integer, nullable=True)  # primary applied promo at freeze time
    promo_code = db.Column(db.String(64), nullable=True)  # customer-entered code
    discount_amount = db.Column(db.Integer, nullable=True)
    promo_discounts_json = db.Column(db.Text, nullable=True)  # {promo_id: discount} applied at freeze
    action = db.Column(db.String(32), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TelegramPurchaseRequest(db.Model):
    """Customer purchase awaiting manual receipt review in Telegram."""
    __tablename__ = 'telegram_purchase_requests'
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey('customer_accounts.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'), nullable=False)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id', ondelete='SET NULL'), nullable=True)
    bank_card_id = db.Column(db.Integer, db.ForeignKey('bank_cards.id', ondelete='SET NULL'), nullable=True)
    amount = db.Column(db.BigInteger, nullable=False)
    receipt_file_id = db.Column(db.Text, nullable=False)
    receipt_file_unique_id = db.Column(db.String(160), nullable=True, index=True)
    receipt_kind = db.Column(db.String(16), nullable=False, default='photo')
    source_chat_id = db.Column(db.BigInteger, nullable=False)
    source_message_id = db.Column(db.BigInteger, nullable=False)
    status = db.Column(db.String(24), nullable=False, default='pending', index=True)
    duplicate_receipt = db.Column(db.Boolean, nullable=False, default=False)
    payment_method = db.Column(db.String(16), nullable=False, default='card')
    original_amount = db.Column(db.BigInteger, nullable=True)
    discount_amount = db.Column(db.BigInteger, nullable=True)
    promo_code = db.Column(db.String(64), nullable=True)
    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    server = db.relationship('Server')
    package = db.relationship('Package')
    bank_card = db.relationship('BankCard')


class CustomerTransaction(db.Model):
    """Signed wallet ledger entry for an end customer."""
    __tablename__ = 'customer_transactions'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey('customer_accounts.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    type = db.Column(db.String(24), nullable=False, index=True)  # topup|purchase|renewal|refund|adjust
    amount = db.Column(db.Integer, nullable=False)
    bank_card_id = db.Column(db.Integer, db.ForeignKey('bank_cards.id', ondelete='SET NULL'), nullable=True)
    receipt_file_id = db.Column(db.Text, nullable=True)
    receipt_file_kind = db.Column(db.String(16), nullable=True)
    receipt_file_unique_id = db.Column(db.String(160), nullable=True)
    status = db.Column(db.String(16), nullable=False, default='completed')
    description = db.Column(db.String(255), nullable=True)
    request_ref = db.Column(db.String(64), nullable=True, index=True)  # e.g. 'purchase:12'
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    customer = db.relationship('CustomerAccount')
    bank_card = db.relationship('BankCard')


class TelegramWalletTopup(db.Model):
    """Customer wallet top-up awaiting manual receipt review in Telegram."""
    __tablename__ = 'telegram_wallet_topups'
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey('customer_accounts.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    bank_card_id = db.Column(db.Integer, db.ForeignKey('bank_cards.id', ondelete='SET NULL'), nullable=True)
    amount = db.Column(db.Integer, nullable=False)
    receipt_file_id = db.Column(db.Text, nullable=False)
    receipt_file_kind = db.Column(db.String(16), nullable=False, default='photo')
    receipt_file_unique_id = db.Column(db.String(160), nullable=True, index=True)
    duplicate_receipt = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    reviewer_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    customer = db.relationship('CustomerAccount')
    bank_card = db.relationship('BankCard')


class TelegramPurchasePolicy(db.Model):
    """Per-bot purchase allocation and account naming policy."""
    __tablename__ = 'telegram_purchase_policies'
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        primary_key=True,
    )
    customer_selects_server = db.Column(db.Boolean, nullable=False, default=False)
    assignment_strategy = db.Column(db.String(32), nullable=False, default='least_clients')
    account_name_mode = db.Column(db.String(24), nullable=False, default='generated')
    account_name_template = db.Column(
        db.String(120), nullable=False, default='tg{order_id}-{phone_last4}',
    )
    trial_enabled = db.Column(db.Boolean, nullable=False, default=False)
    trial_package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=True)
    trial_requires_channel_membership = db.Column(db.Boolean, nullable=False, default=False)
    trial_channel_chat_id = db.Column(db.BigInteger, nullable=True)
    trial_channel_list_json = db.Column(db.Text, nullable=False, default='')
    emergency_enabled = db.Column(db.Boolean, nullable=False, default=False)
    emergency_days = db.Column(db.Integer, nullable=False, default=1)
    emergency_volume_gb = db.Column(db.Integer, nullable=False, default=1)
    emergency_cooldown_days = db.Column(db.Integer, nullable=False, default=30)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_safe_dict(self):
        return {
            'customer_selects_server': bool(self.customer_selects_server),
            'assignment_strategy': self.assignment_strategy or 'least_clients',
            'account_name_mode': self.account_name_mode or 'generated',
            'account_name_template': self.account_name_template or 'tg{order_id}-{phone_last4}',
            'trial_enabled': bool(self.trial_enabled),
            'trial_package_id': self.trial_package_id,
            'trial_requires_channel_membership': bool(self.trial_requires_channel_membership),
            'trial_channel_chat_id': self.trial_channel_chat_id,
            'trial_channels': self.trial_channels(),
            'emergency_enabled': bool(self.emergency_enabled),
            'emergency_days': max(1, int(self.emergency_days or 1)),
            'emergency_volume_gb': max(0, int(self.emergency_volume_gb or 1)),
            'emergency_cooldown_days': max(1, int(self.emergency_cooldown_days or 30)),
        }

    def trial_channels(self):
        """Parsed multi-channel trial gate list: [{chat_id, title, invite_url}]."""
        try:
            raw = json.loads(self.trial_channel_list_json or '[]')
        except (TypeError, ValueError):
            raw = []
        channels = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                chat_id = int(item.get('chat_id'))
            except (TypeError, ValueError):
                continue
            channels.append({
                'chat_id': chat_id,
                'title': str(item.get('title') or '').strip()[:200],
                'invite_url': str(item.get('invite_url') or '').strip()[:200],
            })
        return channels


class TelegramTrialGrant(db.Model):
    """Durable abuse ledger for free trial and emergency-access grants.

    One 'trial' grant per phone number (and per telegram user) per bot, and one
    'emergency' grant per ownership per cooldown window — both enforced in code
    inside the granting transaction."""
    __tablename__ = 'telegram_trial_grants'
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    phone_normalized = db.Column(db.String(20), nullable=False, default='', index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer_accounts.id'), nullable=True)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=True)
    ownership_id = db.Column(db.Integer, db.ForeignKey('service_ownerships.id'), nullable=True)
    kind = db.Column(db.String(16), nullable=False, default='trial', index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class TelegramPromo(db.Model):
    """Rule-based promotion: optional code (NULL = automatic), percent/fixed
    action, bot/package scope, conditions, usage limits, and stacking order."""
    __tablename__ = 'telegram_promos'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), nullable=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    kind = db.Column(db.String(16), nullable=False, default='percent')  # percent | fixed
    value = db.Column(db.Float, nullable=False, default=0)
    max_discount_amount = db.Column(db.BigInteger, nullable=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id'), nullable=True, index=True)
    package_id = db.Column(db.Integer, db.ForeignKey('packages.id'), nullable=True, index=True)
    applies_to = db.Column(db.String(16), nullable=False, default='both')  # purchase | renewal | both
    min_amount = db.Column(db.BigInteger, nullable=True)
    first_purchase_only = db.Column(db.Boolean, nullable=False, default=False)
    min_purchases_30d = db.Column(db.Integer, nullable=True)
    min_purchases_90d = db.Column(db.Integer, nullable=True)
    min_referrals = db.Column(db.Integer, nullable=True)
    requires_channel_chat_id = db.Column(db.BigInteger, nullable=True)
    starts_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    max_uses_total = db.Column(db.Integer, nullable=True)
    max_uses_per_user = db.Column(db.Integer, nullable=True)
    stackable = db.Column(db.Boolean, nullable=False, default=False)
    priority = db.Column(db.Integer, nullable=False, default=0)
    apply_on_reseller_pricing = db.Column(db.Boolean, nullable=False, default=True)
    owner_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_safe_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'description': self.description,
            'kind': self.kind,
            'value': float(self.value or 0),
            'max_discount_amount': self.max_discount_amount,
            'bot_instance_id': self.bot_instance_id,
            'package_id': self.package_id,
            'applies_to': self.applies_to or 'both',
            'min_amount': self.min_amount,
            'first_purchase_only': bool(self.first_purchase_only),
            'min_purchases_30d': self.min_purchases_30d,
            'min_purchases_90d': self.min_purchases_90d,
            'min_referrals': self.min_referrals,
            'requires_channel_chat_id': self.requires_channel_chat_id,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'max_uses_total': self.max_uses_total,
            'max_uses_per_user': self.max_uses_per_user,
            'stackable': bool(self.stackable),
            'priority': int(self.priority or 0),
            'apply_on_reseller_pricing': bool(self.apply_on_reseller_pricing),
            'owner_admin_id': self.owner_admin_id,
            'enabled': bool(self.enabled),
        }


class TelegramPromoUse(db.Model):
    """Durable record of one applied promo — usage-limit enforcement and stats."""
    __tablename__ = 'telegram_promo_uses'
    __table_args__ = (
        db.Index('ix_telegram_promo_uses_promo_user', 'promo_id', 'telegram_user_id'),
    )
    id = db.Column(db.Integer, primary_key=True)
    promo_id = db.Column(
        db.Integer, db.ForeignKey('telegram_promos.id', ondelete='CASCADE'),
        nullable=False, index=True)
    telegram_user_id = db.Column(db.BigInteger, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer_accounts.id'), nullable=True)
    purchase_request_id = db.Column(db.Integer, nullable=True)
    amount_discounted = db.Column(db.BigInteger, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class TelegramReferral(db.Model):
    """One inviter per telegram user; qualified when the invitee verifies a phone."""
    __tablename__ = 'telegram_referrals'
    id = db.Column(db.Integer, primary_key=True)
    referrer_telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    referee_telegram_user_id = db.Column(db.BigInteger, nullable=False, unique=True)
    qualified_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class TelegramPurchaseServerRule(db.Model):
    """Eligibility, customer visibility, and allocation weight for one server."""
    __tablename__ = 'telegram_purchase_server_rules'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'server_id', name='uq_telegram_purchase_server_rule'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    server_id = db.Column(
        db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    eligible = db.Column(db.Boolean, nullable=False, default=True)
    customer_visible = db.Column(db.Boolean, nullable=False, default=False)
    display_name = db.Column(db.String(120), nullable=True)
    priority = db.Column(db.Integer, nullable=False, default=100)
    weight = db.Column(db.Integer, nullable=False, default=1)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    server = db.relationship('Server')


class TelegramPurchaseInboundRoute(db.Model):
    """Inbound allocation policy for one bot/package/server combination."""
    __tablename__ = 'telegram_purchase_inbound_routes'
    __table_args__ = (
        db.UniqueConstraint(
            'bot_instance_id', 'package_id', 'server_id',
            name='uq_telegram_purchase_inbound_route',
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    package_id = db.Column(
        db.Integer, db.ForeignKey('packages.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    server_id = db.Column(
        db.Integer, db.ForeignKey('servers.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    mode = db.Column(db.String(24), nullable=False, default='manual')
    inbound_ids_json = db.Column(db.Text, nullable=False, default='[]')
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    package = db.relationship('Package')
    server = db.relationship('Server')

    def inbound_ids(self):
        try:
            values = json.loads(self.inbound_ids_json or '[]')
        except (TypeError, ValueError):
            values = []
        result = []
        for value in values if isinstance(values, list) else []:
            try:
                inbound_id = int(value)
            except (TypeError, ValueError):
                continue
            if inbound_id > 0 and inbound_id not in result:
                result.append(inbound_id)
        return sorted(result)

    def to_safe_dict(self):
        return {
            'id': self.id,
            'package_id': self.package_id,
            'server_id': self.server_id,
            'mode': self.mode or 'manual',
            'inbound_ids': self.inbound_ids(),
            'enabled': bool(self.enabled),
        }


class TelegramPurchaseNameDraft(db.Model):
    """Sanitized customer account name kept while a receipt is pending."""
    __tablename__ = 'telegram_purchase_name_drafts'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'telegram_user_id', name='uq_telegram_purchase_name_draft'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    telegram_user_id = db.Column(db.BigInteger, nullable=False, index=True)
    requested_name = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TelegramPurchaseRequestDetail(db.Model):
    """Provisioning metadata frozen at purchase submission time."""
    __tablename__ = 'telegram_purchase_request_details'
    request_id = db.Column(
        db.Integer, db.ForeignKey('telegram_purchase_requests.id', ondelete='CASCADE'),
        primary_key=True,
    )
    account_name = db.Column(db.String(64), nullable=False)
    allocation_strategy = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    request = db.relationship('TelegramPurchaseRequest', backref=db.backref(
        'detail', uselist=False, cascade='all, delete-orphan',
    ))


class TelegramPurchaseRequestAllocation(db.Model):
    """Immutable inbound choice used by provisioning retries for one purchase."""
    __tablename__ = 'telegram_purchase_request_allocations'
    request_id = db.Column(
        db.Integer, db.ForeignKey('telegram_purchase_requests.id', ondelete='CASCADE'),
        primary_key=True,
    )
    route_id = db.Column(
        db.Integer, db.ForeignKey('telegram_purchase_inbound_routes.id', ondelete='SET NULL'),
        nullable=True,
    )
    mode = db.Column(db.String(24), nullable=False)
    inbound_ids_json = db.Column(db.Text, nullable=False)
    detected_signature = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    route = db.relationship('TelegramPurchaseInboundRoute')
    request = db.relationship('TelegramPurchaseRequest', backref=db.backref(
        'inbound_allocation', uselist=False, cascade='all, delete-orphan',
    ))

    def inbound_ids(self):
        try:
            values = json.loads(self.inbound_ids_json or '[]')
        except (TypeError, ValueError):
            values = []
        result = []
        for value in values if isinstance(values, list) else []:
            try:
                inbound_id = int(value)
            except (TypeError, ValueError):
                continue
            if inbound_id > 0 and inbound_id not in result:
                result.append(inbound_id)
        return sorted(result)


class TelegramProxyEndpoint(db.Model):
    """One encrypted proxy candidate in a bot's failover pool."""
    __tablename__ = 'telegram_proxy_endpoints'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'host', 'port', name='uq_telegram_bot_proxy'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    proxy_type = db.Column(db.String(16), nullable=False, default='socks5')
    host = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    username_encrypted = db.Column(db.Text, nullable=True)
    password_encrypted = db.Column(db.Text, nullable=True)
    priority = db.Column(db.Integer, nullable=False, default=100, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    health_status = db.Column(db.String(24), nullable=False, default='unknown', index=True)
    last_latency_ms = db.Column(db.Integer, nullable=True)
    failure_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)
    last_success_at = db.Column(db.DateTime, nullable=True)
    last_failure_at = db.Column(db.DateTime, nullable=True)
    cooldown_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = db.relationship('TelegramBotInstance', backref=db.backref(
        'proxy_endpoints', lazy=True, cascade='all, delete-orphan',
    ))

    def to_safe_dict(self):
        return {
            'id': self.id,
            'proxy_type': self.proxy_type,
            'host': self.host,
            'port': self.port,
            'username_configured': bool(self.username_encrypted),
            'password_configured': bool(self.password_encrypted),
            'priority': self.priority,
            'enabled': bool(self.enabled),
            'health_status': self.health_status,
            'last_latency_ms': self.last_latency_ms,
            'failure_count': int(self.failure_count or 0),
            'last_error': self.last_error,
            'last_success_at': self.last_success_at.isoformat() if self.last_success_at else None,
            'last_failure_at': self.last_failure_at.isoformat() if self.last_failure_at else None,
            'cooldown_until': self.cooldown_until.isoformat() if self.cooldown_until else None,
        }


class TelegramEgressProfile(db.Model):
    """A managed Xray client exposed only as a loopback SOCKS route."""
    __tablename__ = 'telegram_egress_profiles'
    __table_args__ = (
        db.UniqueConstraint('bot_instance_id', 'local_port', name='uq_telegram_egress_port'),
    )
    id = db.Column(db.Integer, primary_key=True)
    bot_instance_id = db.Column(
        db.Integer, db.ForeignKey('telegram_bot_instances.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    source_type = db.Column(db.String(24), nullable=False, default='manual_uri')
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id', ondelete='SET NULL'), nullable=True)
    inbound_id = db.Column(db.Integer, nullable=True)
    client_email_snapshot = db.Column(db.String(255), nullable=True)
    protocol = db.Column(db.String(24), nullable=False, default='vless')
    config_encrypted = db.Column(db.Text, nullable=False)
    local_port = db.Column(db.Integer, nullable=False, default=12080)
    priority = db.Column(db.Integer, nullable=False, default=50, index=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, index=True)
    runtime_status = db.Column(db.String(32), nullable=False, default='pending')
    runtime_pid = db.Column(db.Integer, nullable=True)
    health_status = db.Column(db.String(24), nullable=False, default='unknown')
    last_latency_ms = db.Column(db.Integer, nullable=True)
    failure_count = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text, nullable=True)
    last_success_at = db.Column(db.DateTime, nullable=True)
    last_failure_at = db.Column(db.DateTime, nullable=True)
    last_heartbeat_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    bot = db.relationship('TelegramBotInstance', backref=db.backref(
        'egress_profiles', lazy=True, cascade='all, delete-orphan',
    ))
    server = db.relationship('Server')

    def to_safe_dict(self):
        return {
            'id': self.id, 'name': self.name, 'source_type': self.source_type,
            'server_id': self.server_id, 'server_name': self.server.name if self.server else None,
            'inbound_id': self.inbound_id, 'client_email': self.client_email_snapshot,
            'protocol': self.protocol, 'config_configured': bool(self.config_encrypted),
            'local_host': '127.0.0.1', 'local_port': self.local_port,
            'priority': self.priority, 'enabled': bool(self.enabled),
            'runtime_status': self.runtime_status, 'health_status': self.health_status,
            'last_latency_ms': self.last_latency_ms,
            'failure_count': int(self.failure_count or 0), 'last_error': self.last_error,
            'last_success_at': self.last_success_at.isoformat() if self.last_success_at else None,
            'last_failure_at': self.last_failure_at.isoformat() if self.last_failure_at else None,
            'last_heartbeat_at': self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None,
        }
