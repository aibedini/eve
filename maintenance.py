"""Durable, non-interactive maintenance runner used by systemd and the eve CLI."""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

os.environ['DISABLE_BACKGROUND_THREADS'] = 'true'
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'  # maintenance owns the migration run

from app import (  # noqa: E402
    APP_VERSION,
    app,
    db,
    get_usage_migration_status,
    _legacy_usage_table_name,
    _migrate_legacy_usage_snapshots,
)
from panel.migrate import run_migrations  # noqa: E402
from panel.models import SystemMigration  # noqa: E402
from panel.security import (  # noqa: E402
    encrypt_secret,
    decrypt_secret,
    hash_bearer_token,
    is_encrypted,
    is_hashed_bearer_token,
)
from sqlalchemy import text  # noqa: E402


_SECRET_MIGRATION_ID = 'encrypt_sensitive_values_v1'
_SECRET_TASKS = (
    ('system_configs', 'key', ('value',), "key IN ('whatsapp_gateway_api_key','sms_gmweb_api_key','sms_custom_api_key')"),
    ('system_settings', 'key', ('value',), "key IN ('telegram_backup_bot_token','telegram_backup_proxy_url','telegram_backup_proxy_username','telegram_backup_proxy_password')"),
    ('bank_cards', 'id', ('card_number', 'iban', 'account_number'), None),
    ('payments', 'id', ('sender_card',), None),
    ('transactions', 'id', ('sender_card',), None),
    ('backup_configs', 'id', ('config_url',), None),
)
_TOKEN_MIGRATION_ID = 'hash_agent_tokens_v1'
_TOKEN_TASKS = (
    ('pulse_agents', 'pulse-agent'),
    ('bnqo_agents', 'bnqo-agent'),
    ('bnqo_enroll_tokens', 'bnqo-enroll'),
)
_CUSTOM_SECRET_MIGRATION_ID = 'protect_custom_subscription_credentials_v1'
_CUSTOM_SECRET_TASKS = (
    ('custom_subscriptions', 'custom-subscription'),
    ('custom_subscription_configs', 'custom-subscription-config'),
)


def _secret_migration_status(create=False):
    record = SystemMigration.query.filter_by(migration_id=_SECRET_MIGRATION_ID).first()
    if record is None and create:
        record = SystemMigration(
            migration_id=_SECRET_MIGRATION_ID, status='pending', phase='encrypt',
            cursor_json=json.dumps({'task': 0, 'after': None}),
        )
        db.session.add(record)
        db.session.commit()
    return {
        'migrationId': _SECRET_MIGRATION_ID,
        'required': not record or record.status != 'complete',
        'status': record.status if record else 'pending',
        'phase': record.phase if record else 'encrypt',
        'processedRows': int(record.processed_rows or 0) if record else 0,
        'lastError': record.last_error if record else None,
    }


def _run_secret_migration(batch_size=200):
    record = SystemMigration.query.filter_by(migration_id=_SECRET_MIGRATION_ID).first()
    if record is None:
        _secret_migration_status(create=True)
        record = SystemMigration.query.filter_by(migration_id=_SECRET_MIGRATION_ID).first()
    if record.status == 'complete':
        return _secret_migration_status()
    record.status = 'running'
    record.started_at = record.started_at or datetime.utcnow()
    db.session.commit()
    try:
        cursor = json.loads(record.cursor_json or '{}')
        task_index = int(cursor.get('task') or 0)
        after = cursor.get('after')
        while task_index < len(_SECRET_TASKS):
            table, primary_key, columns, where_clause = _SECRET_TASKS[task_index]
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
                record.cursor_json = json.dumps({'task': task_index, 'after': None})
                db.session.commit()
                continue
            for row in rows:
                updates = {}
                for column in columns:
                    value = row[column]
                    if value not in (None, '') and not is_encrypted(value):
                        updates[column] = encrypt_secret(value)
                if updates:
                    assignments = ', '.join(f'{column} = :{column}' for column in updates)
                    db.session.execute(text(
                        f'UPDATE {table} SET {assignments} WHERE {primary_key} = :pk'
                    ), {**updates, 'pk': row[primary_key]})
                after = row[primary_key]
            record.processed_rows = int(record.processed_rows or 0) + len(rows)
            record.cursor_json = json.dumps({'task': task_index, 'after': after})
            record.updated_at = datetime.utcnow()
            db.session.commit()  # data and cursor advance atomically
        record.status = 'complete'
        record.phase = 'complete'
        record.finished_at = datetime.utcnow()
        record.updated_at = datetime.utcnow()
        db.session.commit()
        return _secret_migration_status()
    except Exception as exc:
        db.session.rollback()
        record = SystemMigration.query.filter_by(migration_id=_SECRET_MIGRATION_ID).first()
        record.status = 'failed'
        record.last_error = str(exc)[:2000]
        record.updated_at = datetime.utcnow()
        db.session.commit()
        raise


