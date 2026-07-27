"""Ownership-claim review & verification service (extracted from app.py)."""
import hmac
from datetime import datetime

from panel.core.phone import normalize_iran_mobile
from panel.core.redis_client import (
    GLOBAL_REFRESH_LOCK,
    GLOBAL_SERVER_DATA,
    load_snapshot_from_redis,
)
from panel.extensions import db
from panel.models import (
    OwnershipClaim,
    OwnershipClaimItem,
    ServiceOwnership,
    TelegramIdentity,
)

def _can_review_ownership_claim(reviewer, claim: OwnershipClaim) -> bool:
    if not reviewer:
        return False
    role = str(getattr(reviewer, 'role', '') or '').strip().lower()
    if bool(getattr(reviewer, 'is_superadmin', False)) or role in ('admin', 'superadmin'):
        return True
    return role == 'reseller' and claim.requested_reseller_id == getattr(reviewer, 'id', None)


def _refresh_ownership_claim_status(claim: OwnershipClaim):
    statuses = [item.status for item in claim.items]
    if not statuses or any(status == 'pending' for status in statuses):
        claim.status = 'pending'
    elif any(status == 'conflict' for status in statuses):
        claim.status = 'needs_attention'
    elif all(status == 'approved' for status in statuses):
        claim.status = 'approved'
    elif any(status == 'approved' for status in statuses):
        claim.status = 'partially_approved'
    else:
        claim.status = 'rejected'
    return claim.status


def review_ownership_claim_item(item_id: int, reviewer, *, approve: bool,
                                rejection_reason: str | None = None) -> dict:
    """Atomically approve or reject one claim item with tenant authorization."""
    item = OwnershipClaimItem.query.filter_by(id=int(item_id)).with_for_update().first()
    if not item:
        raise ValueError('Ownership claim item not found')
    claim = OwnershipClaim.query.filter_by(id=item.claim_id).with_for_update().first()
    if not claim or not _can_review_ownership_claim(reviewer, claim):
        raise PermissionError('Not permitted to review this ownership claim')
    if item.status not in ('pending', 'conflict'):
        raise ValueError('Ownership claim item has already been reviewed')

    now = datetime.utcnow()
    item.reviewed_by_admin_id = reviewer.id
    item.reviewed_at = now
    claim.reviewed_by_admin_id = reviewer.id
    claim.reviewed_at = now

    if not approve:
        item.status = 'rejected'
        item.rejection_reason = (rejection_reason or '').strip()[:2000] or None
        _refresh_ownership_claim_status(claim)
        db.session.commit()
        return {'success': True, 'status': item.status, 'claim_status': claim.status}

    ownership = ServiceOwnership.query.filter_by(
        server_id=item.server_id, client_uuid=item.client_uuid,
    ).with_for_update().first()
    if ownership and ownership.is_active and ownership.customer_id != claim.customer_id:
        item.status = 'conflict'
        item.conflict_owner_id = ownership.customer_id
        _refresh_ownership_claim_status(claim)
        db.session.commit()
        return {'success': False, 'status': 'conflict', 'claim_status': claim.status}

    if ownership is None:
        ownership = ServiceOwnership(
            customer_id=claim.customer_id,
            server_id=item.server_id,
            client_uuid=item.client_uuid,
        )
        db.session.add(ownership)
    else:
        ownership.customer_id = claim.customer_id
        ownership.revoked_at = None
    ownership.client_email_snapshot = item.client_email_snapshot
    ownership.reseller_id = claim.requested_reseller_id
    ownership.verification_method = claim.claim_method
    ownership.verified_by_admin_id = reviewer.id
    ownership.verified_at = now
    item.status = 'approved'
    item.conflict_owner_id = None
    item.rejection_reason = None
    _refresh_ownership_claim_status(claim)
    db.session.commit()
    return {
        'success': True,
        'status': item.status,
        'claim_status': claim.status,
        'service_ownership_id': ownership.id,
    }


