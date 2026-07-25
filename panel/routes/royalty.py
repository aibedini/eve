"""Royalty idle-client API routes (extracted from app.py)."""
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from panel.routes.common import login_required

bp = Blueprint('royalty', __name__)


@bp.route('/api/royalty/idle', methods=['GET'])
@login_required
def royalty_idle_clients():
    """List active accounts with no traffic in the window. Fast via the
    (server_id, sub_id, recorded_at) index; server timing is logged."""
    from app import (  # deferred: app-level helper, avoids circular import
        RoyaltyBaselineError, _compute_royalty_idle, _royalty_parse_filters,
    )
    days, server_filter, reseller_filter = _royalty_parse_filters(request.args)
    try:
        idle = _compute_royalty_idle(session['admin_id'], days, server_filter, reseller_filter)
    except RoyaltyBaselineError:
        return jsonify({'success': False, 'error': 'Usage history not available yet. Try a smaller window.'}), 200
    return jsonify({
        'success': True,
        'days': days,
        'count': len(idle),
        'clients': idle,
        'generated_at': datetime.utcnow().isoformat(),
    })
