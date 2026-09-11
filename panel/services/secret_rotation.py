"""Durable, resumable rotation of stored secrets to the current key version.

Each task names a table, its primary key, the encrypted columns and the
cryptographic domain those columns belong to. The runner advances a cursor in
the system_migrations ledger after every batch, commits data and cursor
together, and is therefore idempotent and resumable. Values that cannot be
decrypted with any configured key are counted and skipped rather than aborting
the whole run. A task may also declare legacy domains: a value that the current
domain cannot read is decrypted with the legacy domain and re-encrypted with the
current one, which migrates rows written before a column's domain was settled.
"""
import json
from datetime import datetime

from sqlalchemy import text

from panel.extensions import db
from panel.models import SystemMigration
from panel.security import keyring

ROTATION_MIGRATION_ID = 'rotate_secrets_v2'

_MESSAGING_CONFIG_WHERE = "key IN ('whatsapp_gateway_api_key','sms_gmweb_api_key','sms_custom_api_key')"
_MESSAGING_SETTING_WHERE = ("key IN ('telegram_backup_bot_token','telegram_backup_proxy_url',"
                            "'telegram_backup_proxy_username','telegram_backup_proxy_password')")

# (table, primary key, encrypted columns, domain, optional WHERE, legacy domains).
# The legacy domains list is the compatibility path for values written before a
# column's domain was settled: they are decrypted with the legacy domain and
# re-encrypted with the current one instead of being skipped forever.
TASKS = (
    ('servers', 'id', ('password',), 'xui_credentials', None, ()),
    ('custom_subscriptions', 'id', ('token',), 'subscriptions', None, ()),
    ('custom_subscription_configs', 'id', ('uri',), 'subscriptions', None, ()),
    ('bank_cards', 'id', ('card_number', 'iban', 'account_number'), 'finance', None, ('generic',)),
    ('payments', 'id', ('sender_card',), 'finance', None, ('generic',)),
    ('transactions', 'id', ('sender_card',), 'finance', None, ('generic',)),
    ('backup_configs', 'id', ('config_url',), 'generic', None, ()),
    ('admin_mfa_settings', 'id', ('totp_secret',), 'mfa', None, ()),
    ('telegram_bot_instances', 'id', ('token_encrypted',), 'messaging', None, ()),
    ('telegram_proxy_endpoints', 'id', ('username_encrypted', 'password_encrypted'), 'messaging', None, ()),
    ('telegram_egress_profiles', 'id', ('config_encrypted',), 'messaging', None, ()),
    ('system_configs', 'key', ('value',), 'messaging', _MESSAGING_CONFIG_WHERE, ()),
    ('system_settings', 'key', ('value',), 'messaging', _MESSAGING_SETTING_WHERE, ()),
)


def _rotate_value(value, domain, legacy_domains=()):
    """Re-encrypt one stored ciphertext to the domain's current version.

    Returns (value, changed, resolved); resolved is False only when the value
    cannot be read with the domain key or any documented legacy domain.
    """
    try:
        updated, changed = keyring.rotate(value, domain)
        return updated, changed, True
    except Exception:
        pass
    for legacy in legacy_domains:
        try:
            plaintext = keyring.decrypt(value, legacy)
        except Exception:
            continue
        try:
            updated = keyring.encrypt(plaintext, domain)
        except Exception:
            return value, False, False
        return updated, updated != value, True
    return value, False, False


def rotation_needed() -> bool:
    """True when at least one domain can write a newer version than v1."""
    return any(keyring.current_version(domain) > 1 for domain in keyring.DOMAINS)


def rotation_status(create: bool = False) -> dict:
    record = SystemMigration.query.filter_by(migration_id=ROTATION_MIGRATION_ID).first()
    if record is None and create:
        record = SystemMigration(
            migration_id=ROTATION_MIGRATION_ID, status='pending', phase='rotate',
            cursor_json=json.dumps({'task': 0, 'after': None}),
        )
        db.session.add(record)
        db.session.commit()
    return {
        'migrationId': ROTATION_MIGRATION_ID,
        'required': bool(rotation_needed()) and (record is None or record.status != 'complete'),
        'status': record.status if record else 'pending',
        'phase': record.phase if record else 'rotate',
        'processedRows': int(record.processed_rows or 0) if record else 0,
        'lastError': record.last_error if record else None,
        'currentVersions': {domain: keyring.current_version(domain) for domain in keyring.DOMAINS},
    }


def run_rotation(batch_size: int = 200) -> dict:
    if not rotation_needed():
        return rotation_status()
    record = SystemMigration.query.filter_by(migration_id=ROTATION_MIGRATION_ID).first()
    if record is None:
        rotation_status(create=True)
        record = SystemMigration.query.filter_by(migration_id=ROTATION_MIGRATION_ID).first()
    if record.status == 'complete':
        return rotation_status()
    record.status = 'running'
    record.started_at = record.started_at or datetime.utcnow()
    db.session.commit()
    try:
        cursor = json.loads(record.cursor_json or '{}')
        task_index = int(cursor.get('task') or 0)
        after = cursor.get('after')
        skipped = int(cursor.get('skipped') or 0)
        while task_index < len(TASKS):
            table, primary_key, columns, domain, where_clause, legacy_domains = TASKS[task_index]
            predicates = []
            params = {'limit': max(1, min(int(batch_size), 1000))}
            if where_clause:
                predicates.append(where_clause)
            if after is not None:
                predicates.append(f'{primary_key} > :after')
                params['after'] = after
            where_sql = f" WHERE {' AND '.join(predicates)}" if predicates else ''
            selected = ', '.join((primary_key, *columns))
            rows = db.session.execute(text(
                f'SELECT {selected} FROM {table}{where_sql} '
                f'ORDER BY {primary_key} LIMIT :limit'
            ), params).mappings().all()
            if not rows:
                task_index += 1
                after = None
                record.cursor_json = json.dumps({'task': task_index, 'after': None, 'skipped': skipped})
                db.session.commit()
                continue
            for row in rows:
                updates = {}
                for column in columns:
                    value = row[column]
                    if value in (None, '') or not keyring.is_envelope(value):
                        continue
                    updated, changed, resolved = _rotate_value(value, domain, legacy_domains)
                    if not resolved:
                        skipped += 1
                        continue
                    if changed:
                        updates[column] = updated
                if updates:
                    assignments = ', '.join(f'{column} = :{column}' for column in updates)
                    db.session.execute(text(
                        f'UPDATE {table} SET {assignments} WHERE {primary_key} = :pk'
                    ), {**updates, 'pk': row[primary_key]})
                after = row[primary_key]
            record.processed_rows = int(record.processed_rows or 0) + len(rows)
            record.cursor_json = json.dumps({'task': task_index, 'after': after, 'skipped': skipped})
            record.updated_at = datetime.utcnow()
            db.session.commit()  # data and cursor advance atomically
        record.status = 'complete'
        record.phase = 'complete'
        record.finished_at = datetime.utcnow()
        record.updated_at = datetime.utcnow()
        db.session.commit()
        return rotation_status()
    except Exception as exc:
        db.session.rollback()
        record = SystemMigration.query.filter_by(migration_id=ROTATION_MIGRATION_ID).first()
        record.status = 'failed'
        record.last_error = str(exc)[:2000]
        record.updated_at = datetime.utcnow()
        db.session.commit()
        raise
