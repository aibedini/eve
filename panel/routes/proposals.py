"""Operator review of GOD AI proposals (XR-GA-019).

GOD AI submits proposals over ``POST /machine/v1/proposals``; this page is where
an EVE operator accepts or rejects them. Accepting only records the decision --
the panel must never treat a proposal as an instruction to send an SMS or mutate
a client. A proposal whose ``generation`` no longer matches the service's current
``service_lifecycle_states`` row is marked ``expired`` instead.
"""
from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from panel.extensions import db
from panel.models import PROPOSAL_STATUS_EXPIRED, Proposal
from panel.routes.common import current_admin, permission_required
from panel.services import proposals as proposals_service

bp = Blueprint('proposals', __name__)

#: Flash-style codes carried through the redirect (Post/Redirect/Get).
_NOTICE_CODES = frozenset({'accepted', 'expired', 'rejected'})
_ERROR_CODES = frozenset({'already_decided', 'reason_required', 'reason_too_long'})


def _shell_context(user) -> dict:
    return {
        'admin_username': (user.username if user is not None
                           else session.get('admin_username')),
        'is_superadmin': bool(user and (user.role == 'superadmin' or user.is_superadmin)),
        'role': (user.role if user is not None else session.get('role', 'admin')),
    }


def _render_queue(*, notice='', error='', conflict_proposal='') -> str:
    """Render the pending queue (and the result of the previous action)."""
    from app import format_app_datetime  # deferred: app-level helper, avoids circular import
    rows = proposals_service.pending_proposals()
    return render_template(
        'proposals.html',
        proposals=rows,
        customers=proposals_service.customer_map(rows),
        payload_preview=proposals_service.payload_preview,
        format_app_datetime=format_app_datetime,
        notice=notice if notice in _NOTICE_CODES else '',
        error=error if error in _ERROR_CODES else '',
        conflict_proposal=conflict_proposal,
        **_shell_context(current_admin()),
    )


@bp.route('/proposals')
@permission_required('proposals.review')
def proposals_page():
    notice = (request.args.get('notice') or '').strip().lower()
    return _render_queue(
        notice=notice,
        conflict_proposal=(request.args.get('proposal_id') or '').strip()[:128],
    )


@bp.route('/proposals/<int:proposal_pk>/accept', methods=['POST'])
@permission_required('proposals.review')
def accept_proposal(proposal_pk):
    proposal = db.session.get(Proposal, proposal_pk)
    if proposal is None:
        abort(404)
    try:
        result = proposals_service.accept_proposal(proposal, actor=current_admin())
    except proposals_service.ProposalError as exc:
        status = 409 if exc.code == 'already_decided' else 400
        return _render_queue(error=exc.code), status
    if result['status'] == PROPOSAL_STATUS_EXPIRED:
        # The world moved on: explain the conflict and record nothing else.
        return redirect(url_for(
            'proposals.proposals_page', notice='expired',
            proposal_id=proposal.proposal_id))
    return redirect(url_for('proposals.proposals_page', notice='accepted'))


@bp.route('/proposals/<int:proposal_pk>/reject', methods=['POST'])
@permission_required('proposals.review')
def reject_proposal(proposal_pk):
    proposal = db.session.get(Proposal, proposal_pk)
    if proposal is None:
        abort(404)
    try:
        proposals_service.reject_proposal(
            proposal, request.form.get('reason'), actor=current_admin())
    except proposals_service.ProposalError as exc:
        status = 409 if exc.code == 'already_decided' else 400
        return _render_queue(error=exc.code), status
    return redirect(url_for('proposals.proposals_page', notice='rejected'))
