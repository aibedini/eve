"""Custom subscription public page and management API routes (extracted from app.py)."""
import base64
import secrets
from urllib.parse import quote, unquote, urlparse

from flask import Blueprint, jsonify, make_response, request

from panel.extensions import db, limiter
from panel.models import CustomSubscription, CustomSubscriptionConfig
from panel.routes.common import login_required, user_management_required

bp = Blueprint('custom_subs', __name__)


CUSTOM_SUBSCRIPTION_SCHEMES = {
    'vless', 'vmess', 'trojan', 'ss', 'ssr', 'wireguard', 'hysteria2', 'tuic',
}


def _custom_subscription_uri(value):
    uri = str(value or '').strip()
    scheme = urlparse(uri).scheme.lower()
    if not uri or scheme not in CUSTOM_SUBSCRIPTION_SCHEMES:
        raise ValueError(
            'Unsupported config URI. Allowed schemes: '
            + ', '.join(sorted(CUSTOM_SUBSCRIPTION_SCHEMES))
        )
    return uri


def _custom_subscription_remark(row):
    if str(row.remark or '').strip():
        return str(row.remark).strip()
    fragment = str(row.uri or '').partition('#')[2]
    if fragment:
        try:
            return unquote(fragment).strip()
        except Exception:
            return fragment.strip()
    return f'config-{row.id}'


def _custom_subscription_render_uri(subscription, row):
    base = str(row.uri or '').partition('#')[0]
    label = f'{subscription.tag_prefix or ""}{_custom_subscription_remark(row)}'
    return f'{base}#{quote(label, safe="")}'


def _custom_subscription_public_url(row):
    return f'{request.url_root.rstrip("/")}/cs/{row.token}'



@bp.route('/cs/<token>')
@limiter.limit('60 per minute')
def public_custom_subscription(token):
    row = CustomSubscription.query.filter_by(token=str(token), enabled=True).first()
    if not row:
        return 'Subscription not found', 404
    configs = CustomSubscriptionConfig.query.filter_by(
        subscription_id=row.id, enabled=True,
    ).order_by(CustomSubscriptionConfig.sort_order.asc(), CustomSubscriptionConfig.id.asc()).all()
    raw = '\n'.join(_custom_subscription_render_uri(row, item) for item in configs)
    body = raw if request.args.get('format', '').lower() == 'raw' else base64.b64encode(
        raw.encode('utf-8')).decode('ascii')
    response = make_response(body)
    response.headers['Content-Type'] = 'text/plain; charset=utf-8'
    response.headers['Profile-Title'] = 'base64:' + base64.b64encode(
        row.name.encode('utf-8')).decode('ascii')
    if int(row.update_interval_min or 0) > 0:
        response.headers['Profile-Update-Interval'] = str(int(row.update_interval_min))
    return response


@bp.route('/api/custom-subscriptions', methods=['GET'])
@login_required
def list_custom_subscriptions():
    rows = CustomSubscription.query.order_by(
        CustomSubscription.sort_order.asc(), CustomSubscription.id.asc()).all()
    return jsonify({'success': True, 'subscriptions': [
        row.to_dict(public_url=_custom_subscription_public_url(row)) for row in rows
    ]})


@bp.route('/api/custom-subscriptions/<int:subscription_id>/preview', methods=['GET'])
@login_required
def preview_custom_subscription(subscription_id):
    row = db.session.get(CustomSubscription, subscription_id)
    if not row:
        return jsonify({'success': False, 'error': 'Subscription not found'}), 404
    configs = CustomSubscriptionConfig.query.filter_by(
        subscription_id=row.id, enabled=True,
    ).order_by(CustomSubscriptionConfig.sort_order.asc(), CustomSubscriptionConfig.id.asc()).all()
    return jsonify({
        'success': True,
        'raw': '\n'.join(_custom_subscription_render_uri(row, item) for item in configs),
    })