def _token_migration_status(create=False):
    record = SystemMigration.query.filter_by(migration_id=_TOKEN_MIGRATION_ID).first()
    if record is None and create:
        record = SystemMigration(
            migration_id=_TOKEN_MIGRATION_ID, status='pending', phase='hash',
            cursor_json=json.dumps({'task': 0, 'after': None}),
        )
        db.session.add(record)
        db.session.commit()
    return {
        'migrationId': _TOKEN_MIGRATION_ID,
        'required': not record or record.status != 'complete',
        'status': record.status if record else 'pending',
        'phase': record.phase if record else 'hash',
        'processedRows': int(record.processed_rows or 0) if record else 0,
        'lastError': record.last_error if record else None,
    }


def _run_token_migration(batch_size=200):
    record = SystemMigration.query.filter_by(migration_id=_TOKEN_MIGRATION_ID).first()
    if record is None:
        _token_migration_status(create=True)
        record = SystemMigration.query.filter_by(migration_id=_TOKEN_MIGRATION_ID).first()
    if record.status == 'complete':
        return _token_migration_status()
    record.status = 'running'
    record.started_at = record.started_at or datetime.utcnow()
    db.session.commit()
    try:
        cursor = json.loads(record.cursor_json or '{}')
        task_index = int(cursor.get('task') or 0)
        after = cursor.get('after')
        while task_index < len(_TOKEN_TASKS):
            table, purpose = _TOKEN_TASKS[task_index]
            params = {'limit': max(1, min(int(batch_size), 1000))}
            where_sql = ''
            if after is not None:
                where_sql = ' WHERE id > :after'
                params['after'] = after
            rows = db.session.execute(text(
                f'SELECT id, token FROM {table}{where_sql} ORDER BY id LIMIT :limit'
            ), params).mappings().all()
            if not rows:
                task_index += 1
                after = None
                record.cursor_json = json.dumps({'task': task_index, 'after': None})
                db.session.commit()
                continue
            for row in rows:
                if row['token'] and not is_hashed_bearer_token(row['token']):
                    db.session.execute(text(
                        f'UPDATE {table} SET token = :token WHERE id = :id'
                    ), {'token': hash_bearer_token(row['token'], purpose), 'id': row['id']})
                after = row['id']
            record.processed_rows = int(record.processed_rows or 0) + len(rows)
            record.cursor_json = json.dumps({'task': task_index, 'after': after})
            record.updated_at = datetime.utcnow()
            db.session.commit()
        record.status = 'complete'
        record.phase = 'complete'
        record.finished_at = datetime.utcnow()
        record.updated_at = datetime.utcnow()
        db.session.commit()
        return _token_migration_status()
    except Exception as exc:
        db.session.rollback()
        record = SystemMigration.query.filter_by(migration_id=_TOKEN_MIGRATION_ID).first()
        record.status = 'failed'
        record.last_error = str(exc)[:2000]
        record.updated_at = datetime.utcnow()
        db.session.commit()
        raise


def _custom_secret_migration_status(create=False):
    record = SystemMigration.query.filter_by(
        migration_id=_CUSTOM_SECRET_MIGRATION_ID
    ).first()
    if record is None and create:
        record = SystemMigration(
            migration_id=_CUSTOM_SECRET_MIGRATION_ID, status='pending', phase='protect',
            cursor_json=json.dumps({'task': 0, 'after': None}),
        )
        db.session.add(record)
        db.session.commit()
    return {
        'migrationId': _CUSTOM_SECRET_MIGRATION_ID,
        'required': not record or record.status != 'complete',
        'status': record.status if record else 'pending',
        'phase': record.phase if record else 'protect',
        'processedRows': int(record.processed_rows or 0) if record else 0,
        'lastError': record.last_error if record else None,
    }