def discover_phone_ownership_claim(identity: TelegramIdentity):
    """Create or reuse a claim containing live clients matching a verified phone."""
    if not identity or not identity.customer_id or not identity.phone_verified_at:
        raise ValueError('A verified Telegram identity is required')
    phone = normalize_iran_mobile(identity.phone_normalized)
    if not phone:
        raise ValueError('A verified phone is required')

    existing = OwnershipClaim.query.filter(
        OwnershipClaim.telegram_identity_id == identity.id,
        OwnershipClaim.status.in_(('pending', 'needs_attention', 'partially_approved')),
    ).order_by(OwnershipClaim.id.desc()).first()
    if existing and existing.items:
        return existing

    # The Telegram worker is a separate process. Pull the shared snapshot
    # before scanning; if Redis is unavailable, retain any local test snapshot.
    load_snapshot_from_redis(force=True)
    matches = []
    seen = set()
    with GLOBAL_REFRESH_LOCK:
        for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
            try:
                server_id = int(inbound.get('server_id') or 0)
            except (TypeError, ValueError):
                continue
            if not server_id:
                continue
            for client in (inbound.get('clients') or []):
                email = str(client.get('email') or '').strip()
                client_uuid = str(client.get('id') or '').strip()
                if not email or not client_uuid:
                    continue
                # Match the verified phone against the client name/email AND the
                # comment field — resellers store the subscriber's mobile in
                # either place and in any format (+98…, 09…, 0098…, spaced or
                # dashed, Persian/Arabic digits); normalize_iran_mobile
                # canonicalizes both sides before comparison.
                match_source = None
                if normalize_iran_mobile(email) == phone:
                    match_source = 'verified_phone_in_client_name'
                else:
                    _raw = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
                    for _cand in (client.get('comment'), _raw.get('comment')):
                        if _cand and normalize_iran_mobile(str(_cand)) == phone:
                            match_source = 'verified_phone_in_client_comment'
                            break
                if not match_source:
                    continue
                key = (server_id, client_uuid)
                if key in seen:
                    continue
                seen.add(key)
                ownership = ServiceOwnership.query.filter_by(
                    server_id=server_id, client_uuid=client_uuid,
                ).first()
                if ownership and ownership.is_active and ownership.customer_id == identity.customer_id:
                    continue
                matches.append((server_id, client_uuid, email, ownership, match_source))

    if not matches:
        return None
    claim = existing or OwnershipClaim(
        customer_id=identity.customer_id,
        telegram_identity_id=identity.id,
        verified_phone=phone,
        status='pending',
        claim_method='subscription_link',
    )
    if claim.id is None:
        db.session.add(claim)
        db.session.flush()
    existing_keys = {(item.server_id, item.client_uuid) for item in claim.items}
    for server_id, client_uuid, email, ownership, match_source in matches:
        if (server_id, client_uuid) in existing_keys:
            continue
        db.session.add(OwnershipClaimItem(
            claim_id=claim.id,
            server_id=server_id,
            client_uuid=client_uuid,
            client_email_snapshot=email[:255],
            match_reason=match_source,
            match_score=100 if match_source == 'verified_phone_in_client_name' else 95,
            status='conflict' if ownership and ownership.is_active else 'pending',
            conflict_owner_id=(ownership.customer_id if ownership and ownership.is_active else None),
        ))
    db.session.flush()
    db.session.expire(claim, ['items'])
    return claim


def _live_client_for_claim_item(item: OwnershipClaimItem):
    load_snapshot_from_redis(force=True)
    with GLOBAL_REFRESH_LOCK:
        for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
            try:
                if int(inbound.get('server_id') or 0) != int(item.server_id):
                    continue
            except (TypeError, ValueError):
                continue
            for client in (inbound.get('clients') or []):
                if hmac.compare_digest(str(client.get('id') or ''), str(item.client_uuid or '')):
                    return dict(client)
    return None


def verify_ownership_claim_subscription(item: OwnershipClaimItem, customer_id: int,
                                        supplied_sub_id: str) -> dict:
    """Verify a bearer subscription token and atomically attach its service."""
    if not item or item.claim.customer_id != int(customer_id):
        raise PermissionError('This service does not belong to the current claim')
    if item.status == 'approved':
        return {'success': True, 'status': 'approved', 'already_verified': True}
    if item.status not in ('pending', 'conflict'):
        raise ValueError('This service cannot be verified')
    live_client = _live_client_for_claim_item(item)
    if not live_client:
        raise ValueError('The service is not available in the current server snapshot')
    expected = str(live_client.get('subId') or live_client.get('id') or '').strip()
    supplied = str(supplied_sub_id or '').strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return {'success': False, 'status': 'invalid_subscription'}

    ownership = ServiceOwnership.query.filter_by(
        server_id=item.server_id, client_uuid=item.client_uuid,
    ).with_for_update().first()
    if ownership and ownership.is_active and ownership.customer_id != int(customer_id):
        item.status = 'conflict'
        item.conflict_owner_id = ownership.customer_id
        _refresh_ownership_claim_status(item.claim)
        db.session.flush()
        return {'success': False, 'status': 'conflict'}
    if ownership is None:
        ownership = ServiceOwnership(
            customer_id=int(customer_id), server_id=item.server_id,
            client_uuid=item.client_uuid,
        )
        db.session.add(ownership)
    ownership.customer_id = int(customer_id)
    ownership.client_email_snapshot = item.client_email_snapshot
    ownership.verification_method = 'subscription_link'
    ownership.verified_at = datetime.utcnow()
    ownership.revoked_at = None
    item.subscription_verified = True
    item.status = 'approved'
    item.conflict_owner_id = None
    item.reviewed_at = datetime.utcnow()
    _refresh_ownership_claim_status(item.claim)
    db.session.flush()
    return {'success': True, 'status': 'approved', 'service_ownership_id': ownership.id}
