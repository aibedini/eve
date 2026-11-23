"""Human administration for machine keys and inert proposals."""
import json

from flask import Blueprint, abort, render_template, request, session

from panel.extensions import db
from panel.models.machine import MachineAPIKey, MachineProposal
from panel.routes.common import superadmin_required
from panel.services.machine_security import SCOPES, issue_key, utcnow

bp = Blueprint('machine_admin', __name__)


@bp.route('/admin/machine-integration', methods=['GET', 'POST'])
@superadmin_required
def settings():
    from app import _log_audit

    issued = error = None
    status = 200
    if request.method == 'POST':
        if not request.is_secure:
            abort(403)
        action = request.form.get('action')
        if action == 'issue':
            try:
                row, issued = issue_key(request.form.get('name'), request.form.getlist('scopes'),
                                        int(request.form.get('days', '')), session['admin_id'])
            except (ValueError, TypeError):
                error, status = 'invalid_key', 400
            else:
                _log_audit('machine_key_created', target=row.id, meta={'scopes': row.scopes})
                db.session.commit()
        elif action == 'revoke':
            row = db.get_or_404(MachineAPIKey, request.form.get('id'))
            if row.revoked_at is None:
                row.revoked_at = utcnow()
                _log_audit('machine_key_revoked', target=row.id)
                db.session.commit()
        elif action in ('approved', 'rejected'):
            proposal_id = request.form.get('id')
            changed = MachineProposal.query.filter_by(id=proposal_id, status='pending').update({
                MachineProposal.status: action, MachineProposal.decided_at: utcnow(),
                MachineProposal.decided_by: session['admin_id'],
            })
            if changed:
                _log_audit('machine_proposal_decided', target=proposal_id, meta={'decision': action})
                db.session.commit()
        else:
            abort(400)
    keys = db.session.query(MachineAPIKey.id, MachineAPIKey.name, MachineAPIKey.scopes, MachineAPIKey.expires_at,
                            MachineAPIKey.last_used_at, MachineAPIKey.revoked_at).order_by(
        MachineAPIKey.expires_at.desc()).limit(200).all()
    proposals = MachineProposal.query.order_by(MachineProposal.created_at.desc()).limit(50).all()
    return render_template('machine_integration.html', scopes=SCOPES, keys=keys, proposals=proposals,
                           issued=issued, error=error, format_json=lambda value: json.dumps(
                               json.loads(value), indent=2, ensure_ascii=False)), status, {'Cache-Control': 'no-store'}
