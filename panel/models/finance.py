"""Finance & ownership models (extracted from app.py)."""
import json
from datetime import datetime

from panel.core.phone import _normalize_contact_phone  # noqa: F401
from panel.extensions import db
from panel.models._helpers import _format_jalali  # noqa: F401
from panel.models.core import (  # noqa: F401
    RECEIPT_STATUS_APPROVED,
    RECEIPT_STATUS_AUTO_APPROVED,
    RECEIPT_STATUS_AUTO_PENDING,
    RECEIPT_STATUS_PENDING,
    RECEIPT_STATUS_REJECTED,
)

class ManualReceipt(db.Model):
    __tablename__ = 'manual_receipts'
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    card_id = db.Column(db.Integer, db.ForeignKey('bank_cards.id'))
    amount = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(10), default='IRT')
    deposit_at = db.Column(db.DateTime)
    reference_code = db.Column(db.String(120))
    image_path = db.Column(db.String(300))
    status = db.Column(db.String(32), default=RECEIPT_STATUS_PENDING, index=True)
    auto_deadline = db.Column(db.DateTime, index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('admins.id'))
    reviewed_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.String(255))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    admin = db.relationship('Admin', foreign_keys=[admin_id], backref=db.backref('receipts', lazy=True))
    reviewer = db.relationship('Admin', foreign_keys=[reviewer_id])
    card = db.relationship('BankCard', backref=db.backref('receipts', lazy=True))

    def to_dict(self):
        return {
            'id': self.id,
            'admin': {'id': self.admin.id, 'username': self.admin.username} if self.admin else None,
            'card': self.card.to_dict() if self.card else None,
            'amount': self.amount,
            'currency': self.currency,
            'deposit_at': self.deposit_at.isoformat() if self.deposit_at else None,
            'reference_code': self.reference_code,
            'image_path': self.image_path,
            'status': self.status,
            'auto_deadline': self.auto_deadline.isoformat() if self.auto_deadline else None,
            'reviewer': {'id': self.reviewer.id, 'username': self.reviewer.username} if self.reviewer else None,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'rejection_reason': self.rejection_reason,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class AutoApprovalWindow(db.Model):
    __tablename__ = 'auto_approval_windows'
    id = db.Column(db.Integer, primary_key=True)
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    max_amount = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='enabled')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def is_active(self, moment=None):
        moment = moment or datetime.utcnow()
        if self.status != 'enabled':
            return False
        return self.starts_at <= moment <= self.ends_at

    def to_dict(self):
        return {
            'id': self.id,
            'starts_at': self.starts_at.isoformat() if self.starts_at else None,
            'ends_at': self.ends_at.isoformat() if self.ends_at else None,
            'max_amount': self.max_amount,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class Payment(db.Model):
    """Track incoming payments from customers"""
    __tablename__ = 'payments'
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    card_id = db.Column(db.Integer, db.ForeignKey('bank_cards.id'), nullable=True)  # کارت مقصد (شما)
    sender_card = db.Column(db.String(32))  # شماره کارت مشتری (اختیاری)
    sender_name = db.Column(db.String(120))  # نام فرستنده
    amount = db.Column(db.Integer, nullable=False)  # مبلغ به تومان
    payment_date = db.Column(db.DateTime, nullable=False)  # تاریخ واریز
    client_email = db.Column(db.String(100))  # مربوط به کدوم کلاینت
    description = db.Column(db.Text)  # توضیحات
    verified = db.Column(db.Boolean, default=False)  # تایید شده؟
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    admin = db.relationship('Admin', backref=db.backref('payments', lazy=True))
    card = db.relationship('BankCard', backref=db.backref('payments', lazy=True))
    
    def to_dict(self):
        card_info = None
        if self.card:
            card_info = {
                'id': self.card.id,
                'label': self.card.label,
                'bank_name': self.card.bank_name,
                'masked_card': self.card.masked_card()
            }
        
        admin_info = None
        if self.admin:
            admin_info = {
                'id': self.admin.id,
                'username': self.admin.username,
                'role': self.admin.role
            }
        
        return {
            'id': self.id,
            'admin_id': self.admin_id,
            'admin': admin_info,
            'card_id': self.card_id,
            'card': card_info,
            'sender_card': self.sender_card,
            'sender_name': self.sender_name,
            'amount': self.amount,
            'payment_date': self.payment_date.isoformat() if self.payment_date else None,
            'payment_date_jalali': _format_jalali(self.payment_date),
            'client_email': self.client_email,
            'description': self.description,
            'verified': self.verified,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=False)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id'), nullable=True)
    card_id = db.Column(db.Integer, db.ForeignKey('bank_cards.id'), nullable=True)  # کارت مقصد (شما)
    sender_card = db.Column(db.String(32), nullable=True)  # شماره کارت مشتری
    sender_name = db.Column(db.String(120), nullable=True)  # نام فرستنده
    client_email = db.Column(db.String(100), nullable=True)  # ایمیل کلاینت مرتبط
    amount = db.Column(db.Integer, nullable=False)
    type = db.Column(db.String(20))
    category = db.Column(db.String(16), default='usage', nullable=False)  # 'income', 'expense', 'usage'
    description = db.Column(db.String(255))
    # Reseller-statement breakdown: what plan this purchase/renew was for. Filled
    # for purchase/renew rows; null for deposits/reset/audit rows.
    package_name = db.Column(db.String(120), nullable=True)  # package name, or 'Custom'
    volume_gb = db.Column(db.Integer, nullable=True)         # GB (0 = unlimited)
    days = db.Column(db.Integer, nullable=True)              # days (0 = unlimited)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    server = db.relationship('Server', backref='transactions', lazy=True)
    card = db.relationship('BankCard', backref='transactions', lazy=True)
    
    def to_dict(self):
        admin_info = None
        if hasattr(self, 'admin') and self.admin:
            admin_info = {
                'id': self.admin.id,
                'username': self.admin.username,
                'role': self.admin.role
            }
        
        server_info = None
        if self.server:
            server_info = {
                'id': self.server.id,
                'name': self.server.name
            }
        
        card_info = None
        if self.card:
            card_info = {
                'id': self.card.id,
                'label': self.card.label,
                'bank_name': self.card.bank_name,
                'masked_card': self.card.masked_card()
            }
            
        return {
            'id': self.id,
            'admin_id': self.admin_id,
            'server_id': self.server_id,
            'server': server_info,
            'card_id': self.card_id,
            'card': card_info,
            'sender_card': self.sender_card,
            'sender_name': self.sender_name,
            'client_email': self.client_email,
            'amount': self.amount,
            'type': self.type,
            'description': self.description,
            'package_name': self.package_name,
            'volume_gb': self.volume_gb,
            'days': self.days,
            'date': self.created_at.isoformat() if self.created_at else None,
            'date_jalali': _format_jalali(self.created_at),
            'admin': admin_info
        }

class CustomerAccount(db.Model):
    """Channel-independent identity for an end customer."""
    __tablename__ = 'customer_accounts'
    id = db.Column(db.Integer, primary_key=True)
    display_name = db.Column(db.String(120), nullable=True)
    primary_phone = db.Column(db.String(20), nullable=True, unique=True, index=True)
    phone_verified_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(24), nullable=False, default='active', index=True)
    risk_level = db.Column(db.String(24), nullable=False, default='standard', index=True)
    preferred_language = db.Column(db.String(12), nullable=False, default='fa')
    credit = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def set_primary_phone(self, raw_phone: str | None, *, verified=False,
                          allow_international: bool = False):
        canonical = _normalize_contact_phone(raw_phone, allow_international)
        if not canonical:
            raise ValueError('A valid mobile phone number is required')
        self.primary_phone = canonical
        if verified:
            self.phone_verified_at = datetime.utcnow()
        return canonical


