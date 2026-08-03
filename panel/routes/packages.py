"""Package and price-tier API routes (extracted from app.py)."""
import json
from datetime import datetime

from flask import Blueprint, jsonify, make_response, request, session

from panel.extensions import db
from panel.models import Admin, Package, PriceTier, SystemConfig
from panel.routes.common import login_required, user_management_required
from panel.services.billing import calculate_reseller_price

bp = Blueprint('packages', __name__)


@bp.route('/api/packages', methods=['GET'])
@login_required
def get_packages():
    import json as _j
    user = db.session.get(Admin, session['admin_id'])
    purpose = (request.args.get('purpose') or '').strip().lower()
    packages = Package.query.filter_by(enabled=True).order_by(Package.display_order, Package.id).all()

    # Build creator username lookup
    creator_ids = {p.created_by for p in packages if p.created_by}
    creator_map = {}
    if creator_ids:
        creators = Admin.query.filter(Admin.id.in_(list(creator_ids))).all()
        creator_map = {a.id: a.username for a in creators}

    result = []
    for p in packages:
        if purpose == 'create' and not getattr(p, 'show_on_create', True):
            continue
        if purpose == 'renew' and not getattr(p, 'show_on_renew', True):
            continue
        scope = p.scope or 'global'
        # Resellers only see global or packages explicitly assigned to them
        if user.role == 'reseller':
            if scope == 'global':
                pass
            elif scope == 'assigned':
                try:
                    ids = _j.loads(p.assigned_reseller_ids or '[]')
                except Exception:
                    ids = []
                if user.id not in ids:
                    continue
            else:  # personal — admin-only
                continue
        # Admins / superadmins see all scopes

        p_dict = p.to_dict()
        p_dict['price'] = calculate_reseller_price(user, package=p)
        p_dict['created_by_username'] = creator_map.get(p.created_by)
        result.append(p_dict)

    resp = make_response(jsonify(result))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@bp.route('/admin/packages', methods=['POST'])
@user_management_required
def create_package():
    import json as _j
    data = request.json or {}
    reseller_ids = data.get('reseller_ids', data.get('assigned_reseller_ids', []))
    if not isinstance(reseller_ids, list):
        reseller_ids = []
    package = Package(
        name=data.get('name'),
        days=int(data.get('days', 0)),
        volume=int(data.get('volume', 0)),
        price=int(data.get('price')),
        reseller_price=int(data.get('reseller_price')) if data.get('reseller_price') is not None else None,
        enabled=data.get('enabled', True),
        scope=data.get('scope', 'global'),
        assigned_reseller_ids=_j.dumps([int(r) for r in reseller_ids]),
        created_by=session.get('admin_id'),
        show_on_sub=bool(data.get('show_on_sub', False)),
        is_trial=bool(data.get('is_trial', False)),
        show_on_create=bool(data.get('show_on_create', True)),
        show_on_renew=bool(data.get('show_on_renew', True)),
        created_at=datetime.utcnow(),
    )
    db.session.add(package)
    db.session.commit()
    return jsonify({"success": True, "id": package.id})

@bp.route('/admin/packages/<int:package_id>', methods=['PUT'])
@user_management_required
def update_package(package_id):
    package = Package.query.get_or_404(package_id)
    data = request.json or {}
    if 'name' in data:
        package.name = data['name']
    if 'days' in data:
        package.days = int(data['days'])
    if 'volume' in data:
        package.volume = int(data['volume'])
    if 'price' in data:
        package.price = int(data['price'])
    if 'reseller_price' in data:
        package.reseller_price = int(data['reseller_price']) if data['reseller_price'] is not None else None
    if 'enabled' in data:
        package.enabled = bool(data['enabled'])
    if 'scope' in data:
        package.scope = data['scope']
    if 'show_on_sub' in data:
        package.show_on_sub = bool(data['show_on_sub'])
    if 'is_trial' in data:
        package.is_trial = bool(data['is_trial'])
    if 'show_on_create' in data:
        package.show_on_create = bool(data['show_on_create'])
    if 'show_on_renew' in data:
        package.show_on_renew = bool(data['show_on_renew'])
    if 'assigned_reseller_ids' in data or 'reseller_ids' in data:
        import json as _j
        reseller_ids = data.get('assigned_reseller_ids', data.get('reseller_ids', []))
        package.assigned_reseller_ids = _j.dumps([int(r) for r in reseller_ids] if isinstance(reseller_ids, list) else [])
    package.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True})

