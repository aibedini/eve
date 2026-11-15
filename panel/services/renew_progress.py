"""Read-only, operation-scoped renewal progress for the panel activity drawer."""

import hashlib
from datetime import datetime, timezone

from panel.models import SmsSendLog
from panel.services.client_operations import operation_context, operation_response
from panel.services.renew_notifications import notification_key


def notification_id(operation):
    return 'notif_' + hashlib.sha256(notification_key(operation).encode()).hexdigest()[:40]


def notification_logs(operations):
    keys = [notification_id(operation) for operation in operations]
    if not keys:
        return {}
    latest = {}
    for row in (SmsSendLog.query.filter(SmsSendLog.eve_notification_id.in_(keys))
                .order_by(SmsSendLog.id.desc()).all()):
        latest.setdefault(row.eve_notification_id, row)
    return latest


def sms_status(operation, row=None):
    result = operation_response(operation)
    if row is not None:
        if row.carrier_state == 'delivered':
            return 'delivered'
        if row.status == 'skipped':
            return 'skipped'
        if row.status == 'manual_review' or getattr(row, 'verification_status', None) == 'manual_review_required':
            return 'manual_review'
        if row.status == 'failed' or row.carrier_state in ('failed', 'undelivered') \
                or (row.terminal is True and row.successful is False):
            return 'failed'
        if row.successful is True or row.status == 'sent':
            return 'accepted'
        if row.status in ('queued', 'deferred', 'pending', 'accepted'):
            return 'queued'
        return 'sending'
    if result.get('renew_sms_sent_at'):
        # This marker means the background worker was scheduled, not delivery.
        return 'sending'
    if operation.state == 'completed':
        if result.get('tpl_vars'):
            return 'dispatch_pending'
        return 'unknown'
    return 'waiting'


def sms_progress(operation, row=None):
    status = sms_status(operation, row)
    result = operation_response(operation)
    scheduled = result.get('renew_sms_sent_at')
    if scheduled:
        try:
            scheduled = datetime.fromisoformat(scheduled).replace(tzinfo=timezone.utc).isoformat()
        except (TypeError, ValueError):
            scheduled = None
    return {
        'status': status,
        'request_id': getattr(row, 'request_id', None),
        'gateway_state': getattr(row, 'gateway_state', None),
        'gateway_stage': getattr(row, 'stage', None),
        'delivery_state': getattr(row, 'carrier_state', None),
        'reason': getattr(row, 'reason', None) or (
            'missing_content' if result.get('rebuilt_after_restart') and not result.get('tpl_vars') else None),
        'scheduled_at': scheduled,
        'updated_at': row.updated_at.replace(tzinfo=timezone.utc).isoformat()
                      if getattr(row, 'updated_at', None) else None,
    }


def snapshot(operation, sms_log=None, *, detail=True):
    result = operation_response(operation)
    context = operation_context(operation)
    verify = result.get('verify') if isinstance(result.get('verify'), dict) else {}
    final_state = result.get('final_state') or verify.get('final_state')
    if not final_state:
        final_state = {'failed': 'NOT_APPLIED', 'needs_reconciliation': 'UNKNOWN',
                       'activation_pending': 'CONFIG_APPLIED_ACTIVATION_PENDING'}.get(operation.state)
    confirmed = final_state == 'APPLIED_ACTIVE' and result.get('business_finalized') is True
    notification = sms_progress(operation, sms_log)
    sms = notification['status']
    retryable = bool(confirmed and sms_log is not None and sms == 'failed'
                     and sms_log.terminal is True and sms_log.successful is False)
    attention = (operation.state in ('failed', 'needs_reconciliation')
                 or final_state in ('UNKNOWN', 'PARTIALLY_APPLIED', 'AUTH_DEGRADED', 'NOT_APPLIED')
                 or sms in ('failed', 'skipped', 'unknown', 'manual_review', 'dispatch_pending'))
    notification['retryable'] = retryable
    view = {
        'operation_id': operation.idempotency_key,
        'server_id': operation.server_id,
        'inbound_id': operation.inbound_id,
        'email': operation.client_email,
        'state': operation.state,
        'final_state': final_state,
        'business_finalized': result.get('business_finalized') is True,
        'confirmed': confirmed,
        'config_applied': result.get('config_applied') is True or verify.get('config_applied') is True,
        'reason_codes': verify.get('reason_codes') or [
            item.get('code') for item in (result.get('failed_steps') or []) if isinstance(item, dict)],
        'repairable': not confirmed and (result.get('repairable') is True
                       or any(code in ('GLOBAL_DISABLED', 'MEMBERSHIP_DISABLED',
                                       'MEMBERSHIP_MISSING', 'TRAFFIC_DISABLED')
                              for code in (verify.get('reason_codes') or []))),
        'attention': attention,
        'pending': operation.state in ('reserved', 'prepared', 'panel_applied', 'activation_pending')
                   or sms in ('sending', 'queued'),
        'days': context.get('days'),
        'volume_gb': context.get('volume_gb'),
        'amount': operation.amount,
        'sms': notification,
        'created_at': operation.created_at.replace(tzinfo=timezone.utc).isoformat(),
        'updated_at': operation.updated_at.replace(tzinfo=timezone.utc).isoformat(),
    }
    if detail:
        view.update(copy_text=result.get('copy_text') if confirmed else None,
                    tpl_vars=result.get('tpl_vars') if confirmed else None,
                    trace_id=result.get('trace_id') or context.get('renewal_trace_id'),
                    timing=result.get('timing') or {}, error=operation.error)
    return view