class ServiceOwnership(db.Model):
    """The single end-customer owner of a stable panel client identity."""
    __tablename__ = 'service_ownerships'
    __table_args__ = (
        db.UniqueConstraint('server_id', 'client_uuid', name='uq_service_ownership_identity'),
    )
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer_accounts.id'), nullable=False, index=True)
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id'), nullable=False, index=True)
    client_uuid = db.Column(db.String(100), nullable=False)
    client_email_snapshot = db.Column(db.String(255), nullable=True, index=True)
    reseller_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True, index=True)
    verification_method = db.Column(db.String(32), nullable=False, default='admin')
    verified_by_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    verified_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('CustomerAccount', backref=db.backref('service_ownerships', lazy=True))
    server = db.relationship('Server')

    @property
    def is_active(self):
        return self.revoked_at is None


class ServiceDelegation(db.Model):
    """Revocable, limited access granted by a service owner to another customer."""
    __tablename__ = 'service_delegations'
    __table_args__ = (
        db.UniqueConstraint('service_ownership_id', 'delegate_customer_id',
                            name='uq_service_delegation_customer'),
    )
    ALLOWED_PERMISSIONS = frozenset({
        'view_status', 'receive_subscription', 'renew',
        'create_ticket', 'receive_notifications',
    })

    id = db.Column(db.Integer, primary_key=True)
    service_ownership_id = db.Column(
        db.Integer, db.ForeignKey('service_ownerships.id', ondelete='CASCADE'), nullable=False, index=True,
    )
    delegate_customer_id = db.Column(
        db.Integer, db.ForeignKey('customer_accounts.id'), nullable=False, index=True,
    )
    invited_by_customer_id = db.Column(
        db.Integer, db.ForeignKey('customer_accounts.id'), nullable=False, index=True,
    )
    permissions_json = db.Column(db.Text, nullable=False, default='{}')
    invite_token_hash = db.Column(db.String(64), nullable=True, unique=True, index=True)
    invite_expires_at = db.Column(db.DateTime, nullable=True)
    accepted_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    ownership = db.relationship('ServiceOwnership', backref=db.backref('delegations', lazy=True))

    def set_permissions(self, permissions: dict | None):
        source = permissions if isinstance(permissions, dict) else {}
        safe = {key: bool(source.get(key, False)) for key in sorted(self.ALLOWED_PERMISSIONS)}
        self.permissions_json = json.dumps(safe, separators=(',', ':'), sort_keys=True)
        return safe

    def get_permissions(self):
        try:
            source = json.loads(self.permissions_json or '{}')
        except (TypeError, ValueError):
            source = {}
        if not isinstance(source, dict):
            source = {}
        return {key: bool(source.get(key, False)) for key in sorted(self.ALLOWED_PERMISSIONS)}

    @property
    def is_active(self):
        return self.accepted_at is not None and self.revoked_at is None


