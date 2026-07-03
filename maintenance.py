"""Durable, non-interactive maintenance runner used by systemd and the eve CLI."""
import argparse
import json
import os
import shutil
import sys

os.environ['DISABLE_BACKGROUND_THREADS'] = 'true'

from app import (  # noqa: E402
    APP_VERSION,
    app,
    db,
    get_usage_migration_status,
    _legacy_usage_table_name,
    _migrate_legacy_usage_snapshots,
)
from sqlalchemy import text  # noqa: E402


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
    disk = shutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
    status.update({
        'appVersion': APP_VERSION,
        'sourceSizeBytes': _source_size_bytes(_legacy_usage_table_name()),
        'diskFreeBytes': disk.free,
        'diskTotalBytes': disk.total,
        'mayTakeTime': bool(status['required']),
        'panelMayBeSlower': bool(status['required']),
    })
    return status


def main():
    parser = argparse.ArgumentParser(description='Eve maintenance migration runner')
    parser.add_argument('command', choices=('plan', 'status', 'run'), nargs='?', default='run')
    parser.add_argument('--batch-accounts', type=int, default=10)
    args = parser.parse_args()

    with app.app_context():
        db.create_all()
        if args.command in ('plan', 'status'):
            print(json.dumps(maintenance_plan(), ensure_ascii=False, indent=2))
            return 0

        plan = maintenance_plan()
        print(json.dumps({'event': 'maintenance-start', **plan}, ensure_ascii=False))
        if plan['required']:
            print('[Maintenance] Data compaction may take time. The panel can be slower during this process.')
        result = _migrate_legacy_usage_snapshots(
            finalize=True,
            batch_accounts=max(1, min(args.batch_accounts, 1000)),
        )
        print(json.dumps({'event': 'maintenance-complete', **result}, ensure_ascii=False))
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'[Maintenance] failed: {exc}', file=sys.stderr)
        raise
