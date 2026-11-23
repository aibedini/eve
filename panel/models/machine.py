"""Machine integration persistence."""
from panel.extensions import db
from panel.security import EncryptedText


class MachineAPIKey(db.Model):
    __tablename__ = 'machine_api_keys'
    id = db.Column(db.String(32), primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    key_hash = db.Column(db.String(64), nullable=False)
    scopes = db.Column(db.JSON, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    last_used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)
    created_by = db.Column(db.Integer, nullable=False)
    rate_window = db.Column(db.Integer, nullable=False, default=0)
    rate_count = db.Column(db.Integer, nullable=False, default=0)


class MachineProposal(db.Model):
    __tablename__ = 'machine_proposals'
    id = db.Column(db.String(36), primary_key=True)
    key_id = db.Column(db.String(32), nullable=False)
    payload = db.Column(EncryptedText('machine-proposal'), nullable=False)
    status = db.Column(db.String(12), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    decided_at = db.Column(db.DateTime)
    decided_by = db.Column(db.Integer)


class MachinePhoneLookup(db.Model):
    __tablename__ = 'machine_phone_lookups'
    customer_id = db.Column(db.Integer, db.ForeignKey('customer_accounts.id', ondelete='CASCADE'), primary_key=True)
    phone_hash = db.Column(db.String(64), nullable=False, index=True)


class MachineEvent(db.Model):
    __tablename__ = 'machine_events'
    id = db.Column(db.String(40), primary_key=True)
    body = db.Column(db.Text, nullable=False)
    next_attempt_at = db.Column(db.DateTime, nullable=False, index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    delivered_at = db.Column(db.DateTime, index=True)
