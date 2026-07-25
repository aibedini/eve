"""Manual receipt upload/review and auto-approval-window API routes (extracted from app.py)."""
import os
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file, session, url_for

from panel.extensions import db
from panel.models import (
    Admin, AutoApprovalWindow, BankCard, ManualReceipt,
    RECEIPT_STATUS_APPROVED, RECEIPT_STATUS_AUTO_APPROVED,
    RECEIPT_STATUS_AUTO_PENDING, RECEIPT_STATUS_PENDING, RECEIPT_STATUS_REJECTED,
)
from panel.routes.common import login_required, user_management_required

bp = Blueprint('receipts', __name__)


@bp.route('/api/receipts', methods=['POST'])
@login_required
def upload_receipt():
    from app import MAX_FILE_SIZE, allowed_receipt_file, get_active_auto_window, parse_iso_datetime, save_receipt_file, trigger_auto_receipt_processing  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401
    trigger_auto_receipt_processing()

    form = request.form
    try:
        amount = int(form.get('amount', 0))
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify({'success': False, 'error': 'Amount must be positive'}), 400

    card_id = form.get('card_id')
    card = None
    if card_id:
        try:
            card = db.session.get(BankCard, int(card_id))
        except (TypeError, ValueError):
            card = None
        if not card:
            return jsonify({'success': False, 'error': 'Selected card not found'}), 404
        if not card.is_active and not (user.role == 'superadmin' or user.is_superadmin):
            return jsonify({'success': False, 'error': 'Card is inactive'}), 400

    slip_file = request.files.get('file')
    if not slip_file or not slip_file.filename:
        return jsonify({'success': False, 'error': 'Receipt image is required'}), 400

    slip_file.seek(0, os.SEEK_END)
    file_length = slip_file.tell()
    slip_file.seek(0)
    if file_length > MAX_FILE_SIZE:
        return jsonify({'success': False, 'error': 'File too large'}), 413

    if not allowed_receipt_file(slip_file):
        return jsonify({'success': False, 'error': 'Unsupported file type'}), 400
    stored_path = save_receipt_file(slip_file)
    if not stored_path:
        return jsonify({'success': False, 'error': 'Failed to store file'}), 400

    deposit_at = parse_iso_datetime(form.get('deposit_at'))
    reference_code = (form.get('reference_code') or '').strip() or None
    notes = (form.get('notes') or '').strip() or None
    currency = (form.get('currency') or 'IRT').strip().upper()
    if len(currency) > 10:
        currency = currency[:10]

    auto_window = get_active_auto_window()
    initial_status = RECEIPT_STATUS_PENDING
    auto_deadline = None
    if auto_window and (auto_window.max_amount <= 0 or amount <= auto_window.max_amount):
        initial_status = RECEIPT_STATUS_AUTO_PENDING
        auto_deadline = auto_window.ends_at

    receipt = ManualReceipt(
        admin_id=user.id,
        card_id=card.id if card else None,
        amount=amount,
        currency=currency,
        deposit_at=deposit_at,
        reference_code=reference_code,
        image_path=stored_path,
        status=initial_status,
        auto_deadline=auto_deadline,
        notes=notes
    )
    db.session.add(receipt)
    db.session.commit()

    payload = receipt.to_dict()
    payload['image_url'] = url_for('receipts.download_receipt_file', receipt_id=receipt.id)
    return jsonify({'success': True, 'receipt': payload})

@bp.route('/api/receipts', methods=['GET'])
@login_required
def list_receipts():
    from app import trigger_auto_receipt_processing  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401
    trigger_auto_receipt_processing()
    query = ManualReceipt.query.join(Admin, ManualReceipt.admin_id == Admin.id)
    if not (user.role == 'superadmin' or user.is_superadmin):
        query = query.filter(ManualReceipt.admin_id == user.id)
    else:
        admin_filter = request.args.get('user_id', type=int)
        if admin_filter:
            query = query.filter(ManualReceipt.admin_id == admin_filter)
    status_filter = request.args.get('status')
    if status_filter:
        query = query.filter(ManualReceipt.status == status_filter)
    limit = request.args.get('limit', type=int) or 200
    limit = max(1, min(limit, 1000))
    receipts = query.order_by(ManualReceipt.created_at.desc()).limit(limit).all()
    payload = []
    for receipt in receipts:
        data = receipt.to_dict()
        data['image_url'] = url_for('receipts.download_receipt_file', receipt_id=receipt.id)
        payload.append(data)
    return jsonify({'success': True, 'receipts': payload})