@bp.route('/admin/packages/<int:package_id>', methods=['DELETE'])
@user_management_required
def delete_package(package_id):
    package = Package.query.get_or_404(package_id)
    db.session.delete(package)
    db.session.commit()
    return jsonify({"success": True})


# ── Reseller self-service packages ───────────────────────────────────────────
# Resellers manage their OWN packages and choose which packages (own + assigned
# + global) appear on their customers' subscription pages. Selling price floor
# is the SYSTEM base tariff; the per-use wallet cost stays the reseller's own.

def _reseller_sub_shown_ids(reseller) -> set:
    import json as _j
    try:
        return set(int(x) for x in _j.loads(reseller.sub_shown_package_ids or '[]'))
    except Exception:
        return set()


@bp.route('/api/my-packages', methods=['GET'])
@login_required
def reseller_list_packages():
    import json as _j
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return jsonify({'success': False, 'error': 'Resellers only'}), 403

    shown = _reseller_sub_shown_ids(user)
    packages = Package.query.filter_by(enabled=True).order_by(Package.display_order, Package.id).all()
    items = []
    for p in packages:
        scope = p.scope or 'global'
        is_own = (p.created_by == user.id)
        if is_own:
            visible = True
        elif scope == 'global':
            visible = True
        elif scope == 'assigned':
            try:
                ids = _j.loads(p.assigned_reseller_ids or '[]')
            except Exception:
                ids = []
            visible = user.id in ids
        else:
            visible = False
        if not visible:
            continue
        items.append({
            'id': p.id,
            'name': p.name,
            'days': int(p.days or 0),
            'volume': int(p.volume or 0),
            'price': int(p.price if is_own else calculate_reseller_price(user, package=p) or 0),
            'is_own': is_own,
            'sub_shown': bool(p.show_on_sub) if is_own else (p.id in shown),
            'show_on_create': bool(getattr(p, 'show_on_create', True)),
            'show_on_renew': bool(getattr(p, 'show_on_renew', True)),
        })
    resp = make_response(jsonify({'success': True, 'items': items}))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@bp.route('/api/my-packages', methods=['POST'])
@login_required
def reseller_create_package():
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return jsonify({'success': False, 'error': 'Resellers only'}), 403

    data = request.get_json(force=True) or {}
    name = (data.get('name') or '').strip()
    try:
        days = int(data.get('days') or 0)
        volume = int(data.get('volume') or 0)
        price = int(data.get('price') or 0)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid numbers'}), 400
    if not name or (price <= 0 and not data.get('is_trial')):
        return jsonify({'success': False, 'error': 'Name and price are required'}), 400

    # Selling-price floor = SYSTEM base tariff (no reseller discount applied).
    floor, _cpg, _cpd, _tier = _calculate_minimum_price(volume, days, reseller_id=None, server_id=None, user=None)
    if not data.get('is_trial') and days > 0 and volume > 0 and floor and price < floor:
        return jsonify({'success': False, 'error': f'Price must be at least {floor:,} (system base tariff).', 'min_price': floor}), 400

    pkg = Package(
        name=name, days=days, volume=volume, price=price,
        enabled=True, scope='personal', assigned_reseller_ids='[]',
        created_by=user.id, show_on_sub=bool(data.get('show_on_sub', False)),
        is_trial=bool(data.get('is_trial', False)),
        show_on_create=bool(data.get('show_on_create', True)),
        show_on_renew=bool(data.get('show_on_renew', True)),
        created_at=datetime.utcnow(),
    )
    db.session.add(pkg)
    db.session.commit()
    return jsonify({'success': True, 'id': pkg.id})


