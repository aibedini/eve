"""Bounded, resumable data retention (phase 30).

Operational log tables grow without limit unless something removes old rows, and
unbounded growth ends in a full disk and an outage. This module owns that: a
small registry of policies, each deleting rows older than its window in bounded
batches, with the progress kept in the durable system_migrations ledger so a run
that hits its batch ceiling resumes where it stopped. An operator can preview
what would be deleted and disable any policy.

Rules:

* a policy is driven by a system setting retention_days_<name>; 0 disables it and
  the value is clamped to 1..3650;
* rows newer than the cutoff are never touched;
* each batch deletes rows and updates the ledger cursor in the same transaction,
  so an interrupted run is safe to repeat (idempotent);
* AuditLog is deliberately not in the registry: the audit trail is evidence and
  must be exported or archived, not silently deleted.

CLI:

    python -m panel.services.retention --dry-run
    python -m panel.services.retention --only health_logs
"""
import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES = 20
MAX_RETENTION_DAYS = 3650
ENABLED_SETTING = "retention_enabled"
LEDGER_PREFIX = "retention:"


@dataclass(frozen=True)
class Policy:
    name: str
    model_name: str
    timestamp_column: str
    default_days: int
    description: str = ""
    terminal_only: bool = False


POLICIES = (
    Policy("health_logs", "HealthLog", "timestamp", 90,
           "health watchdog and auto-heal log"),
    Policy("monitor_message_log", "MonitorMessageLog", "sent_at", 90,
           "per-recipient depletion message log (dedup window)"),
    Policy("whatsapp_bot_log", "WhatsappBotLog", "sent_at", 30,
           "WhatsApp depletion send log"),
    Policy("sms_send_log", "SmsSendLog", "created_at", 180,
           "SMS send history shown by the operator UI"),
    Policy("bnqo_jobs", "BnqoJob", "created_at", 30,
           "delivered BNQO agent jobs", terminal_only=True),
    Policy("admin_sessions", "AdminSession", "expires_at", 30,
           "expired or revoked browser sessions"),
)


def policies():
    return list(POLICIES)


def policy_by_name(name):
    for policy in POLICIES:
        if policy.name == name:
            return policy
    return None


def _setting(key, default=None):
    try:
        from panel.extensions import db
        from panel.models import SystemSetting
        row = db.session.get(SystemSetting, key)
        return row.value if row is not None else default
    except Exception:
        return default