class TelegramIdentity(db.Model):
    """A Telegram user identity, independent of any particular bot instance."""
    __tablename__ = 'telegram_identities'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer_accounts.id'), nullable=True, index=True)
    telegram_user_id = db.Column(db.BigInteger, nullable=False, unique=True, index=True)
    telegram_chat_id = db.Column(db.BigInteger, nullable=True, index=True)
    username = db.Column(db.String(64), nullable=True)
    first_name = db.Column(db.String(128), nullable=True)
    last_name = db.Column(db.String(128), nullable=True)
    phone_normalized = db.Column(db.String(20), nullable=True, index=True)
    phone_verified_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(24), nullable=False, default='active', index=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('CustomerAccount', backref=db.backref('telegram_identities', lazy=True))

    def set_verified_phone(self, raw_phone: str | None, *, allow_international: bool = False):
        canonical = _normalize_contact_phone(raw_phone, allow_international)
        if not canonical:
            raise ValueError('A valid mobile phone number is required')
        self.phone_normalized = canonical
        self.phone_verified_at = datetime.utcnow()
        return canonical


class OwnershipClaim(db.Model):
    """A customer request to attach one or more existing panel clients."""
    __tablename__ = 'ownership_claims'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer_accounts.id'), nullable=False, index=True)
    telegram_identity_id = db.Column(
        db.Integer, db.ForeignKey('telegram_identities.id'), nullable=False, index=True,
    )
    requested_reseller_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True, index=True)
    status = db.Column(db.String(32), nullable=False, default='pending', index=True)
    claim_method = db.Column(db.String(32), nullable=False, default='admin_review')
    verified_phone = db.Column(db.String(20), nullable=False, index=True)
    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship('CustomerAccount')
    telegram_identity = db.relationship('TelegramIdentity')


class OwnershipClaimItem(db.Model):
    """One candidate service within an ownership claim."""
    __tablename__ = 'ownership_claim_items'
    __table_args__ = (
        db.UniqueConstraint('claim_id', 'server_id', 'client_uuid', name='uq_claim_service_item'),
    )
    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(
        db.Integer, db.ForeignKey('ownership_claims.id', ondelete='CASCADE'), nullable=False, index=True,
    )
    server_id = db.Column(db.Integer, db.ForeignKey('servers.id'), nullable=False, index=True)
    client_uuid = db.Column(db.String(100), nullable=False)
    client_email_snapshot = db.Column(db.String(255), nullable=True, index=True)
    match_reason = db.Column(db.String(64), nullable=False, default='phone_match')
    match_score = db.Column(db.Integer, nullable=False, default=0)
    subscription_verified = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(32), nullable=False, default='pending', index=True)
    conflict_owner_id = db.Column(db.Integer, db.ForeignKey('customer_accounts.id'), nullable=True)
    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    claim = db.relationship('OwnershipClaim', backref=db.backref('items', lazy=True, cascade='all, delete-orphan'))
    server = db.relationship('Server')