@bp.route('/api/my-packages/<int:package_id>', methods=['PUT'])
@login_required
def reseller_update_package(package_id):
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return jsonify({'success': False, 'error': 'Resellers only'}), 403

    pkg = db.session.get(Package, package_id)
    if not pkg:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if pkg.created_by != user.id:
        return jsonify({'success': False, 'error': 'You can only edit your own packages'}), 403

    data = request.get_json(force=True) or {}
    name = (data.get('name') or pkg.name or '').strip()
    try:
        days = int(data.get('days', pkg.days) or 0)
        volume = int(data.get('volume', pkg.volume) or 0)
        price = int(data.get('price', pkg.price) or 0)
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid numbers'}), 400
    if not name or (price <= 0 and not data.get('is_trial')):
        return jsonify({'success': False, 'error': 'Name and price are required'}), 400

    floor, _cpg, _cpd, _tier = _calculate_minimum_price(volume, days, reseller_id=None, server_id=None, user=None)
    if not data.get('is_trial') and days > 0 and volume > 0 and floor and price < floor:
        return jsonify({'success': False, 'error': f'Price must be at least {floor:,} (system base tariff).', 'min_price': floor}), 400

    pkg.name = name
    pkg.days = days
    pkg.volume = volume
    pkg.price = price
    if 'is_trial' in data:
        pkg.is_trial = bool(data['is_trial'])
    if 'show_on_sub' in data:
        pkg.show_on_sub = bool(data['show_on_sub'])
    if 'show_on_create' in data:
        pkg.show_on_create = bool(data['show_on_create'])
    if 'show_on_renew' in data:
        pkg.show_on_renew = bool(data['show_on_renew'])
    pkg.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/my-packages/<int:package_id>', methods=['DELETE'])
@login_required
def reseller_delete_package(package_id):
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return jsonify({'success': False, 'error': 'Resellers only'}), 403
    pkg = db.session.get(Package, package_id)
    if not pkg:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if pkg.created_by != user.id:
        return jsonify({'success': False, 'error': 'You can only delete your own packages'}), 403
    db.session.delete(pkg)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/my-packages/<int:package_id>/sub-toggle', methods=['POST'])
@login_required
def reseller_toggle_package_sub(package_id):
    import json as _j
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return jsonify({'success': False, 'error': 'Resellers only'}), 403

    pkg = db.session.get(Package, package_id)
    if not pkg or not pkg.enabled:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    data = request.get_json(force=True) or {}
    desired = bool(data.get('show_on_sub'))

    if pkg.created_by == user.id:
        # Own package: flip its own flag.
        pkg.show_on_sub = desired
        db.session.commit()
        return jsonify({'success': True, 'sub_shown': pkg.show_on_sub})

    # Global / assigned package: confirm the reseller may see it, then update the
    # per-reseller shown list.
    scope = pkg.scope or 'global'
    if scope == 'assigned':
        try:
            ids = _j.loads(pkg.assigned_reseller_ids or '[]')
        except Exception:
            ids = []
        if user.id not in ids:
            return jsonify({'success': False, 'error': 'Not allowed'}), 403
    elif scope != 'global':
        return jsonify({'success': False, 'error': 'Not allowed'}), 403

    shown = _reseller_sub_shown_ids(user)
    if desired:
        shown.add(pkg.id)
    else:
        shown.discard(pkg.id)
    user.sub_shown_package_ids = _j.dumps(sorted(shown))
    db.session.commit()
    return jsonify({'success': True, 'sub_shown': desired})


# ── Package scope / reseller endpoints ───────────────────────────────────────