def retention_enabled() -> bool:
    raw = str(_setting(ENABLED_SETTING, "true") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def retention_days(policy) -> int:
    raw = _setting("retention_days_%s" % policy.name)
    try:
        days = int(str(raw).strip()) if raw not in (None, "") else policy.default_days
    except (TypeError, ValueError):
        days = policy.default_days
    return max(0, min(days, MAX_RETENTION_DAYS))


def _model(policy):
    from panel import models
    return getattr(models, policy.model_name)


def _filters(policy, model, cutoff):
    column = getattr(model, policy.timestamp_column)
    clauses = [column < cutoff]
    if policy.terminal_only and hasattr(model, "status"):
        clauses.append(model.status != "pending")
    if policy.name == "admin_sessions":
        column = model.expires_at
        clauses = [column < cutoff]
    return clauses


def _ledger(name, create=False):
    from panel.extensions import db
    from panel.models import SystemMigration
    migration_id = LEDGER_PREFIX + name
    record = SystemMigration.query.filter_by(migration_id=migration_id).first()
    if record is None and create:
        record = SystemMigration(migration_id=migration_id, status="pending",
                                 phase="pending")
        db.session.add(record)
        db.session.flush()
    return record


def preview(name=None) -> dict:
    """Count the rows each policy would delete right now (no writes)."""
    from panel.extensions import db
    result = {}
    if not retention_enabled():
        return {"enabled": False, "policies": {}}
    for policy in POLICIES:
        if name and policy.name != name:
            continue
        days = retention_days(policy)
        if days <= 0:
            result[policy.name] = {"days": 0, "eligible": 0, "disabled": True}
            continue
        cutoff = datetime.utcnow() - timedelta(days=days)
        try:
            model = _model(policy)
            eligible = int(model.query.filter(*_filters(policy, model, cutoff)).count())
            result[policy.name] = {"days": days, "eligible": eligible,
                                   "cutoff": cutoff.isoformat() + "Z"}
        except Exception as exc:
            db.session.rollback()
            result[policy.name] = {"days": days, "error": str(exc)[:200]}
    return {"enabled": True, "policies": result}


def run(name=None, *, dry_run=False, batch_size=DEFAULT_BATCH_SIZE,
        max_batches=DEFAULT_MAX_BATCHES) -> dict:
    """Delete expired rows for one or all policies, in bounded batches."""
    from panel.extensions import db
    batch_size = max(1, min(int(batch_size or DEFAULT_BATCH_SIZE), 5000))
    max_batches = max(1, int(max_batches or DEFAULT_MAX_BATCHES))
    outcome = {"dry_run": bool(dry_run), "batch_size": batch_size,
               "max_batches": max_batches, "enabled": retention_enabled(),
               "policies": {}}
    if not outcome["enabled"]:
        outcome["reason"] = "retention_disabled"
        return outcome
    for policy in POLICIES:
        if name and policy.name != name:
            continue
        days = retention_days(policy)
        if days <= 0:
            outcome["policies"][policy.name] = {"deleted": 0, "batches": 0,
                                                "disabled": True, "days": 0}
            continue
        cutoff = datetime.utcnow() - timedelta(days=days)
        entry = {"deleted": 0, "batches": 0, "days": days,
                 "cutoff": cutoff.isoformat() + "Z", "exhausted": False}
        try:
            model = _model(policy)
            if dry_run:
                entry["eligible"] = int(
                    model.query.filter(*_filters(policy, model, cutoff)).count())
                outcome["policies"][policy.name] = entry
                continue
            record = _ledger(policy.name, create=True)
            record.status = "running"
            record.phase = "deleting"
            db.session.flush()
            while entry["batches"] < max_batches:
                rows = (model.query.filter(*_filters(policy, model, cutoff))
                        .order_by(model.id.asc()).limit(batch_size).all())
                ids = [row.id for row in rows]
                if not ids:
                    entry["exhausted"] = True
                    break
                model.query.filter(model.id.in_(ids)).delete(synchronize_session=False)
                entry["deleted"] += len(ids)
                entry["batches"] += 1
                entry["last_id"] = ids[-1]
                record.cursor_json = json.dumps(
                    {"last_id": ids[-1], "deleted_last_run": entry["deleted"]},
                    sort_keys=True)
                record.processed_rows = int(record.processed_rows or 0) + len(ids)
                record.updated_at = datetime.utcnow()
                db.session.commit()
            record.status = "done" if entry["exhausted"] else "pending"
            record.phase = "idle" if entry["exhausted"] else "deleting"
            record.finished_at = datetime.utcnow() if entry["exhausted"] else None
            record.last_error = None
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            entry["error"] = str(exc)[:200]
            try:
                record = _ledger(policy.name, create=True)
                record.status = "error"
                record.last_error = str(exc)[:200]
                record.updated_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()
        outcome["policies"][policy.name] = entry
    outcome["deleted"] = sum(item.get("deleted", 0)
                             for item in outcome["policies"].values())
    return outcome


def status() -> dict:
    """Last recorded run per policy, from the migration ledger."""
    from panel.extensions import db
    from panel.models import SystemMigration
    result = {"enabled": retention_enabled(), "policies": {}}
    for policy in POLICIES:
        days = retention_days(policy)
        entry = {"days": days, "default_days": policy.default_days}
        try:
            record = SystemMigration.query.filter_by(
                migration_id=LEDGER_PREFIX + policy.name).first()
            if record is not None:
                entry.update({
                    "status": record.status,
                    "phase": record.phase,
                    "processed_rows": record.processed_rows,
                    "updated_at": record.updated_at.isoformat() + "Z"
                    if record.updated_at else None,
                    "last_error": record.last_error,
                })
        except Exception as exc:
            db.session.rollback()
            entry["error"] = str(exc)[:200]
        result["policies"][policy.name] = entry
    return result


def _main(argv=None):
    parser = argparse.ArgumentParser(description="Eve data retention runner")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--only", default=None, help="one policy name")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from app import app  # noqa: E402  deferred: the CLI needs the app context
    with app.app_context():
        if args.status:
            payload = status()
        elif args.dry_run:
            payload = preview(args.only)
        else:
            payload = run(args.only, dry_run=False, batch_size=args.batch_size,
                          max_batches=args.max_batches)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
