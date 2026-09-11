"""Server-side admin session registry with role-based timeouts.

The Flask cookie only carries an opaque token; every authorization decision
looks up the registry row so idle/absolute timeouts, MFA state, step-up
freshness and revocation are enforced server-side. SuperAdmin sessions expire
quickly (default 45 min idle / 12 h absolute) instead of the 7-day cookie.
"""
import os
import secrets
from datetime import datetime, timedelta

from panel.extensions import db
from panel.models import Admin, AdminSession
from panel.security import hash_bearer_token

SESSION_TOKEN_KEY = 'session_token'

# role -> (idle_minutes, absolute_hours)
_POLICY = {
    'superadmin': (45, 12),
    'admin': (240, 72),
    'reseller': (720, 168),
}


def _env_policy(role: str):
    upper = role.upper()
    idle = os.environ.get(f'EVE_SESSION_IDLE_MINUTES_{upper}')
    absolute = os.environ.get(f'EVE_SESSION_ABSOLUTE_HOURS_{upper}')
    fallback = _POLICY.get(role, _POLICY['reseller'])
    try:
        idle_minutes = int(idle) if idle else fallback[0]
    except (TypeError, ValueError):
        idle_minutes = fallback[0]
    try:
        absolute_hours = int(absolute) if absolute else fallback[1]
    except (TypeError, ValueError):
        absolute_hours = fallback[1]
    return max(1, idle_minutes), max(1, absolute_hours)


def role_of(admin) -> str:
    return 'superadmin' if (admin.role == 'superadmin' or admin.is_superadmin) else (admin.role or 'reseller')


def policy_for(admin) -> tuple[int, int]:
    return _env_policy(role_of(admin))


def step_up_ttl_seconds() -> int:
    default = 600
    raw = os.environ.get('EVE_STEP_UP_TTL_SECONDS')
    try:
        return max(30, int(raw)) if raw else default
    except (TypeError, ValueError):
        return default


def _token_hash(token: str) -> str:
    return hash_bearer_token(token, 'admin-session')


def create_session(admin, *, ip: str | None = None, user_agent: str | None = None,
                   mfa_verified: bool = False, commit: bool = False):
    """Create a registry row and return ``(token, row)``. Caller commits."""
    token = secrets.token_urlsafe(32)
    _, absolute_hours = policy_for(admin)
    now = datetime.utcnow()
    row = AdminSession(
        admin_id=admin.id,
        token_hash=_token_hash(token),
        ip=(ip or '')[:64] or None,
        user_agent=(user_agent or '')[:255] or None,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=absolute_hours),
        mfa_verified=bool(mfa_verified),
    )
    db.session.add(row)
    db.session.flush()
    if commit:
        db.session.commit()
    return token, row


def resolve_session(token: str | None, *, touch: bool = True):
    """Return the live registry row for ``token`` or None when invalid/expired."""
    if not token:
        return None
    row = AdminSession.query.filter_by(token_hash=_token_hash(token)).first()
    if row is None or row.revoked_at is not None:
        return None
    now = datetime.utcnow()
    if row.expires_at and row.expires_at <= now:
        return None
    admin = db.session.get(Admin, row.admin_id)
    if admin is None or not admin.enabled:
        return None
    idle_minutes, _ = policy_for(admin)
    if row.last_seen_at and (now - row.last_seen_at) > timedelta(minutes=idle_minutes):
        return None
    if touch and (row.last_seen_at is None or (now - row.last_seen_at).total_seconds() >= 60):
        # Throttle the heartbeat so a busy request loop does not write on every hit.
        row.last_seen_at = now
    return row


def revoke_session(token: str | None) -> bool:
    if not token:
        return False
    row = AdminSession.query.filter_by(token_hash=_token_hash(token)).first()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.utcnow()
    return True


def revoke_other_sessions(admin_id: int, keep_token: str | None) -> int:
    keep_hash = _token_hash(keep_token) if keep_token else None
    rows = AdminSession.query.filter_by(admin_id=int(admin_id)).filter(
        AdminSession.revoked_at.is_(None),
    ).all()
    now = datetime.utcnow()
    revoked = 0
    for row in rows:
        if keep_hash and row.token_hash == keep_hash:
            continue
        row.revoked_at = now
        revoked += 1
    return revoked


def list_sessions(admin_id: int, current_token: str | None = None):
    rows = AdminSession.query.filter_by(admin_id=int(admin_id)).order_by(
        AdminSession.created_at.desc(),
    ).limit(50).all()
    current_hash = _token_hash(current_token) if current_token else None
    return [(row, bool(current_hash and row.token_hash == current_hash)) for row in rows]


def mark_step_up(row, commit: bool = False) -> None:
    if row is not None:
        row.step_up_at = datetime.utcnow()
        if commit:
            db.session.commit()


def step_up_fresh(row) -> bool:
    if row is None or row.step_up_at is None:
        return False
    return (datetime.utcnow() - row.step_up_at).total_seconds() <= step_up_ttl_seconds()
