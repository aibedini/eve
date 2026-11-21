"""Delegated support access and operator accounting."""

from datetime import datetime

from panel.extensions import db


class SupportRole(db.Model):
    __tablename__ = 'support_roles'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    permissions = db.Column(db.JSON, nullable=False)


class SupportOperator(db.Model):
    __tablename__ = 'support_operators'

    admin_id = db.Column(db.Integer, db.ForeignKey('admins.id', ondelete='CASCADE'), primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('admins.id'), nullable=True, index=True)
    role_id = db.Column(db.Integer, db.ForeignKey('support_roles.id'), nullable=True)
    overrides = db.Column(db.JSON, nullable=False, default=dict)
    locations = db.Column(db.JSON, nullable=False)
    account_filter = db.Column(db.JSON, nullable=False, default=dict)
    sms_limit = db.Column(db.Integer, nullable=False, default=0)
    free_limit = db.Column(db.Integer, nullable=False, default=0)
    free_max_gb = db.Column(db.Integer, nullable=False, default=0)
    role = db.relationship('SupportRole')


class SupportContract(db.Model):
    __tablename__ = 'support_contracts'

    id = db.Column(db.Integer, primary_key=True)
    operator_id = db.Column(db.Integer, nullable=False, index=True)
    owner_id = db.Column(db.Integer, nullable=True)
    terms = db.Column(db.JSON, nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class SupportActivity(db.Model):
    __tablename__ = 'support_activity'

    id = db.Column(db.Integer, primary_key=True)
    operator_id = db.Column(db.Integer, nullable=False, index=True)
    owner_id = db.Column(db.Integer, nullable=True, index=True)
    kind = db.Column(db.String(32), nullable=False)
    reference = db.Column(db.String(196), nullable=False, unique=True)
    customer_id = db.Column(db.Integer, nullable=True)
    server_id = db.Column(db.Integer, nullable=True)
    client_uuid = db.Column(db.String(100), nullable=True)
    gateway_request_id = db.Column(db.String(120), nullable=True, index=True)
    state = db.Column(db.String(24), nullable=False)
    details = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class SupportBudget(db.Model):
    __tablename__ = 'support_budgets'

    scope = db.Column(db.String(64), primary_key=True)
    day = db.Column(db.Date, primary_key=True)
    used = db.Column(db.Integer, nullable=False)


class SupportPayroll(db.Model):
    __tablename__ = 'support_payroll'
    __table_args__ = (db.UniqueConstraint('operator_id', 'period', name='uq_support_payroll_period'),)

    id = db.Column(db.Integer, primary_key=True)
    operator_id = db.Column(db.Integer, nullable=False, index=True)
    owner_id = db.Column(db.Integer, nullable=True, index=True)
    period = db.Column(db.Date, nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    breakdown = db.Column(db.JSON, nullable=False)
    approved_by = db.Column(db.Integer, nullable=False)
    approved_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)
    paid_by = db.Column(db.Integer, nullable=True)


class SupportChat(db.Model):
    __tablename__ = 'support_chats'

    service_id = db.Column(db.Integer, db.ForeignKey('service_ownerships.id', ondelete='CASCADE'), primary_key=True)
    assigned_to = db.Column(db.Integer, nullable=True)
    open = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class SupportChatMessage(db.Model):
    __tablename__ = 'support_chat_messages'

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey('support_chats.service_id', ondelete='CASCADE'),
                           nullable=False, index=True)
    operator_id = db.Column(db.Integer, nullable=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
