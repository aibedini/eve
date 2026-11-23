"""Package and price-tier API routes (extracted from app.py)."""
import json
import hashlib
import time
import math
from datetime import datetime

from flask import Blueprint, jsonify, make_response, request, session
from sqlalchemy.exc import IntegrityError

from panel.extensions import db
from panel.models import Admin, Package, PriceTier, ResellerContract, ResellerOffer, Server, SystemConfig, WalletLedger
from panel.routes.common import login_required, permission_required
from panel.services.permissions import has_permission
from panel.services.billing import (
    _get_applicable_price_tier, _tier_assigned_reseller_ids, calculate_reseller_price,
    calculate_tariff_price, reseller_sub_package_ids,
    catalog_offer, creation_reseller_id, customer_package_price, customer_package_visible, package_allowed_in_panel,
    package_in_catalog, package_quote_context, pricing_quote, pricing_quote_revision,
    renewal_pricing_owner,
)

bp = Blueprint('packages', __name__)


def _renewal_reseller_id(user, server_id):
    owner = renewal_pricing_owner(server_id, request.args.get('email'),
                                  request.args.get('inbound_id', type=int)) if server_id else None
    if user.role == 'reseller':
        if owner is not None and owner != user.id:
            raise PermissionError('Account is outside your reseller scope')
        return user.id
    return owner


def _optional_cost(value):
    if value in (None, ''):
        return None
    if isinstance(value, bool):
        raise ValueError('Cost must be a non-negative integer')
    parsed = int(str(value))
    if parsed < 0 or parsed > 2147483647:
        raise ValueError('Cost must be between 0 and 2147483647')
    return parsed


def _package_cost_settings(data, package=None):
    """Validate the administrator's wallet-cost policy before mutating a package."""
    mode = data.get('reseller_cost_mode', getattr(package, 'reseller_cost_mode', None) or 'auto')
    if mode not in ('auto', 'fixed', 'percent', 'tariff'):
        raise ValueError('Invalid reseller cost mode')
    raw_fixed = data.get('reseller_price', getattr(package, 'reseller_price', None))
    fixed = _optional_cost(raw_fixed)
    raw_percent = data.get('reseller_discount_percent', getattr(package, 'reseller_discount_percent', None))
    percent = float(raw_percent) if raw_percent not in (None, '') else None
    rounding = _optional_cost(data.get('reseller_rounding', getattr(package, 'reseller_rounding', 0))) or 0
    if percent is not None and (not math.isfinite(percent) or not 0 <= percent <= 100):
        raise ValueError('Discount must be between 0 and 100')
    if rounding < 0 or rounding > 1000000000:
        raise ValueError('Invalid rounding step')
    if mode == 'fixed' and fixed is None:
        raise ValueError('Fixed reseller cost is required')
    if mode == 'percent' and percent is None:
        raise ValueError('Discount percentage is required')
    return dict(reseller_cost_mode=mode, reseller_price=fixed,
                reseller_discount_percent=percent, reseller_rounding=rounding)


def _operational_roles(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'create', 'renew'}:
        raise ValueError('Invalid operation permissions')
    result = {}
    for action in ('create', 'renew'):
        roles = value.get(action, [])
        if not isinstance(roles, list) or any(role not in ('reseller', 'admin', 'superadmin') for role in roles):
            raise ValueError('Invalid operation role')
        result[action] = sorted(set(roles))
    return result


