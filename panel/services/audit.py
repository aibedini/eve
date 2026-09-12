"""Tamper-evident audit trail (phase 29).

Every sensitive action already appends an AuditLog row through app._log_audit.
This module makes the trail verifiable: each row carries a SHA-256 digest of its
own content plus the previous row hash, so an UPDATE or a DELETE breaks the chain
and verify_chain() reports where and why. Rows written before the chain existed
have NULL hashes and are reported as legacy.

record() never raises and never commits: the row travels inside the transaction
that the caller is about to commit, exactly like the previous helper.
"""
import hashlib
import json
import threading
from datetime import datetime

GENESIS_HASH = "0" * 64
_CHAIN_LOCK = threading.Lock()


def _canonical(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str).encode("utf-8")


def _created_iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def entry_digest(*, prev_hash, actor_type, actor_admin_id, action, target_type=None,
                 target_id=None, meta_json=None, request_id=None, source_ip=None,
                 user_agent=None, created_at=None) -> str:
    """SHA-256 over the canonical row content (the verifier recomputes it)."""
    payload = {
        "actor_type": actor_type,
        "actor_admin_id": actor_admin_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "meta_json": meta_json,
        "request_id": request_id,
        "source_ip": source_ip,
        "user_agent": user_agent,
        "created_at": _created_iso(created_at),
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def request_context() -> dict:
    """request_id / source_ip / user_agent from the active request, if any."""
    context = {"request_id": None, "source_ip": None, "user_agent": None}
    try:
        from flask import g, has_request_context, request
        if not has_request_context():
            return context
        context["request_id"] = getattr(g, "request_id", None)
        context["user_agent"] = (request.headers.get("User-Agent") or "")[:200] or None
        try:
            from panel.security import client_ip
            context["source_ip"] = client_ip()
        except Exception:
            pass
    except Exception:
        pass
    return context


def _resolve_actor(actor):
    actor_type = "system"
    actor_admin_id = None
    try:
        from panel.models import Admin
        if isinstance(actor, Admin):
            actor_type, actor_admin_id = "admin", actor.id
        elif isinstance(actor, str) and actor in ("admin", "system", "customer"):
            actor_type = actor
    except Exception:
        pass
    return actor_type, actor_admin_id


def _resolve_target(target):
    target_type = None
    target_id = None
    if isinstance(target, tuple) and len(target) == 2:
        target_type, target_id = target
    elif target is not None:
        target_type = target.__class__.__name__
        target_id = getattr(target, "id", None)
    if target_type is not None:
        target_type = str(target_type)[:32]
    if target_id in (None, ""):
        target_id = None
    else:
        target_id = str(target_id)[:64]
    return target_type, target_id


def tip_hash() -> str:
    """The newest entry hash, or the genesis hash when the chain is empty."""
    try:
        from panel.models import AuditLog
        row = (AuditLog.query
               .filter(AuditLog.entry_hash.isnot(None))
               .order_by(AuditLog.id.desc())
               .first())
        return (row.entry_hash if row is not None else None) or GENESIS_HASH
    except Exception:
        return GENESIS_HASH


def record(action, target=None, actor=None, meta=None) -> None:
    """Append one chained audit row. Best effort: never raises, never commits."""
    try:
        from panel.extensions import db
        from panel.models import AuditLog

        actor_type, actor_admin_id = _resolve_actor(actor)
        target_type, target_id = _resolve_target(target)
        meta_json = json.dumps(meta, ensure_ascii=False, default=str) if meta else None
        context = request_context()
        with _CHAIN_LOCK:
            previous = tip_hash()
            created_at = datetime.utcnow()
            row = AuditLog(
                actor_type=actor_type,
                actor_admin_id=actor_admin_id,
                action=str(action)[:64],
                target_type=target_type,
                target_id=target_id,
                meta_json=meta_json,
                request_id=(context.get("request_id") or None),
                source_ip=context.get("source_ip"),
                user_agent=context.get("user_agent"),
                prev_hash=previous,
                created_at=created_at,
            )
            row.entry_hash = entry_digest(
                prev_hash=previous,
                actor_type=actor_type,
                actor_admin_id=actor_admin_id,
                action=row.action,
                target_type=target_type,
                target_id=target_id,
                meta_json=meta_json,
                request_id=row.request_id,
                source_ip=row.source_ip,
                user_agent=row.user_agent,
                created_at=created_at,
            )
            db.session.add(row)
    except Exception:
        pass


def verify_chain(limit=None) -> dict:
    """Recompute the chain oldest-first and report the first break."""
    result = {"ok": True, "checked": 0, "legacy": 0, "broken_at": None,
              "reason": None, "tip": GENESIS_HASH}
    try:
        from panel.models import AuditLog
        query = AuditLog.query.order_by(AuditLog.id.asc())
        if limit:
            query = query.limit(max(1, int(limit)))
        previous = GENESIS_HASH
        for row in query:
            if not row.entry_hash:
                result["legacy"] += 1
                continue
            expected = entry_digest(
                prev_hash=row.prev_hash or GENESIS_HASH,
                actor_type=row.actor_type,
                actor_admin_id=row.actor_admin_id,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                meta_json=row.meta_json,
                request_id=row.request_id,
                source_ip=row.source_ip,
                user_agent=row.user_agent,
                created_at=row.created_at,
            )
            if (row.prev_hash or GENESIS_HASH) != previous:
                result.update(ok=False, broken_at=row.id, reason="chain_link")
                return result
            if expected != row.entry_hash:
                result.update(ok=False, broken_at=row.id, reason="content_mismatch")
                return result
            previous = row.entry_hash
            result["checked"] += 1
        result["tip"] = previous
    except Exception as exc:
        result.update(ok=False, reason="verify_failed", error=str(exc)[:200])
    return result


def row_to_dict(row) -> dict:
    """Serialise one audit row for the read API."""
    try:
        meta = json.loads(row.meta_json) if row.meta_json else None
    except Exception:
        meta = None
    return {
        "id": row.id,
        "action": row.action,
        "actor_type": row.actor_type,
        "actor_admin_id": row.actor_admin_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "meta": meta,
        "request_id": row.request_id,
        "source_ip": row.source_ip,
        "user_agent": row.user_agent,
        "created_at": _created_iso(row.created_at),
        "prev_hash": row.prev_hash,
        "entry_hash": row.entry_hash,
    }