@bp.route('/api/custom-subscriptions', methods=['POST'])
@user_management_required
def create_custom_subscription():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()[:120]
    if not name:
        return jsonify({'success': False, 'error': 'Name is required'}), 400
    try:
        interval = int(data.get('update_interval_min') or 0)
        sort_order = int(data.get('sort_order') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Interval and sort order must be whole numbers'}), 400
    if interval < 0 or interval > 10080:
        return jsonify({'success': False, 'error': 'Update interval must be between 0 and 10080 minutes'}), 400
    row = CustomSubscription(
        name=name, token=secrets.token_urlsafe(16),
        tag_prefix=str(data.get('tag_prefix') or '')[:64],
        enabled=bool(data.get('enabled', True)), update_interval_min=interval,
        sort_order=sort_order,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({'success': True, 'subscription': row.to_dict(
        public_url=_custom_subscription_public_url(row))}), 201


@bp.route('/api/custom-subscriptions/<int:subscription_id>', methods=['PUT'])
@user_management_required
def update_custom_subscription(subscription_id):
    row = db.session.get(CustomSubscription, subscription_id)
    if not row:
        return jsonify({'success': False, 'error': 'Subscription not found'}), 404
    data = request.get_json(silent=True) or {}
    if 'name' in data:
        name = str(data.get('name') or '').strip()[:120]
        if not name:
            return jsonify({'success': False, 'error': 'Name is required'}), 400
        row.name = name
    if 'tag_prefix' in data:
        row.tag_prefix = str(data.get('tag_prefix') or '')[:64]
    if 'enabled' in data:
        row.enabled = bool(data.get('enabled'))
    try:
        if 'update_interval_min' in data:
            row.update_interval_min = int(data.get('update_interval_min') or 0)
        if 'sort_order' in data:
            row.sort_order = int(data.get('sort_order') or 0)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Interval and sort order must be whole numbers'}), 400
    if row.update_interval_min < 0 or row.update_interval_min > 10080:
        return jsonify({'success': False, 'error': 'Update interval must be between 0 and 10080 minutes'}), 400
    if bool(data.get('regenerate_token')):
        row.token = secrets.token_urlsafe(16)
    db.session.commit()
    return jsonify({'success': True, 'subscription': row.to_dict(
        public_url=_custom_subscription_public_url(row))})


@bp.route('/api/custom-subscriptions/<int:subscription_id>', methods=['DELETE'])
@user_management_required
def delete_custom_subscription(subscription_id):
    row = db.session.get(CustomSubscription, subscription_id)
    if not row:
        return jsonify({'success': False, 'error': 'Subscription not found'}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/custom-subscriptions/<int:subscription_id>/configs', methods=['POST'])
@user_management_required
def add_custom_subscription_configs(subscription_id):
    row = db.session.get(CustomSubscription, subscription_id)
    if not row:
        return jsonify({'success': False, 'error': 'Subscription not found'}), 404
    data = request.get_json(silent=True) or {}
    lines = [line.strip() for line in str(data.get('configs') or '').splitlines() if line.strip()]
    if not lines:
        return jsonify({'success': False, 'error': 'Paste at least one config URI'}), 400
    if len(lines) > 1000:
        return jsonify({'success': False, 'error': 'At most 1000 configs can be added at once'}), 400
    try:
        uris = [_custom_subscription_uri(line) for line in lines]
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    if len(set(uris)) != len(uris):
        return jsonify({'success': False, 'error': 'The pasted list contains duplicate configs'}), 409
    existing = {value for value, in db.session.query(CustomSubscriptionConfig.uri).filter_by(
        subscription_id=row.id).all()}
    duplicates = [uri for uri in uris if uri in existing]
    if duplicates:
        return jsonify({'success': False, 'error': 'One or more configs already exist in this subscription'}), 409
    next_order = max([item.sort_order for item in row.configs] or [-1]) + 1
    created = []
    for offset, uri in enumerate(uris):
        item = CustomSubscriptionConfig(
            subscription_id=row.id, uri=uri, enabled=True,
            sort_order=next_order + offset,
        )
        db.session.add(item)
        created.append(item)
    db.session.commit()
    return jsonify({'success': True, 'configs': [item.to_dict() for item in created]}), 201


@bp.route('/api/custom-subscriptions/<int:subscription_id>/configs/<int:config_id>', methods=['PUT'])
@user_management_required
def update_custom_subscription_config(subscription_id, config_id):
    item = CustomSubscriptionConfig.query.filter_by(
        id=config_id, subscription_id=subscription_id).first()
    if not item:
        return jsonify({'success': False, 'error': 'Config not found'}), 404
    data = request.get_json(silent=True) or {}
    if 'uri' in data:
        try:
            uri = _custom_subscription_uri(data.get('uri'))
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 400
        duplicate = CustomSubscriptionConfig.query.filter(
            CustomSubscriptionConfig.subscription_id == subscription_id,
            CustomSubscriptionConfig.uri == uri,
            CustomSubscriptionConfig.id != item.id,
        ).first()
        if duplicate:
            return jsonify({'success': False, 'error': 'This config already exists'}), 409
        item.uri = uri
    if 'remark' in data:
        item.remark = str(data.get('remark') or '').strip()[:190] or None
    if 'enabled' in data:
        item.enabled = bool(data.get('enabled'))
    if 'sort_order' in data:
        try:
            item.sort_order = int(data.get('sort_order'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Sort order must be a whole number'}), 400
    db.session.commit()
    return jsonify({'success': True, 'config': item.to_dict()})


@bp.route('/api/custom-subscriptions/<int:subscription_id>/configs/<int:config_id>', methods=['DELETE'])
@user_management_required
def delete_custom_subscription_config(subscription_id, config_id):
    item = CustomSubscriptionConfig.query.filter_by(
        id=config_id, subscription_id=subscription_id).first()
    if not item:
        return jsonify({'success': False, 'error': 'Config not found'}), 404
    db.session.delete(item)
    db.session.commit()
    return jsonify({'success': True})
