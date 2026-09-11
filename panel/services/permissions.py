"""Permission-based RBAC: catalog, role defaults and per-admin overrides.

Roles remain the coarse default, but every authorization decision is expressed
as a permission. A row in admin_permissions grants or denies one permission for
one admin and takes precedence over the role default, which makes least
privilege enforceable per account without inventing new roles.
"""
from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import AdminPermission

PERMISSIONS = frozenset({
    'servers.read', 'servers.write',
    'clients.read', 'clients.write',
    'finance.read', 'finance.write', 'finance.manage', 'bank.manage', 'bank.reveal',
    'settings.read', 'settings.write',
    'backups.run', 'backups.restore',
    'admins.read', 'admins.write', 'admins.manage',
    'secrets.manage',
    'telegram.read', 'telegram.write',
    'content.manage',
})

# Role defaults. admin mirrors the historical "user management" surface
# (everything except the superadmin-only secrets / backup-restore / admin-manage
# actions); reseller is limited to its own servers, clients and finance.
ROLE_DEFAULTS = {
    'superadmin': PERMISSIONS,
    'admin': frozenset({
        'servers.read', 'servers.write',
        'clients.read', 'clients.write',
        'finance.read', 'finance.write', 'finance.manage', 'bank.manage', 'bank.reveal',
        'settings.read', 'settings.write',
        'admins.read', 'admins.write',
        'telegram.read', 'telegram.write',
        'content.manage',
    }),
    'reseller': frozenset({
        'servers.read', 'clients.read', 'clients.write',
        'finance.read', 'finance.write',
        'telegram.read',
    }),
}


def normalize_role(admin) -> str:
    if admin is None:
        return ''
    if admin.role == 'superadmin' or admin.is_superadmin:
        return 'superadmin'
    role = str(admin.role or 'reseller')
    return role if role in ROLE_DEFAULTS else 'reseller'


def is_superadmin(admin) -> bool:
    return normalize_role(admin) == 'superadmin'


def _overrides(admin_id) -> dict:
    rows = AdminPermission.query.filter_by(admin_id=int(admin_id)).all()
    return {row.permission: bool(row.allowed) for row in rows}


def permissions_for(admin) -> frozenset:
    """Return the effective permission set for an admin (overrides included)."""
    if admin is None:
        return frozenset()
    role = normalize_role(admin)
    if role == 'superadmin':
        # A superadmin cannot lock itself out through an override.
        return PERMISSIONS
    effective = set(ROLE_DEFAULTS.get(role, ROLE_DEFAULTS['reseller']))
    admin_id = getattr(admin, 'id', None)
    if admin_id is not None:
        # Unsaved transient objects (tests, dry runs) never touch the database.
        for permission, allowed in _overrides(admin_id).items():
            if permission not in PERMISSIONS:
                continue
            if allowed:
                effective.add(permission)
            else:
                effective.discard(permission)
    return frozenset(effective)


def has_permission(admin, permission: str) -> bool:
    if not permission or permission not in PERMISSIONS:
        return False
    if admin is None or not bool(getattr(admin, 'enabled', False)):
        return False
    return permission in permissions_for(admin)


def set_permission(admin_id: int, permission: str, allowed: bool) -> None:
    """Upsert one override. The caller owns the transaction."""
    if permission not in PERMISSIONS:
        raise ValueError(f'Unknown permission: {permission}')
    row = AdminPermission.query.filter_by(
        admin_id=int(admin_id), permission=permission,
    ).first()
    if row is None:
        row = AdminPermission(admin_id=int(admin_id), permission=permission, allowed=bool(allowed))
        db.session.add(row)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            row = AdminPermission.query.filter_by(
                admin_id=int(admin_id), permission=permission,
            ).one()
            row.allowed = bool(allowed)
    else:
        row.allowed = bool(allowed)


def clear_permission(admin_id: int, permission: str) -> None:
    AdminPermission.query.filter_by(
        admin_id=int(admin_id), permission=permission,
    ).delete()


def overrides_for(admin_id) -> dict:
    return _overrides(admin_id)
