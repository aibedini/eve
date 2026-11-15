"""Machine API credentials for the read-only Context API v1.

The machine API (``/machine/v1``) is EVE's only non-session data surface: it is
consumed by GOD AI, so its credentials are deliberately separate from admin
sessions. A key is issued out of band (``scripts/create_machine_api_key.py`` or
:func:`panel.services.machine_api.create_machine_api_key`) and only its digest is
stored here; the plaintext ``eve_mk_<id>.<secret>`` token is shown once, at
creation time.

``scopes`` is a JSON list in a Text column (the repo-wide convention for list
columns, e.g. ``Admin.allowed_servers``), so the same schema works on SQLite in
tests and PostgreSQL in production.
"""
from datetime import datetime

from panel.extensions import db


class MachineApiKey(db.Model):
    """One machine credential with explicit, least-privilege scopes."""

    __tablename__ = 'machine_api_keys'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    # Domain-separated bearer-token digest (panel.security.hash_bearer_token).
    # The plaintext secret is never persisted.
    key_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    scopes = db.Column(db.Text, nullable=False, default='[]')
    expires_at = db.Column(db.DateTime, nullable=True)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def scope_set(self) -> set:
        """Return the parsed scope list; malformed JSON grants nothing."""
        import json

        try:
            raw = json.loads(self.scopes or '[]')
        except (TypeError, ValueError):
            return set()
        if not isinstance(raw, list):
            return set()
        return {str(scope).strip() for scope in raw if str(scope).strip()}

    def is_expired(self, now=None) -> bool:
        moment = now or datetime.utcnow()
        return bool(self.expires_at and self.expires_at <= moment)

    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def to_dict(self) -> dict:
        """Admin-safe view: never includes ``key_hash`` or the secret."""
        return {
            'id': self.id,
            'name': self.name,
            'scopes': sorted(self.scope_set()),
            'expires_at': self.expires_at.isoformat() + 'Z' if self.expires_at else None,
            'last_used_at': self.last_used_at.isoformat() + 'Z' if self.last_used_at else None,
            'revoked_at': self.revoked_at.isoformat() + 'Z' if self.revoked_at else None,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None,
        }