@bp.route('/admin/packages/<int:package_id>/assign', methods=['POST'])
@user_management_required
def assign_package_to_resellers(package_id):
    """Set which resellers can see this package (scope=assigned)."""
    import json as _j
    package = db.session.get(Package, package_id)
    if not package:
        return jsonify({'success': False, 'error': 'Package not found'}), 404
    data = request.json or {}
    scope = data.get('scope', 'global')
    reseller_ids = data.get('reseller_ids', [])
    package.scope = scope
    package.assigned_reseller_ids = _j.dumps([int(r) for r in reseller_ids])
    db.session.commit()
    return jsonify({'success': True})


# ── PriceTier CRUD ────────────────────────────────────────────────────────────

def _get_applicable_price_tier(volume_gb, days, reseller_id=None, server_id=None):
    """Return the best matching PriceTier or None (falls back to SystemConfig defaults)."""
    volume_gb = float(volume_gb or 0)
    days = float(days or 0)

    # Collect candidates: reseller-specific + global, ordered by priority desc, reseller first
    tiers = (PriceTier.query
             .filter_by(is_active=True)
             .order_by(PriceTier.priority.desc(), PriceTier.reseller_id.desc())
             .all())

    for tier in tiers:
        # Skip if this tier belongs to a different reseller
        if tier.reseller_id is not None and tier.reseller_id != reseller_id:
            continue
        # Skip if reseller context given but tier belongs to no-one AND a reseller-specific one exists
        # (handled naturally by sort order — reseller-specific come first)
        if tier.server_id is not None and tier.server_id != server_id:
            continue
        # Check conditions
        if tier.min_volume_gb is not None and volume_gb < tier.min_volume_gb:
            continue
        if tier.max_volume_gb is not None and volume_gb >= tier.max_volume_gb:
            continue
        if tier.min_days is not None and days < tier.min_days:
            continue
        if tier.max_days is not None and days >= tier.max_days:
            continue
        return tier
    return None


def _calculate_minimum_price(volume_gb, days, reseller_id=None, server_id=None):
    """Returns (min_price, effective_cost_per_gb, effective_cost_per_day)."""
    volume_gb = float(volume_gb or 0)
    days = float(days or 0)

    tier = _get_applicable_price_tier(volume_gb, days, reseller_id=reseller_id, server_id=server_id)

    if tier:
        cpg = tier.cost_per_gb
        cpd = tier.cost_per_day
    else:
        cpg = cpd = None

    if cpg is None:
        try:
            cpg = int((db.session.get(SystemConfig, 'cost_per_gb') or SystemConfig()).value or 0)
        except Exception:
            cpg = 0
    if cpd is None:
        try:
            cpd = int((db.session.get(SystemConfig, 'cost_per_day') or SystemConfig()).value or 0)
        except Exception:
            cpd = 0

    if days == 0:
        try:
            cpd_unlimited = int((db.session.get(SystemConfig, 'cost_per_day_unlimited') or SystemConfig()).value or 0)
        except Exception:
            cpd_unlimited = 0
        min_price = int(volume_gb * cpg + cpd_unlimited)
    else:
        min_price = int(volume_gb * cpg + days * cpd)
    return min_price, cpg, cpd


def _tier_assigned_reseller_ids(tier):
    ids = set()
    if getattr(tier, 'reseller_id', None) is not None:
        try:
            ids.add(int(tier.reseller_id))
        except Exception:
            pass
    try:
        raw_ids = json.loads(tier.assigned_reseller_ids or '[]')
    except Exception:
        raw_ids = []
    for rid in raw_ids if isinstance(raw_ids, list) else []:
        try:
            ids.add(int(rid))
        except Exception:
            continue
    return ids


