# Permission-based RBAC

## Model

Authorization is expressed as permissions, not only roles. Each admin has an
effective set derived from the role default plus per-admin overrides:

    effective(admin) = role_default(role)
                       + grants(admin)      # AdminPermission.allowed = true
                       - denies(admin)      # AdminPermission.allowed = false

A superadmin always holds the full catalog, so an override can never lock the
owner out.

## Catalog

| Permission | Meaning |
|------------|---------|
| servers.read / servers.write | view / modify X-UI servers |
| clients.read / clients.write | view / modify panel clients |
| finance.read / finance.write | view / modify finance, wallet and payouts |
| finance.manage / bank.manage | approve receipts, adjust credit, manage bank cards |
| bank.reveal | reveal full bank-card data (Phase 6) |
| settings.read / settings.write | view / modify panel settings |
| backups.run / backups.restore | create/inspect backups / restore a database |
| admins.read / admins.write / admins.manage | view / edit / create-delete admins |
| secrets.manage | TLS keys and other secret material |
| telegram.read / telegram.write | Telegram routing and notifications |
| content.manage | announcements, templates, FAQ and other content |

## Role defaults

- **superadmin** (or is_superadmin): every permission.
- **admin**: the historical "user management" surface - all reads/writes except
  secrets.manage, backups.restore and admins.manage.
- **reseller**: servers.read, clients.read, clients.write, finance.read,
  finance.write, telegram.read. No settings, backups, admin management, secret
  or bank-reveal access.
- Unknown roles fall back to the reseller set.

## Enforcement

permission_required("...") in panel/routes/common.py is the server-side gate:
unauthenticated callers get 401, authenticated callers without the permission
get 403 with {"code": "forbidden"}. It is applied to the server, admin, client,
backup and permission-management endpoints; the Flask session carries no
authority of its own.

The effective set is exposed to the UI by GET /api/me/permissions so controls
can be hidden, but hiding a control is never the enforcement point.

## Admin API

- GET /api/admins/<id>/permissions - effective set, overrides and the catalog.
- PUT /api/admins/<id>/permissions with
  {"permissions": {"backups.run": true, "settings.write": false, "x": null}}
  - grant, deny or clear (null) an override. Requires admins.manage plus
  step-up MFA, and is audited as admins.permissions_update.

## Scope

Reseller data scoping (which servers, inbounds and clients a reseller may
touch) remains enforced by the ownership helpers; the permission layer decides
what kind of action is allowed, and the ownership layer decides on which rows.
Both are required, and neither is delegated to the UI.
