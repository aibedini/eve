"""Proposals submitted by GOD AI over the machine API (XR-GA-019).

A proposal is GOD AI's *suggestion* -- a reminder text, a suspected duplicate
receipt, a Telegram reply draft -- never an action. EVE owns the decision: an
operator reviews the pending row in the panel, and accepting or rejecting it only
records that decision here. Nothing in this module (or in
``panel.services.proposals``) sends an SMS, renews a service or mutates a panel
client.

``proposal_id`` is client-generated and idempotent: GOD AI may retry a POST after
a timeout, so the same id with the same type/payload is the same proposal.
"""
from datetime import datetime

from panel.extensions import db

#: The only proposal types the machine API accepts (contract section 6).
PROPOSAL_TYPE_REMINDER_TEXT = 'reminder_text'
PROPOSAL_TYPE_DUPLICATE_RECEIPT_FLAG = 'duplicate_receipt_flag'
PROPOSAL_TYPE_TELEGRAM_REPLY_DRAFT = 'telegram_reply_draft'
PROPOSAL_TYPES = (
    PROPOSAL_TYPE_REMINDER_TEXT,
    PROPOSAL_TYPE_DUPLICATE_RECEIPT_FLAG,
    PROPOSAL_TYPE_TELEGRAM_REPLY_DRAFT,
)

#: Lifecycle of a proposal. Only ``pending`` rows are reviewable.
PROPOSAL_STATUS_PENDING = 'pending'
PROPOSAL_STATUS_ACCEPTED = 'accepted'
PROPOSAL_STATUS_REJECTED = 'rejected'
PROPOSAL_STATUS_EXPIRED = 'expired'
PROPOSAL_STATUSES = (
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_ACCEPTED,
    PROPOSAL_STATUS_REJECTED,
    PROPOSAL_STATUS_EXPIRED,
)

#: Machine-readable reasons recorded when a decision cannot be applied as-is.
PROPOSAL_EXPIRED_SERVICE_MISSING = 'service_missing'
PROPOSAL_EXPIRED_GENERATION_STALE = 'generation_stale'


class Proposal(db.Model):
    """One GOD AI proposal and the operator decision EVE recorded for it."""

    __tablename__ = 'proposals'
    __table_args__ = (
        # The review queue is always "pending, newest first"; the machine listing
        # filters the same way, so one composite index serves both.
        db.Index('ix_proposals_status_created', 'status', 'created_at'),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Client-generated idempotency key. Unique, so a retried POST can never create
    # a second proposal (the unique index also makes the replay race safe).
    proposal_id = db.Column(db.String(128), nullable=False, unique=True, index=True)
    type = db.Column(db.String(32), nullable=False)
    customer_id = db.Column(db.Integer, nullable=True)
    # Canonical ``eve:<serverId>:<clientUuid>``; nullable because some proposals
    # (for example a generic reminder draft) are not bound to a service yet.
    service_key = db.Column(db.String(255), nullable=True)
    # The service_lifecycle_states.generation the proposal was computed from. An
    # accept revalidates it: a stale proposal is expired, never applied.
    generation = db.Column(db.BigInteger, nullable=True)
    # Compact, validated JSON text (max 8 KB, enforced by the service layer).
    payload = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False,
                       default=PROPOSAL_STATUS_PENDING,
                       server_default=PROPOSAL_STATUS_PENDING)
    reason = db.Column(db.String(500), nullable=True)
    # machine_api_keys.id of the proposing key (no FK: keys are never deleted, and
    # SQLite tests drop tables in dependency order).
    created_by_key_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    decided_by = db.Column(db.String(100), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
