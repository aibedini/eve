"""Read-only candidate-audit state: counts plus the legacy-history verdict.

Answers the one question the delivery-intelligence panel cannot answer from its
own KPIs: does a candidate manifest exist AT ALL? A send log with no decision
means historical sends predate the candidate audit, and that must never be read
as "there were no candidates" - the two are different claims and only one of them
is true.

Read-only by construction: no writes, no commits, no state changes. Every query
is wrapped so a diagnostic can never take down the surface that renders it.
"""
from sqlalchemy import func

from panel.extensions import db
from panel.models import (
    ServiceNotificationEvent,
    SmsScanDecision,
    SmsScanRun,
    SmsSendLog,
)

# The single sentence an operator needs when the numbers are ambiguous.
LEGACY_NOTE = ('Legacy send history - candidate manifest unavailable. These sends '
               'predate the candidate audit and do not prove there were no candidates.')


def _iso(value):
    try:
        return value.isoformat() if value is not None else None
    except Exception:
        return None


def _count(model):
    try:
        return int(db.session.query(func.count(model.id)).scalar() or 0)
    except Exception:
        db.session.rollback()
        return None


def _decisions_without_send_log():
    """Decisions recorded with no attempt behind them: deferred, suppressed, or a
    candidate the pipeline refused before submitting. This is the count that must
    be non-zero for a deferral to be visible at all."""
    try:
        return int(db.session.query(func.count(SmsScanDecision.id))
                   .filter(SmsScanDecision.sms_send_log_id.is_(None)).scalar() or 0)
    except Exception:
        db.session.rollback()
        return None


def _send_logs_without_decision():
    """Attempts with no candidate decision: the shape of the production defect
    where the pipeline that actually sends recorded no decisions."""
    try:
        linked = db.session.query(SmsScanDecision.sms_send_log_id).filter(
            SmsScanDecision.sms_send_log_id.isnot(None))
        return int(db.session.query(func.count(SmsSendLog.id))
                   .filter(~SmsSendLog.id.in_(linked)).scalar() or 0)
    except Exception:
        db.session.rollback()
        return None


def _latest_run():
    try:
        run = SmsScanRun.query.order_by(SmsScanRun.started_at.desc()).first()
    except Exception:
        db.session.rollback()
        return None
    if run is None:
        return None
    return {
        'run_id': run.run_id,
        'status': run.status,
        'triggered_by': run.triggered_by,
        'started_at': _iso(run.started_at),
        'finished_at': _iso(run.finished_at),
        'scanned_count': int(run.scanned_count or 0),
        'matched_count': int(run.matched_count or 0),
        'eligible_count': int(run.eligible_count or 0),
        'audit_gap_count': int(run.audit_gap_count or 0),
    }


def _dispositions():
    try:
        rows = db.session.query(SmsScanDecision.disposition,
                                func.count(SmsScanDecision.id)).group_by(
            SmsScanDecision.disposition).all()
        return {str(key or 'unknown'): int(value or 0) for key, value in rows}
    except Exception:
        db.session.rollback()
        return {}


def candidate_audit_snapshot() -> dict:
    """Counts, dispositions and the verdict. Read-only; never raises."""
    counts = {
        'scan_runs': _count(SmsScanRun),
        'scan_decisions': _count(SmsScanDecision),
        'send_logs': _count(SmsSendLog),
        'notification_events': _count(ServiceNotificationEvent),
    }
    decisions = counts.get('scan_decisions')
    send_logs = counts.get('send_logs')
    manifest_present = bool(decisions)
    legacy = bool(send_logs) and not manifest_present
    latest = _latest_run()
    return {
        'counts': counts,
        'dispositions': _dispositions(),
        'decisions_without_send_log': _decisions_without_send_log(),
        'send_logs_without_decision': _send_logs_without_decision(),
        'latest_run': latest,
        'candidate_manifest_present': manifest_present,
        'legacy_send_history_without_manifest': legacy,
        # eligible_count is NOT NULL DEFAULT 0 and no code path writes it, so a
        # finished run reporting 0 means "never measured", not "none eligible".
        'eligible_count_is_measured': (None if latest is None
                                       else latest['eligible_count'] > 0),
        'note': LEGACY_NOTE if legacy else None,
    }