@bp.route('/api/packages', methods=['GET'])
@login_required
def get_packages():
    started = time.perf_counter()
    import json as _j
    user = db.session.get(Admin, session['admin_id'])
    purpose = (request.args.get('purpose') or '').strip().lower()
    server_id = request.args.get('server_id', type=int)
    reseller_id = user.id if user.role == 'reseller' else None
    if purpose == 'create':
        try:
            reseller_id = creation_reseller_id(user, request.args.get('reseller_id'))
        except PermissionError as exc:
            return jsonify(success=False, error=str(exc)), 403
        except (ValueError, TypeError) as exc:
            return jsonify(success=False, error=str(exc)), 400
    if purpose == 'renew':
        try:
            reseller_id = _renewal_reseller_id(user, server_id)
        except PermissionError as exc:
            return jsonify(success=False, error=str(exc)), 403
    payer = db.session.get(Admin, reseller_id) if reseller_id else None
    query = Package.query
    if purpose or request.args.get('include_archived') != '1' or not has_permission(user, 'content.manage'):
        query = query.filter_by(enabled=True)
    packages = query.order_by(Package.display_order, Package.id).all()

    # Build creator username lookup
    creator_ids = {p.created_by for p in packages if p.created_by}
    creator_map = {}
    if creator_ids:
        creators = Admin.query.filter(Admin.id.in_(list(creator_ids))).all()
        creator_map = {a.id: a.username for a in creators}

    quote_context = package_quote_context(payer) if payer and packages else None
    result = []
    for p in packages:
        if purpose in ('create', 'renew') and not package_allowed_in_panel(p, user, reseller_id, purpose):
            continue
        scope = p.scope or 'global'
        # Resellers only see global or packages explicitly assigned to them
        if not purpose and user.role == 'reseller':
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
        if purpose in ('create', 'renew') or user.role == 'reseller':
            try:
                quote = pricing_quote(payer, package=p, server_id=server_id, package_context=quote_context)
            except ValueError:
                continue
            p_dict['price'] = quote['amount']
            p_dict['quota_mib'] = quote['quota_mib']
            p_dict['quote_revision'] = pricing_quote_revision(quote)
            if purpose in ('create', 'renew'):
                p_dict['selling_price'] = quote['retail']
        p_dict['created_by_username'] = creator_map.get(p.created_by)
        result.append(p_dict)

    # The representation is private to this authenticated pricing scope. Its digest is
    # both an ETag and a revision: package edits and reseller-specific price changes
    # naturally produce a new value without sharing one reseller's prices with another.
    canonical = json.dumps(result, sort_keys=True, separators=(',', ':'), default=str)
    revision = hashlib.sha256(
        f"{user.id}:{user.role}:{purpose}:{canonical}".encode('utf-8')).hexdigest()[:20]
    if request.if_none_match.contains(revision):
        resp = make_response('', 304)
    else:
        for item in result:
            item['revision'] = revision
        resp = make_response(jsonify(result))
    resp.set_etag(revision)
    resp.headers['Cache-Control'] = 'private, max-age=60, stale-while-revalidate=120'
    resp.headers['Vary'] = 'Cookie'
    resp.headers['X-Package-Revision'] = revision
    resp.headers['Server-Timing'] = 'packages;dur=%.1f' % (
        (time.perf_counter() - started) * 1000.0)
    return resp