def _run_custom_secret_migration(batch_size=200):
    record = SystemMigration.query.filter_by(
        migration_id=_CUSTOM_SECRET_MIGRATION_ID
    ).first()
    if record is None:
        _custom_secret_migration_status(create=True)
        record = SystemMigration.query.filter_by(
            migration_id=_CUSTOM_SECRET_MIGRATION_ID
        ).first()
    if record.status == 'complete':
        return _custom_secret_migration_status()
    record.status = 'running'
    record.started_at = record.started_at or datetime.utcnow()
    db.session.commit()
    try:
        cursor = json.loads(record.cursor_json or '{}')
        task_index = int(cursor.get('task') or 0)
        after = cursor.get('after')
        while task_index < len(_CUSTOM_SECRET_TASKS):
            table, purpose = _CUSTOM_SECRET_TASKS[task_index]
            params = {'limit': max(1, min(int(batch_size), 1000))}
            where_sql = ''
            if after is not None:
                where_sql = ' WHERE id > :after'
                params['after'] = after
            rows = db.session.execute(text(
                f'SELECT id, {"token" if table == "custom_subscriptions" else "uri"} AS secret_value '
                f'FROM {table}{where_sql} ORDER BY id LIMIT :limit'
            ), params).mappings().all()
            if not rows:
                task_index += 1
                after = None
                record.cursor_json = json.dumps({'task': task_index, 'after': None})
                db.session.commit()
                continue
            value_column = 'token' if table == 'custom_subscriptions' else 'uri'
            hash_column = 'token_hash' if table == 'custom_subscriptions' else 'uri_hash'
            for row in rows:
                stored = row['secret_value'] or ''
                raw = decrypt_secret(stored) if is_encrypted(stored) else stored
                db.session.execute(text(
                    f'UPDATE {table} SET {value_column} = :protected, '
                    f'{hash_column} = :lookup_hash WHERE id = :id'
                ), {
                    'protected': encrypt_secret(raw),
                    'lookup_hash': hash_bearer_token(raw, purpose),
                    'id': row['id'],
                })
                after = row['id']
            record.processed_rows = int(record.processed_rows or 0) + len(rows)
            record.cursor_json = json.dumps({'task': task_index, 'after': after})
            record.updated_at = datetime.utcnow()
            db.session.commit()
        record.status = 'complete'
        record.phase = 'complete'
        record.finished_at = datetime.utcnow()
        record.updated_at = datetime.utcnow()
        db.session.commit()
        return _custom_secret_migration_status()
    except Exception as exc:
        db.session.rollback()
        record = SystemMigration.query.filter_by(
            migration_id=_CUSTOM_SECRET_MIGRATION_ID
        ).first()
        record.status = 'failed'
        record.last_error = str(exc)[:2000]
        record.updated_at = datetime.utcnow()
        db.session.commit()
        raise


def _source_size_bytes(table_name):
    if not table_name:
        return 0
    try:
        if db.engine.dialect.name == 'postgresql':
            return int(db.session.execute(
                text('SELECT pg_total_relation_size(to_regclass(:table_name))'),
                {'table_name': table_name},
            ).scalar() or 0)
        path = db.engine.url.database
        return os.path.getsize(path) if path and os.path.exists(path) else 0
    except Exception:
        db.session.rollback()
        return None


def maintenance_plan():
    status = get_usage_migration_status()
    secret_status = _secret_migration_status()
    token_status = _token_migration_status()
    custom_secret_status = _custom_secret_migration_status()
    status['required'] = bool(
        status['required'] or secret_status['required'] or token_status['required']
        or custom_secret_status['required']
    )
    disk = shutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
    status.update({
        'appVersion': APP_VERSION,
        'sourceSizeBytes': _source_size_bytes(_legacy_usage_table_name()),
        'diskFreeBytes': disk.free,
        'diskTotalBytes': disk.total,
        'mayTakeTime': bool(status['required']),
        'panelMayBeSlower': bool(status['required']),
        'secretEncryption': secret_status,
        'agentTokenHashing': token_status,
        'customSubscriptionProtection': custom_secret_status,
    })
    return status


def main():
    parser = argparse.ArgumentParser(description='Eve maintenance migration runner')
    parser.add_argument('command', choices=('plan', 'status', 'run'), nargs='?', default='run')
    parser.add_argument('--batch-accounts', type=int, default=10)
    parser.add_argument(
        '--skip-schema-migrations',
        action='store_true',
        help='Skip schema setup when the updater already completed it.',
    )
    args = parser.parse_args()

    with app.app_context():
        if not args.skip_schema_migrations:
            run_migrations()
        if args.command in ('plan', 'status'):
            print(json.dumps(maintenance_plan(), ensure_ascii=False, indent=2))
            return 0

        plan = maintenance_plan()
        print(json.dumps({'event': 'maintenance-start', **plan}, ensure_ascii=False))
        if plan['required']:
            print('[Maintenance] Data compaction may take time. The panel can be slower during this process.')
        usage_result = _migrate_legacy_usage_snapshots(
            finalize=True,
            batch_accounts=max(1, min(args.batch_accounts, 1000)),
        )
        secret_result = _run_secret_migration(batch_size=max(20, min(args.batch_accounts * 20, 1000)))
        token_result = _run_token_migration(batch_size=max(20, min(args.batch_accounts * 20, 1000)))
        custom_secret_result = _run_custom_secret_migration(
            batch_size=max(20, min(args.batch_accounts * 20, 1000))
        )
        print(json.dumps({
            'event': 'maintenance-complete',
            'usage': usage_result,
            'secretEncryption': secret_result,
            'agentTokenHashing': token_result,
            'customSubscriptionProtection': custom_secret_result,
        }, ensure_ascii=False))
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'[Maintenance] failed: {exc}', file=sys.stderr)
        raise