def _get_applicable_price_tier(volume_gb, days, reseller_id=None, server_id=None):
    """Return the best matching active dynamic pricing tier."""
    volume_gb = float(volume_gb or 0)
    days = float(days or 0)
    try:
        reseller_id = int(reseller_id) if reseller_id not in (None, '', 0, '0') else None
    except Exception:
        reseller_id = None

    tiers = (PriceTier.query
             .filter_by(is_active=True)
             .order_by(PriceTier.priority.desc(), PriceTier.id.desc())
             .all())

    best_global = None
    for tier in tiers:
        assigned_ids = _tier_assigned_reseller_ids(tier)
        if assigned_ids:
            if reseller_id is None or reseller_id not in assigned_ids:
                continue
        elif best_global is not None:
            continue

        if tier.server_id is not None and tier.server_id != server_id:
            continue
        if tier.min_volume_gb is not None and volume_gb < tier.min_volume_gb:
            continue
        if tier.max_volume_gb is not None and volume_gb >= tier.max_volume_gb:
            continue
        if tier.min_days is not None and days < tier.min_days:
            continue
        if tier.max_days is not None and days >= tier.max_days:
            continue
        if assigned_ids:
            return tier
        best_global = tier
    return best_global


def _calculate_minimum_price(volume_gb, days, reseller_id=None, server_id=None, user=None):
    """Returns (price, cost_per_gb, cost_per_day, matched_tier)."""
    volume_gb = float(volume_gb or 0)
    days = float(days or 0)
    tier = _get_applicable_price_tier(volume_gb, days, reseller_id=reseller_id, server_id=server_id)

    if tier:
        cpg = int(tier.cost_per_gb or 0)
        cpd = int(tier.cost_per_day or 0)
    else:
        try:
            cpg = int((db.session.get(SystemConfig, 'cost_per_gb') or SystemConfig()).value or 0)
        except Exception:
            cpg = 0
        try:
            cpd = int((db.session.get(SystemConfig, 'cost_per_day') or SystemConfig()).value or 0)
        except Exception:
            cpd = 0
        if user is not None:
            cpg = calculate_reseller_price(user, base_price=cpg, cost_type='gb')
            cpd = calculate_reseller_price(user, base_price=cpd, cost_type='day')

    if days == 0:
        if tier:
            cpd_unlimited = 0
        else:
            try:
                cpd_unlimited = int((db.session.get(SystemConfig, 'cost_per_day_unlimited') or SystemConfig()).value or 0)
            except Exception:
                cpd_unlimited = 0
            if user is not None:
                cpd_unlimited = calculate_reseller_price(user, base_price=cpd_unlimited, cost_type='day')
        min_price = int(volume_gb * cpg + cpd_unlimited)
    else:
        min_price = int(volume_gb * cpg + days * cpd)
    return min_price, cpg, cpd, tier


@bp.route('/api/packages/min-price')
@login_required
def package_min_price():
    """Calculate minimum cost for a given volume+days (for price warning in UI)."""
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False}), 401

    try:
        volume_gb = float(request.args.get('volume_gb', 0) or 0)
        days = float(request.args.get('days', 0) or 0)
        server_id = request.args.get('server_id')
        server_id = int(server_id) if server_id else None
    except Exception:
        return jsonify({'success': False, 'error': 'Invalid params'}), 400

    # For superadmin, also accept explicit reseller_id; otherwise use caller's ID
    is_super = user.role == 'superadmin' or user.is_superadmin
    if is_super:
        try:
            reseller_id = int(request.args.get('reseller_id', 0) or 0) or None
        except Exception:
            reseller_id = None
    else:
        reseller_id = user.id

    min_price, cpg, cpd, tier = _calculate_minimum_price(
        volume_gb, days, reseller_id=reseller_id, server_id=server_id, user=user
    )
    return jsonify({
        'success': True,
        'min_price': min_price,
        'cost_per_gb': cpg,
        'cost_per_day': cpd,
        'tier_id': tier.id if tier else None,
        'tier_name': tier.name if tier else None,
    })


@bp.route('/api/price-tiers', methods=['GET'])
@user_management_required
def list_price_tiers():
    reseller_id = request.args.get('reseller_id')
    q = PriceTier.query
    if reseller_id:
        try:
            rid = int(reseller_id)
            q = q.filter(
                db.or_(
                    PriceTier.reseller_id.is_(None),
                    PriceTier.reseller_id == rid,
                    PriceTier.assigned_reseller_ids.like(f'%{rid}%')
                )
            )
        except Exception:
            pass
    tiers = q.order_by(PriceTier.priority.desc(), PriceTier.id).all()
    return jsonify({'success': True, 'tiers': [t.to_dict() for t in tiers]})


