"""Bank card management API routes (extracted from app.py)."""
import json

from flask import Blueprint, jsonify, request, session

from panel.extensions import db
from panel.models import Admin, BankCard
from panel.routes.common import login_required, user_management_required

bp = Blueprint('bank_cards', __name__)


@bp.route('/api/bank-cards', methods=['GET'])
@login_required
def list_bank_cards():
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401
    include_inactive = request.args.get('include_inactive', '0') in ('1', 'true', 'True')
    query = BankCard.query
    if not (user.role == 'superadmin' or user.is_superadmin):
        query = query.filter_by(is_active=True)
    elif not include_inactive:
        query = query.filter_by(is_active=True)
    cards = query.order_by(BankCard.created_at.desc()).all()
    if not (user.role == 'superadmin' or user.is_superadmin):
        cards = [card for card in cards if _bank_card_accessible_to(card, user)]
    return jsonify({'success': True, 'cards': [card.to_dict() for card in cards]})


def _bank_card_accessible_to(card, user):
    """Non-superadmins see central cards, their own cards, and cards assigned to them."""
    if not card.reseller_id:
        return True
    if user and card.reseller_id == user.id:
        return True
    try:
        assigned = json.loads(card.assigned_reseller_ids or '[]')
    except Exception:
        assigned = []
    return bool(user) and user.id in [int(value) for value in assigned]


def _bank_card_reseller_fields(data):
    """Validate and normalize reseller ownership fields; returns (reseller_id, assigned_json, error)."""
    reseller_id = data.get('reseller_id')
    if reseller_id in ('', 0, '0'):
        reseller_id = None
    if reseller_id is not None:
        try:
            reseller_id = int(reseller_id)
        except (TypeError, ValueError):
            return None, None, 'Invalid reseller_id'
        owner = db.session.get(Admin, reseller_id)
        if not owner or str(owner.role or '').lower() != 'reseller':
            return None, None, 'reseller_id must reference a reseller admin'
    assigned_ids = data.get('assigned_reseller_ids', [])
    if not isinstance(assigned_ids, list):
        assigned_ids = []
    try:
        assigned_json = json.dumps([int(value) for value in assigned_ids])
    except (TypeError, ValueError):
        return None, None, 'Invalid assigned_reseller_ids'
    return reseller_id, assigned_json, None


@bp.route('/api/bank-cards', methods=['POST'])
@user_management_required
def create_bank_card():
    from app import sanitize_html  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    data = request.get_json() or {}
    label = (data.get('label') or '').strip()
    if not label:
        return jsonify({'success': False, 'error': 'Label is required'}), 400

    card = BankCard(
        label=sanitize_html(label),
        bank_name=sanitize_html((data.get('bank_name') or '').strip() or None),
        owner_name=sanitize_html((data.get('owner_name') or '').strip() or None),
        card_number=sanitize_html((data.get('card_number') or '').strip() or None),
        iban=sanitize_html((data.get('iban') or '').strip() or None),
        account_number=sanitize_html((data.get('account_number') or '').strip() or None),
        notes=sanitize_html((data.get('notes') or '').strip() or None),
        is_active=bool(data.get('is_active', True))
    )
    if user and (user.role == 'superadmin' or user.is_superadmin):
        if 'reseller_id' in data or 'assigned_reseller_ids' in data:
            reseller_id, assigned_json, error = _bank_card_reseller_fields(data)
            if error:
                return jsonify({'success': False, 'error': error}), 400
            card.reseller_id = reseller_id
            card.assigned_reseller_ids = assigned_json
    db.session.add(card)
    db.session.commit()
    return jsonify({'success': True, 'card': card.to_dict()})

@bp.route('/api/bank-cards/<int:card_id>', methods=['PUT'])
@user_management_required
def update_bank_card(card_id):
    from app import sanitize_html  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    card = db.session.get(BankCard, card_id)
    if not card:
        return jsonify({'success': False, 'error': 'Card not found'}), 404
    data = request.get_json() or {}
    for field in ('label', 'bank_name', 'owner_name', 'card_number', 'iban', 'account_number', 'notes'):
        if field in data:
            value = data.get(field)
            if isinstance(value, str):
                value = sanitize_html(value.strip())
            setattr(card, field, value)
    if 'is_active' in data:
        card.is_active = bool(data.get('is_active'))
    if user and (user.role == 'superadmin' or user.is_superadmin):
        if 'reseller_id' in data or 'assigned_reseller_ids' in data:
            reseller_id, assigned_json, error = _bank_card_reseller_fields(data)
            if error:
                return jsonify({'success': False, 'error': error}), 400
            if 'reseller_id' in data:
                card.reseller_id = reseller_id
            if 'assigned_reseller_ids' in data:
                card.assigned_reseller_ids = assigned_json
    db.session.commit()
    return jsonify({'success': True, 'card': card.to_dict()})

@bp.route('/api/bank-cards/<int:card_id>', methods=['DELETE'])
@user_management_required
def delete_bank_card(card_id):
    card = db.session.get(BankCard, card_id)
    if not card:
        return jsonify({'success': False, 'error': 'Card not found'}), 404
    db.session.delete(card)
    db.session.commit()
    return jsonify({'success': True})