@bp.route('/admin/packages', methods=['POST'])
@permission_required('content.manage')
def create_package():
    import json as _j
    data = request.json or {}
    try:
        cost_settings = _package_cost_settings(data)
        panel_roles = _operational_roles(data.get('panel_roles'))
    except (ValueError, TypeError, OverflowError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    reseller_ids = data.get('reseller_ids', data.get('assigned_reseller_ids', []))
    if not isinstance(reseller_ids, list):
        reseller_ids = []
    package = Package(
        name=data.get('name'),
        days=int(data.get('days', 0)),
        volume=int(data.get('volume', 0)),
        price=int(data.get('price')),
        **cost_settings,
        panel_roles=panel_roles,
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
@permission_required('content.manage')
def update_package(package_id):
    package = Package.query.get_or_404(package_id)
    data = request.json or {}
    try:
        cost_settings = _package_cost_settings(data, package)
        panel_roles = _operational_roles(data.get('panel_roles', package.panel_roles))
    except (ValueError, TypeError, OverflowError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    if 'name' in data:
        package.name = data['name']
    if 'days' in data:
        package.days = int(data['days'])
    if 'volume' in data:
        package.volume = int(data['volume'])
    if 'price' in data:
        package.price = int(data['price'])
    for field, value in cost_settings.items():
        setattr(package, field, value)
    package.panel_roles = panel_roles
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
@permission_required('content.manage')
def delete_package(package_id):
    package = Package.query.get_or_404(package_id)
    package.enabled = False
    package.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True})


# ── Reseller self-service packages ───────────────────────────────────────────
# Resellers manage their OWN packages and choose which packages (own + assigned
# + global) appear on their customers' subscription pages. Their selling price
# is independent of the per-use wallet charge.

@bp.route('/api/my-packages', methods=['GET'])
@login_required
def reseller_list_packages():
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return jsonify({'success': False, 'error': 'Resellers only'}), 403

    shown = reseller_sub_package_ids(user)
    packages = Package.query.filter_by(enabled=True).order_by(Package.display_order, Package.id).all()
    items = []
    for p in packages:
        is_own = (p.created_by == user.id)
        if not package_in_catalog(p, user.id):
            continue
        offer = catalog_offer(p, user.id)
        try:
            quote = pricing_quote(user, package=p, sale_amount=customer_package_price(p, user.id, offer))
        except ValueError:
            quote = None
        items.append({
            'id': p.id,
            'name': p.name,
            'days': int(p.days or 0),
            'volume': int(p.volume or 0),
            'price': customer_package_price(p, user.id, offer),
            'base_price': p.price,
            'selling_price_override': offer.selling_price if offer else None,
            'reseller_cost': quote['amount'] if quote else None,
            'quota_mib': quote['quota_mib'] if quote else 0,
            'is_own': is_own,
            'sub_shown': customer_package_visible(p, user.id, offer=offer, selection=shown),
            'show_on_create': bool(getattr(p, 'show_on_create', True)),
            'show_on_renew': bool(getattr(p, 'show_on_renew', True)),
        })
    contract = db.session.get(ResellerContract, user.id)
    resp = make_response(jsonify(success=True, items=items,
                                 contract_mode=contract.policy.get('mode') if contract else 'legacy',
                                 quota_mib=contract.quota_mib if contract else 0))
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
    pkg.enabled = False
    pkg.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/my-packages/<int:package_id>/sub-toggle', methods=['POST'])
@login_required
def reseller_toggle_package_sub(package_id):
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return jsonify({'success': False, 'error': 'Resellers only'}), 403

    pkg = db.session.get(Package, package_id)
    if not pkg or not pkg.enabled:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    data = request.get_json(force=True) or {}
    desired = bool(data.get('show_on_sub'))
    if not package_in_catalog(pkg, user.id):
        return jsonify({'success': False, 'error': 'Not allowed'}), 403
    response = _save_offer(user.id, pkg.id, {'published': desired}, admin=False)
    if isinstance(response, tuple):
        return response
    return jsonify(success=True, sub_shown=desired)


@bp.route('/api/my-packages/<int:package_id>/offer', methods=['PUT'])
@login_required
def reseller_save_offer(package_id):
    user = db.session.get(Admin, session['admin_id'])
    package = db.session.get(Package, package_id)
    if not user or user.role != 'reseller' or not package or not package.enabled or not package_in_catalog(package, user.id):
        return jsonify(success=False, error='Package is unavailable'), 404
    return _save_offer(user.id, package_id, request.json or {}, admin=False)


def _save_offer(reseller_id, package_id, data, *, admin):
    if not isinstance(data, dict):
        return jsonify(success=False, error='Expected an object'), 400
    offer = db.session.get(ResellerOffer, (reseller_id, package_id))
    if offer is None:
        offer = ResellerOffer(reseller_id=reseller_id, package_id=package_id)
        db.session.add(offer)
    try:
        if 'selling_price' in data:
            offer.selling_price = _optional_cost(data['selling_price'])
        if 'published' in data:
            if data['published'] is not None and not isinstance(data['published'], bool):
                raise ValueError('Invalid publication setting')
            offer.published = data['published']
        if admin and 'wholesale_price' in data:
            offer.wholesale_price = _optional_cost(data['wholesale_price'])
        db.session.commit()
    except (ValueError, TypeError, IntegrityError) as exc:
        db.session.rollback()
        message = 'Offer changed concurrently. Reload and retry.' if isinstance(exc, IntegrityError) else str(exc)
        return jsonify(success=False, error=message), 400
    return jsonify(success=True)


@bp.route('/api/reseller-pricing/<int:reseller_id>', methods=['GET', 'PUT'])
@permission_required('content.manage')
def reseller_pricing_contract(reseller_id):
    reseller = db.session.get(Admin, reseller_id)
    if not reseller or reseller.role != 'reseller':
        return jsonify(success=False, error='Reseller not found'), 404
    contract = db.session.get(ResellerContract, reseller_id)
    if request.method == 'GET':
        selection = reseller_sub_package_ids(reseller)
        offers = {item.package_id: item for item in ResellerOffer.query.filter_by(reseller_id=reseller_id)}
        packages = []
        for p in Package.query.filter_by(enabled=True).order_by(Package.display_order, Package.id):
            if not package_in_catalog(p, reseller_id):
                continue
            offer = offers.get(p.id)
            packages.append(dict(id=p.id, name=p.name, price=p.price,
                                 selling_price=offer.selling_price if offer else None,
                                 wholesale_price=offer.wholesale_price if offer else None,
                                 published=offer.published if offer else None,
                                 visible=customer_package_visible(p, reseller_id, offer=offer, selection=selection)))
        return jsonify(success=True, policy=contract.policy if contract else {'mode': 'legacy'},
                       version=contract.version if contract else 0,
                       quota_mib=contract.quota_mib if contract else 0, packages=packages)
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify(success=False, error='Expected a contract object'), 400
    mode = data.get('mode', 'legacy')
    if mode not in ('legacy', 'fixed', 'tier', 'percent', 'quota'):
        return jsonify(success=False, error='Invalid contract mode'), 400
    policy = {'mode': mode}
    try:
        submitted_version = int(data.get('version', 0))
        for field in ('retail_gb', 'retail_day'):
            value = _optional_cost(data.get(field))
            if value is not None:
                policy[field] = value
        if mode == 'fixed':
            policy['fixed'] = _optional_cost(data.get('fixed'))
            if policy['fixed'] is None:
                raise ValueError('Enter the fixed wholesale amount')
        if mode == 'percent':
            percent = float(data.get('percent'))
            basis = data.get('basis', 'reference')
            if not math.isfinite(percent) or not 0 <= percent <= 100 or basis not in ('reference', 'net_sale'):
                raise ValueError('Enter a valid percentage and basis')
            policy.update(percent=percent, basis=basis, rounding=_optional_cost(data.get('rounding')) or 0)
    except (ValueError, TypeError, OverflowError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    if contract and ResellerContract.query.filter_by(reseller_id=reseller_id, version=submitted_version).update(
            {ResellerContract.policy: policy, ResellerContract.version: ResellerContract.version + 1},
            synchronize_session=False) != 1:
        db.session.rollback()
        return jsonify(success=False, error='Contract changed. Reload it before saving.'), 409
    if not contract:
        db.session.add(ResellerContract(reseller_id=reseller_id, policy=policy))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(success=False, error='Contract changed. Reload it before saving.'), 409
    return jsonify(success=True)


@bp.route('/api/reseller-pricing/<int:reseller_id>/offer/<int:package_id>', methods=['PUT'])
@permission_required('content.manage')
def admin_save_offer(reseller_id, package_id):
    reseller = db.session.get(Admin, reseller_id)
    package = db.session.get(Package, package_id)
    if not reseller or reseller.role != 'reseller' or not package or not package_in_catalog(package, reseller_id):
        return jsonify(success=False, error='Package or reseller not found'), 404
    return _save_offer(reseller_id, package_id, request.json or {}, admin=True)


@bp.route('/api/reseller-pricing/<int:reseller_id>/quota', methods=['POST'])
@permission_required('content.manage')
def grant_reseller_quota(reseller_id):
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify(success=False, error='Expected a quota grant object'), 400
    try:
        gb = _optional_cost(data.get('gb'))
        key = str(data.get('key') or '').strip()
        if not gb or gb > 2097151 or not key or len(key) > 60:
            raise ValueError('Enter a positive quota and a request key')
    except (ValueError, TypeError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    units = gb * 1024
    ledger_key = f'reseller-quota:{reseller_id}:{key}'
    entry = WalletLedger.query.filter_by(idempotency_key=ledger_key).first()
    if entry:
        if entry.amount != units:
            return jsonify(success=False, error='Request key already used with a different quota'), 409
        return jsonify(success=True)
    db.session.add(WalletLedger(account_type='reseller_quota', owner_id=reseller_id,
                                amount=units, type='quota_grant', idempotency_key=ledger_key,
                                description=f'Granted by admin #{session["admin_id"]}'))
    try:
        db.session.flush()
        updated = ResellerContract.query.filter(
            ResellerContract.reseller_id == reseller_id,
            ResellerContract.quota_mib <= 9007199254740991 - units,
        ).update({ResellerContract.quota_mib: ResellerContract.quota_mib + units}, synchronize_session=False)
        if updated != 1:
            db.session.rollback()
            return jsonify(success=False, error='Set up the contract first or reduce the grant'), 400
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(success=False, error='Grant already submitted. Reload the quota before retrying.'), 409
    return jsonify(success=True)


@bp.route('/api/reseller-pricing/<int:reseller_id>/preview', methods=['GET'])
@permission_required('content.manage')
def preview_reseller_pricing(reseller_id):
    reseller = db.session.get(Admin, reseller_id)
    if not reseller or reseller.role != 'reseller':
        return jsonify(success=False, error='Reseller not found'), 404
    package_id = request.args.get('package_id', type=int)
    package = db.session.get(Package, package_id) if package_id else None
    if package_id and (not package or not package_in_catalog(package, reseller_id)):
        return jsonify(success=False, error='Package not found for this reseller'), 404
    try:
        volume = float(request.args.get('volume_gb', 0))
        days = float(request.args.get('days', 0))
        if not all(math.isfinite(n) and 0 <= n <= 1000000 for n in (volume, days)):
            raise ValueError('Enter a finite, non-negative volume and duration')
        quote = pricing_quote(reseller, package=package, volume_gb=volume, days=days,
                              server_id=request.args.get('server_id', type=int),
                              sale_amount=_optional_cost(request.args.get('sale_amount')))
    except (ValueError, TypeError, OverflowError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    if package:
        quote['published'] = customer_package_visible(
            package, reseller_id, offer=catalog_offer(package, reseller_id),
            selection=reseller_sub_package_ids(reseller))
        from types import SimpleNamespace
        quote['panel_access'] = {
            action: [role for role in ('reseller', 'admin', 'superadmin')
                     if package_allowed_in_panel(package, SimpleNamespace(role=role, is_superadmin=False), reseller_id, action)]
            for action in ('create', 'renew')
        }
    return jsonify(success=True, quote=quote)


# ── Package scope / reseller endpoints ───────────────────────────────────────

@bp.route('/admin/packages/<int:package_id>/assign', methods=['POST'])
@permission_required('content.manage')
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

def _calculate_minimum_price(volume_gb, days, reseller_id=None, server_id=None, user=None):
    return calculate_tariff_price(volume_gb, days, reseller_id, server_id, user)


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

    if not all(math.isfinite(n) and 0 <= n <= 1000000 for n in (volume_gb, days)):
        return jsonify(success=False, error='Invalid volume or duration'), 400

    if request.args.get('purpose') in ('create', 'renew'):
        try:
            if request.args['purpose'] == 'create':
                reseller_id = creation_reseller_id(user, request.args.get('reseller_id'))
            else:
                reseller_id = _renewal_reseller_id(user, server_id)
            payer = db.session.get(Admin, reseller_id) if reseller_id else None
            quote = pricing_quote(payer, volume_gb=volume_gb, days=days, server_id=server_id,
                                  unlimited=request.args.get('unlimited') == '1')
        except PermissionError as exc:
            return jsonify(success=False, error=str(exc)), 403
        except ValueError as exc:
            return jsonify(success=False, error=str(exc)), 400
        return jsonify(success=True, min_price=quote['amount'], quota_mib=quote['quota_mib'],
                       selling_price=quote['retail'], tier_id=quote['tier_id'],
                       quote_revision=pricing_quote_revision(quote))

    if request.args.get('basis') == 'system':
        quote = pricing_quote(None, volume_gb=volume_gb, days=days, server_id=server_id)
        return jsonify(success=True, min_price=quote['amount'])

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
@permission_required('content.manage')
def list_price_tiers():
    reseller_id = request.args.get('reseller_id', type=int)
    tiers = PriceTier.query.order_by(PriceTier.priority.desc(), PriceTier.id).all()
    if reseller_id:
        tiers = [tier for tier in tiers
                 if reseller_id in (assigned := _tier_assigned_reseller_ids(tier))
                 or (not assigned and not tier.applies_to_system)]
    return jsonify({'success': True, 'tiers': [t.to_dict() for t in tiers]})


@bp.route('/api/price-tiers', methods=['POST'])
@permission_required('content.manage')
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
            reseller_cost_per_gb=_optional_cost(data.get('reseller_cost_per_gb')),
            reseller_cost_per_day=_optional_cost(data.get('reseller_cost_per_day')),
            reseller_id=assigned_ids[0] if len(assigned_ids) == 1 else None,
            assigned_reseller_ids=json.dumps(assigned_ids),
            applies_to_system=data.get('applies_to_system') is True,
            server_id=int(data['server_id']) if data.get('server_id') else None,
            priority=int(data.get('priority') or 0),
            is_active=bool(data.get('is_active', True)),
            created_by=admin_id,
        )
        _validate_tier_ranges(tier)
        db.session.add(tier)
        db.session.commit()
        return jsonify({'success': True, 'id': tier.id, 'tier': tier.to_dict()})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400


@bp.route('/api/price-tiers/<int:tier_id>', methods=['PUT'])
@permission_required('content.manage')
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
        for _field in ('reseller_cost_per_gb', 'reseller_cost_per_day'):
            if _field in data:
                setattr(tier, _field, _optional_cost(data[_field]))
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
        if 'applies_to_system' in data:
            tier.applies_to_system = data['applies_to_system'] is True
        if 'is_active' in data:
            tier.is_active = bool(data['is_active'])
        with db.session.no_autoflush:
            _validate_tier_ranges(tier)
        db.session.commit()
        return jsonify({'success': True, 'tier': tier.to_dict()})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400


def _validate_tier_ranges(tier):
    def interval(low, high):
        if any(value is not None and (not math.isfinite(value) or value < 0) for value in (low, high)):
            raise ValueError('Range bounds must be finite and non-negative')
        start, end = low or 0, high if high is not None else math.inf
        if start >= end:
            raise ValueError('The upper bound must be greater than the lower bound')
        return start, end

    def ranges(row):
        return (interval(row.min_volume_gb, row.max_volume_gb), interval(row.min_days, row.max_days))

    current = ranges(tier)
    if not tier.is_active:
        return
    ids = _tier_assigned_reseller_ids(tier)
    global_scope = not ids and not tier.applies_to_system
    for other in PriceTier.query.filter_by(is_active=True, server_id=tier.server_id, priority=tier.priority or 0):
        other_ids = _tier_assigned_reseller_ids(other)
        same_scope = ((global_scope and not other_ids and not other.applies_to_system)
                      or (tier.applies_to_system and other.applies_to_system)
                      or ids.intersection(other_ids))
        if other.id == tier.id or not same_scope:
            continue
        try:
            existing = ranges(other)
        except ValueError:  # Ignore obsolete malformed legacy bounds.
            continue
        if all(max(left[0], right[0]) < min(left[1], right[1]) for left, right in zip(current, existing)):
            raise ValueError('Overlapping active rule at the same priority and scope')


@bp.route('/api/price-tiers/<int:tier_id>', methods=['DELETE'])
@permission_required('content.manage')
def delete_price_tier(tier_id):
    tier = db.session.get(PriceTier, tier_id)
    if not tier:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    db.session.delete(tier)
    db.session.commit()
    return jsonify({'success': True})