@bp.route('/api/price-tiers', methods=['POST'])
@user_management_required
def create_price_tier():
    data = request.json or {}
    admin_id = session.get('admin_id')
    try:
        assigned_ids = data.get('assigned_reseller_ids', data.get('reseller_ids', []))
        if data.get('reseller_id') and not assigned_ids:
            assigned_ids = [data.get('reseller_id')]
        if not isinstance(assigned_ids, list):
            assigned_ids = []
        assigned_ids = [int(r) for r in assigned_ids if str(r or '').strip()]
        tier = PriceTier(
            name=str(data.get('name', '')).strip() or 'Tier',
            min_volume_gb=float(data['min_volume_gb']) if data.get('min_volume_gb') not in (None, '') else None,
            max_volume_gb=float(data['max_volume_gb']) if data.get('max_volume_gb') not in (None, '') else None,
            min_days=int(data['min_days']) if data.get('min_days') not in (None, '') else None,
            max_days=int(data['max_days']) if data.get('max_days') not in (None, '') else None,
            cost_per_gb=int(data['cost_per_gb']) if data.get('cost_per_gb') not in (None, '') else None,
            cost_per_day=int(data['cost_per_day']) if data.get('cost_per_day') not in (None, '') else None,
            reseller_id=assigned_ids[0] if len(assigned_ids) == 1 else None,
            assigned_reseller_ids=json.dumps(assigned_ids),
            server_id=int(data['server_id']) if data.get('server_id') else None,
            priority=int(data.get('priority') or 0),
            is_active=bool(data.get('is_active', True)),
            created_by=admin_id,
        )
        db.session.add(tier)
        db.session.commit()
        return jsonify({'success': True, 'id': tier.id, 'tier': tier.to_dict()})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400


@bp.route('/api/price-tiers/<int:tier_id>', methods=['PUT'])
@user_management_required
def update_price_tier(tier_id):
    tier = db.session.get(PriceTier, tier_id)
    if not tier:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    data = request.json or {}
    try:
        if 'name' in data:
            tier.name = str(data['name']).strip() or tier.name
        for _field in ('min_volume_gb', 'max_volume_gb'):
            if _field in data:
                setattr(tier, _field, float(data[_field]) if data[_field] not in (None, '') else None)
        for _field in ('min_days', 'max_days', 'cost_per_gb', 'cost_per_day', 'priority'):
            if _field in data:
                setattr(tier, _field, int(data[_field]) if data[_field] not in (None, '') else None)
        if 'reseller_id' in data:
            tier.reseller_id = int(data['reseller_id']) if data['reseller_id'] else None
        if 'assigned_reseller_ids' in data or 'reseller_ids' in data:
            assigned_ids = data.get('assigned_reseller_ids', data.get('reseller_ids', []))
            if not isinstance(assigned_ids, list):
                assigned_ids = []
            assigned_ids = [int(r) for r in assigned_ids if str(r or '').strip()]
            tier.assigned_reseller_ids = json.dumps(assigned_ids)
            tier.reseller_id = assigned_ids[0] if len(assigned_ids) == 1 else None
        if 'server_id' in data:
            tier.server_id = int(data['server_id']) if data['server_id'] else None
        if 'is_active' in data:
            tier.is_active = bool(data['is_active'])
        db.session.commit()
        return jsonify({'success': True, 'tier': tier.to_dict()})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400


@bp.route('/api/price-tiers/<int:tier_id>', methods=['DELETE'])
@user_management_required
def delete_price_tier(tier_id):
    tier = db.session.get(PriceTier, tier_id)
    if not tier:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    db.session.delete(tier)
    db.session.commit()
    return jsonify({'success': True})