@bp.route('/receipts/file/<int:receipt_id>')
@login_required
def download_receipt_file(receipt_id):
    from app import RECEIPTS_DIR, app  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401
    receipt = db.session.get(ManualReceipt, receipt_id)
    if not receipt:
        return jsonify({'success': False, 'error': 'Receipt not found'}), 404
    if receipt.admin_id != user.id and not (user.role == 'superadmin' or user.is_superadmin):
        return jsonify({'success': False, 'error': 'Forbidden'}), 403
    if not receipt.image_path:
        return jsonify({'success': False, 'error': 'File missing'}), 404
    full_path = os.path.join(app.instance_path, receipt.image_path)
    if not os.path.abspath(full_path).startswith(os.path.abspath(RECEIPTS_DIR)):
        return jsonify({'success': False, 'error': 'Invalid path'}), 403
    if not os.path.isfile(full_path):
        return jsonify({'success': False, 'error': 'File missing'}), 404
    return send_file(full_path, as_attachment=False)

@bp.route('/api/receipts/<int:receipt_id>/approve', methods=['POST'])
@user_management_required
def approve_receipt(receipt_id):
    from app import apply_receipt_credit, trigger_auto_receipt_processing  # deferred: app-level helper, avoids circular import
    trigger_auto_receipt_processing()
    receipt = db.session.get(ManualReceipt, receipt_id)
    if not receipt:
        return jsonify({'success': False, 'error': 'Receipt not found'}), 404
    reviewer = db.session.get(Admin, session['admin_id'])
    allowed_states = {RECEIPT_STATUS_PENDING, RECEIPT_STATUS_AUTO_PENDING, RECEIPT_STATUS_REJECTED}
    if receipt.status not in allowed_states:
        if receipt.status in (RECEIPT_STATUS_APPROVED, RECEIPT_STATUS_AUTO_APPROVED):
            data = receipt.to_dict()
            data['image_url'] = url_for('receipts.download_receipt_file', receipt_id=receipt.id)
            return jsonify({'success': True, 'receipt': data})
        return jsonify({'success': False, 'error': 'Invalid receipt state'}), 400
    success, error = apply_receipt_credit(receipt, reviewer=reviewer, auto=False)
    if not success:
        return jsonify({'success': False, 'error': error}), 400
    db.session.commit()
    data = receipt.to_dict()
    data['image_url'] = url_for('receipts.download_receipt_file', receipt_id=receipt.id)
    data['new_balance'] = receipt.admin.credit if receipt.admin else None
    return jsonify({'success': True, 'receipt': data})

@bp.route('/api/receipts/<int:receipt_id>/reject', methods=['POST'])
@user_management_required
def reject_receipt(receipt_id):
    from app import rollback_receipt_credit, trigger_auto_receipt_processing  # deferred: app-level helper, avoids circular import
    trigger_auto_receipt_processing()
    receipt = db.session.get(ManualReceipt, receipt_id)
    if not receipt:
        return jsonify({'success': False, 'error': 'Receipt not found'}), 404
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip() or 'Rejected'
    reviewer = db.session.get(Admin, session['admin_id'])
    if receipt.status in (RECEIPT_STATUS_APPROVED, RECEIPT_STATUS_AUTO_APPROVED):
        success, error = rollback_receipt_credit(receipt, reviewer=reviewer, reason=reason)
        if not success:
            return jsonify({'success': False, 'error': error}), 400
    receipt.status = RECEIPT_STATUS_REJECTED
    receipt.reviewer_id = reviewer.id if reviewer else None
    receipt.reviewed_at = datetime.utcnow()
    receipt.rejection_reason = reason
    receipt.auto_deadline = None
    db.session.commit()
    data = receipt.to_dict()
    data['image_url'] = url_for('receipts.download_receipt_file', receipt_id=receipt.id)
    return jsonify({'success': True, 'receipt': data})

@bp.route('/api/receipts/auto-windows', methods=['GET'])
@user_management_required
def list_auto_windows():
    windows = AutoApprovalWindow.query.order_by(AutoApprovalWindow.starts_at.desc()).all()
    return jsonify({'success': True, 'windows': [w.to_dict() for w in windows]})

@bp.route('/api/receipts/auto-windows', methods=['POST'])
@user_management_required
def create_auto_window():
    from app import parse_iso_datetime  # deferred: app-level helper, avoids circular import
    data = request.get_json() or {}
    starts_at = parse_iso_datetime(data.get('starts_at')) or datetime.utcnow()
    ends_at = parse_iso_datetime(data.get('ends_at'))
    if not ends_at or ends_at <= starts_at:
        return jsonify({'success': False, 'error': 'Invalid window timeframe'}), 400
    try:
        max_amount = int(data.get('max_amount', 0) or 0)
    except (TypeError, ValueError):
        max_amount = 0
    window = AutoApprovalWindow(
        starts_at=starts_at,
        ends_at=ends_at,
        max_amount=max_amount,
        status='enabled'
    )
    db.session.add(window)
    db.session.commit()
    return jsonify({'success': True, 'window': window.to_dict()})

@bp.route('/api/receipts/auto-windows/<int:window_id>', methods=['DELETE'])
@user_management_required
def disable_auto_window(window_id):
    window = db.session.get(AutoApprovalWindow, window_id)
    if not window:
        return jsonify({'success': False, 'error': 'Window not found'}), 404
    window.status = 'disabled'
    db.session.commit()
    return jsonify({'success': True})
