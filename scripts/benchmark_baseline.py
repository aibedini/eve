"""Repeatable performance baseline for Eve's hot request paths.

Phase 10 of the hardening program: measure before optimizing. The harness seeds a
deterministic synthetic database (its own SQLite file) plus an in-memory
dashboard snapshot, then times the request paths the later phases target:
/api/refresh for a superadmin (snapshot serialization) and for a reseller
(deepcopy + per-client filtering), the paginated and filtered finance lists, the
dashboard render and a couple of trivial authenticated calls. For every scenario
it records latency percentiles, response size and the number of SQL statements
executed while serving the request.

Nothing in this script changes application behaviour; it is a measurement tool.

Usage:
    python scripts/benchmark_baseline.py --out docs/performance/baseline.json
    python scripts/benchmark_baseline.py --compare docs/performance/baseline.json --fail-on-regression 10

Set EVE_BENCH_DATABASE_URL to reuse a specific database file (used by the test
suite so it never touches the shared test database).
"""
import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

DEFAULT_SIZES = {
    'servers': 12,
    'inbounds_per_server': 30,
    'clients_per_inbound': 50,
    'transactions': 5000,
    'payments': 3000,
    'bank_cards': 10,
    'ownerships': 4000,
    'packages': 30,
}

QUICK_SIZES = {
    'servers': 2,
    'inbounds_per_server': 3,
    'clients_per_inbound': 4,
    'transactions': 40,
    'payments': 20,
    'bank_cards': 2,
    'ownerships': 20,
    'packages': 3,
}

METRICS = ('mean_ms', 'p50_ms', 'p95_ms', 'min_ms', 'max_ms', 'response_bytes', 'sql_statements')

# Running this file directly puts scripts/ on sys.path, not the repository root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_APP = None
_NS = None
_SQL_COUNTER = None


def configure_environment(db_path):
    """Point the application at an isolated benchmark database."""
    url = 'sqlite:///' + str(db_path).replace(os.sep, '/')
    os.environ['DATABASE_URL'] = url
    os.environ['FLASK_ENV'] = 'development'
    os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
    os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
    os.environ.setdefault('SESSION_SECRET', 'benchmark-session-secret')
    return url


def load_app():
    """Import the Flask app and the models (after configure_environment)."""
    global _APP, _NS
    if _APP is not None:
        return _APP, _NS
    import app as app_module
    from panel.models import (
        Admin, BankCard, ClientOwnership, Package, Payment, Server, Transaction,
    )
    _APP = app_module.app
    _NS = {
        'app_module': app_module,
        'db': app_module.db,
        'Admin': Admin,
        'BankCard': BankCard,
        'ClientOwnership': ClientOwnership,
        'Package': Package,
        'Payment': Payment,
        'Server': Server,
        'Transaction': Transaction,
        'GLOBAL_SERVER_DATA': app_module.GLOBAL_SERVER_DATA,
        'version': getattr(app_module, 'APP_VERSION', 'unknown'),
    }
    return _APP, _NS


def _git_sha():
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=10,
        )
        return (result.stdout or '').strip()
    except Exception:
        return ''


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    index = min(len(ordered) - 1, max(0, index))
    return ordered[index]


def install_sql_counter(db):
    """Count executed statements for the current request."""
    from sqlalchemy import event
    counter = {'count': 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        counter['count'] += 1

    event.listen(db.engine, 'before_cursor_execute', _before)
    return counter


def seed(sizes, seed_value=1234):
    """Create a deterministic dataset and return the ids the scenarios need."""
    flask_app, ns = load_app()
    db = ns['db']
    rng = random.Random(seed_value)
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        now = datetime.utcnow()
        root = ns['Admin'](username='bench-root', role='superadmin', is_superadmin=True, enabled=True)
        root.set_password('BenchRootPassw0rd!')
        admin = ns['Admin'](username='bench-admin', role='admin', enabled=True)
        admin.set_password('BenchAdminPassw0rd!')
        reseller = ns['Admin'](username='bench-reseller', role='reseller', enabled=True,
                               allowed_servers='[]')
        reseller.set_password('BenchResellerPassw0rd!')
        db.session.add_all([root, admin, reseller])
        db.session.commit()

        servers = []
        for index in range(sizes['servers']):
            servers.append(ns['Server'](
                name='bench-%d' % index, host='https://bench-%d.invalid' % index,
                username='u', password='p', panel_type='auto', enabled=True,
            ))
        db.session.add_all(servers)
        db.session.commit()

        cards = []
        for index in range(sizes['bank_cards']):
            cards.append(ns['BankCard'](
                label='card-%d' % index, card_number='60379975%08d' % index,
                is_active=True,
            ))
        db.session.add_all(cards)
        db.session.commit()

        transactions = []
        for index in range(sizes['transactions']):
            amount = rng.randint(1000, 900000)
            transactions.append(ns['Transaction'](
                admin_id=admin.id,
                amount=amount if index % 2 else -amount,
                category='income' if index % 2 else 'expense',
                type='purchase' if index % 3 else 'server_cost',
                server_id=servers[index % len(servers)].id if index % 5 else None,
                client_email='client%d@example.test' % (index % 200),
                description='Bench transaction %d - client%d@example.test' % (index, index % 200),
                card_id=cards[index % len(cards)].id,
                sender_card='61043378%08d' % index,
                created_at=now - timedelta(minutes=index),
            ))
        db.session.add_all(transactions)

        payments = []
        for index in range(sizes['payments']):
            payments.append(ns['Payment'](
                admin_id=admin.id,
                amount=rng.randint(1000, 500000),
                payment_date=now - timedelta(minutes=index),
                sender_card='60379975%08d' % index,
                sender_name='Payer %d' % index,
                client_email='client%d@example.test' % (index % 200),
                card_id=cards[index % len(cards)].id,
            ))
        db.session.add_all(payments)

        ownerships = []
        for index in range(sizes['ownerships']):
            ownerships.append(ns['ClientOwnership'](
                reseller_id=reseller.id,
                server_id=servers[index % len(servers)].id,
                inbound_id=(index % sizes['inbounds_per_server']) + 1,
                client_email='client%d@example.test' % index,
                client_uuid='uuid-%d' % index,
            ))
        db.session.add_all(ownerships)

        packages = []
        for index in range(sizes['packages']):
            packages.append(ns['Package'](
                name='package-%d' % index, days=30, volume=100 * (2 ** 30), price=100000,
            ))
        db.session.add_all(packages)
        db.session.commit()

        return {
            'root': root.id,
            'admin': admin.id,
            'reseller': reseller.id,
            'servers': [server.id for server in servers],
            'cards': [card.id for card in cards],
        }


def build_snapshot(sizes, ids, seed_value=1234):
    """Fill GLOBAL_SERVER_DATA with a realistic enriched snapshot."""
    _flask_app, ns = load_app()
    rng = random.Random(seed_value + 1)
    snapshot = ns['GLOBAL_SERVER_DATA']
    now_ms = int(time.time() * 1000)
    inbounds = []
    servers_status = []
    for server_index, server_id in enumerate(ids['servers']):
        # The canonical status shape uses server_id (see refresh._update_reachability_status).
        servers_status.append({
            'server_id': server_id,
            'name': 'bench-%d' % server_index,
            'success': True,
            'panel_type': 'auto',
            'reachable': True,
            'online_count': 0,
            'inbound_count': sizes['inbounds_per_server'],
            'client_count': sizes['inbounds_per_server'] * sizes['clients_per_inbound'],
        })
        for inbound_index in range(1, sizes['inbounds_per_server'] + 1):
            clients = []
            for client_index in range(sizes['clients_per_inbound']):
                serial = (inbound_index * 1000) + client_index
                total = 0 if serial % 7 == 0 else 100 * (2 ** 30)
                clients.append({
                    'server_id': server_id,
                    'inbound_id': inbound_index,
                    'email': 'client%d@example.test' % serial,
                    'id': 'uuid-%d' % serial,
                    'up': rng.randint(0, 50 * (2 ** 30)),
                    'down': rng.randint(0, 200 * (2 ** 30)),
                    'totalGB': total,
                    'expiryTimestamp': now_ms + rng.randint(-10, 90) * 86400000,
                    'enable': bool(serial % 5),
                    'is_online': bool(serial % 3 == 0),
                    'expiryType': 'start_after_use' if serial % 11 == 0 else 'fixed',
                    'totalGB_formatted': 'Unlimited' if total == 0 else '100 GB',
                    'raw_client': {
                        'id': 'uuid-%d' % serial,
                        'email': 'client%d@example.test' % serial,
                        'enable': bool(serial % 5),
                        'expiryTime': now_ms + rng.randint(-10, 90) * 86400000,
                        'totalGB': total,
                        'subId': 'sub%d' % serial,
                        'limitIp': 0,
                        'flow': '',
                        'tgId': '',
                        'reset': 0,
                    },
                })
            inbounds.append({
                'server_id': server_id,
                'id': inbound_index,
                'tag': 'in-%d-%d' % (server_index, inbound_index),
                'remark': 'bench %d/%d' % (server_index, inbound_index),
                'protocol': 'vless',
                'port': 10000 + inbound_index,
                'enable': True,
                'client_count': len(clients),
                'total_up': '1.0 GB',
                'total_down': '2.0 GB',
                'clients': clients,
            })
    snapshot.update({
        'last_update': datetime.utcnow().isoformat(),
        'inbounds': inbounds,
        'stats': {
            'total_inbounds': len(inbounds),
            'active_inbounds': len(inbounds),
            'total_clients': sum(item['client_count'] for item in inbounds),
        },
        'servers_status': servers_status,
        'is_updating': False,
    })
    snapshot.pop('_enriched_key', None)
    return len(inbounds)


def _client_for(flask_app, admin_id, role, is_superadmin):
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess.clear()
        sess['admin_id'] = admin_id
        sess['role'] = role
        sess['is_superadmin'] = bool(is_superadmin)
    return client


def build_scenarios(sizes, ids):
    flask_app, _ns = load_app()
    root = _client_for(flask_app, ids['root'], 'superadmin', True)
    admin = _client_for(flask_app, ids['admin'], 'admin', False)
    reseller = _client_for(flask_app, ids['reseller'], 'reseller', False)
    anonymous = flask_app.test_client()
    return [
        ('html_login', 'public login page render',
         lambda: anonymous.get('/login')),
        ('html_dashboard', 'dashboard render with a populated snapshot',
         lambda: root.get('/')),
        ('api_permissions', 'trivial authenticated API',
         lambda: admin.get('/api/me/permissions')),
        ('api_refresh_superadmin', 'serialize the shared snapshot',
         lambda: root.get('/api/refresh')),
        ('api_refresh_reseller', 'deepcopy + per-client filter for a reseller',
         lambda: reseller.get('/api/refresh')),
        ('api_transactions_page', 'paginated transaction list',
         lambda: admin.get('/api/transactions?limit=20')),
        ('api_transactions_search', 'filtered transaction list',
         lambda: admin.get('/api/transactions?limit=20&search=client7')),
        ('api_payments_page', 'payments + transactions page',
         lambda: admin.get('/api/payments?limit=20')),
        ('api_finance_stats', 'finance aggregates',
         lambda: admin.get('/api/finance/stats')),
        ('api_bank_cards', 'bank card list',
         lambda: admin.get('/api/bank-cards')),
    ]


def run_scenarios(scenarios, repeat, warmup):
    counter = _SQL_COUNTER
    results = {}
    for name, description, call in scenarios:
        for _ in range(max(0, warmup)):
            call()
        samples = []
        sizes = []
        statements = []
        status = None
        for _ in range(max(1, repeat)):
            if counter is not None:
                counter['count'] = 0
            started = time.perf_counter()
            response = call()
            elapsed = (time.perf_counter() - started) * 1000.0
            body = response.get_data()
            samples.append(elapsed)
            sizes.append(len(body))
            statements.append(counter['count'] if counter is not None else 0)
            status = response.status_code
        results[name] = {
            'description': description,
            'status': status,
            'repeat': repeat,
            'mean_ms': round(statistics.fmean(samples), 3),
            'p50_ms': round(_percentile(samples, 50), 3),
            'p95_ms': round(_percentile(samples, 95), 3),
            'min_ms': round(min(samples), 3),
            'max_ms': round(max(samples), 3),
            'response_bytes': max(sizes),
            'sql_statements': max(statements),
        }
    return results


def run_harness(sizes, repeat=7, warmup=2, seed_value=1234):
    global _SQL_COUNTER
    flask_app, ns = load_app()
    ids = seed(sizes, seed_value)
    build_snapshot(sizes, ids, seed_value)
    with flask_app.app_context():
        _SQL_COUNTER = install_sql_counter(ns['db'])
    scenarios = build_scenarios(sizes, ids)
    results = run_scenarios(scenarios, repeat, warmup)
    return {
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'app_version': ns['version'],
        'git_sha': _git_sha(),
        'python': platform.python_version(),
        'platform': platform.platform(),
        'repeat': repeat,
        'warmup': warmup,
        'sizes': dict(sizes),
        'scenarios': results,
    }


def compare_reports(baseline, current, tolerance_pct=10.0):
    rows = []
    regressions = []
    improvements = []
    base_scenarios = baseline.get('scenarios') or {}
    for name, cur in (current.get('scenarios') or {}).items():
        base = base_scenarios.get(name)
        if not base:
            rows.append({'scenario': name, 'status': 'new'})
            continue
        row = {'scenario': name, 'status': 'compared'}
        for metric in ('mean_ms', 'p95_ms', 'response_bytes', 'sql_statements'):
            base_value = base.get(metric) or 0
            cur_value = cur.get(metric) or 0
            delta = ((cur_value - base_value) / base_value) * 100.0 if base_value else 0.0
            row[metric] = round(cur_value, 3)
            row[metric + '_delta_pct'] = round(delta, 1)
        rows.append(row)
        for metric in ('mean_ms', 'p95_ms'):
            if row[metric + '_delta_pct'] > tolerance_pct:
                regressions.append({
                    'scenario': name, 'metric': metric,
                    'delta_pct': row[metric + '_delta_pct'],
                })
        if (row['mean_ms_delta_pct'] < -tolerance_pct
                or row['p95_ms_delta_pct'] < -tolerance_pct):
            improvements.append({
                'scenario': name,
                'mean_delta_pct': row['mean_ms_delta_pct'],
                'p95_delta_pct': row['p95_ms_delta_pct'],
            })
    return {
        'tolerance_pct': tolerance_pct,
        'rows': rows,
        'regressions': regressions,
        'improvements': improvements,
    }


def format_report_lines(report, compare=None):
    lines = []
    lines.append('Eve performance baseline  version=%s  commit=%s'
                 % (report['app_version'], report['git_sha'] or 'unknown'))
    lines.append('python=%s  platform=%s' % (report['python'], report['platform']))
    lines.append('repeat=%s  warmup=%s  dataset: %s'
                 % (report['repeat'], report['warmup'],
                    ', '.join('%s=%s' % item for item in sorted(report['sizes'].items()))))
    lines.append('')
    header = '%-26s %6s %9s %9s %9s %10s %5s' % (
        'scenario', 'status', 'mean_ms', 'p50_ms', 'p95_ms', 'bytes', 'sql')
    lines.append(header)
    lines.append('-' * len(header))
    for name, row in report['scenarios'].items():
        lines.append('%-26s %6s %9.3f %9.3f %9.3f %10d %5d' % (
            name, row['status'], row['mean_ms'], row['p50_ms'], row['p95_ms'],
            row['response_bytes'], row['sql_statements']))
    if compare:
        lines.append('')
        lines.append('compare (tolerance %s%%):' % compare['tolerance_pct'])
        for row in compare['rows']:
            if row.get('status') == 'new':
                lines.append('  %-26s new scenario' % row['scenario'])
                continue
            lines.append('  %-26s mean %+7.1f%%  p95 %+7.1f%%  bytes %+7.1f%%  sql %+7.1f%%' % (
                row['scenario'], row['mean_ms_delta_pct'], row['p95_ms_delta_pct'],
                row['response_bytes_delta_pct'], row['sql_statements_delta_pct']))
        if compare['regressions']:
            lines.append('  REGRESSIONS: ' + ', '.join(
                '%s.%s %+.1f%%' % (item['scenario'], item['metric'], item['delta_pct'])
                for item in compare['regressions']))
        if compare['improvements']:
            lines.append('  IMPROVEMENTS: ' + ', '.join(
                '%s mean %+.1f%%' % (item['scenario'], item['mean_delta_pct'])
                for item in compare['improvements']))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description='Eve performance baseline harness')
    parser.add_argument('--out', default=None, help='where to write the JSON report')
    parser.add_argument('--compare', default=None, help='baseline JSON to compare against')
    parser.add_argument('--fail-on-regression', dest='fail_on_regression', type=float,
                        default=None, help='exit non-zero when mean/p95 worsen by more than this %%')
    parser.add_argument('--repeat', type=int, default=7)
    parser.add_argument('--warmup', type=int, default=2)
    parser.add_argument('--seed', type=int, default=1234)
    parser.add_argument('--quick', action='store_true', help='tiny dataset for smoke tests')
    parser.add_argument('--db', default=None, help='database file to use')
    parser.add_argument('--keep-db', action='store_true')
    args = parser.parse_args(argv)

    sizes = dict(QUICK_SIZES if args.quick else DEFAULT_SIZES)
    owned_db = args.db or os.environ.get('EVE_BENCH_DATABASE_URL')
    if not owned_db:
        owned_db = os.path.join(tempfile.gettempdir(), 'eve-bench-%d.db' % os.getpid())
    if os.path.exists(owned_db):
        try:
            os.remove(owned_db)
        except OSError:
            pass
    configure_environment(owned_db)

    report = run_harness(sizes, repeat=args.repeat, warmup=args.warmup, seed_value=args.seed)
    compare = None
    if args.compare:
        with open(args.compare, encoding='utf-8') as handle:
            baseline = json.load(handle)
        compare = compare_reports(
            baseline, report,
            tolerance_pct=(args.fail_on_regression if args.fail_on_regression is not None else 10.0),
        )
        report['compare'] = compare

    out_path = args.out or os.path.join(
        'docs', 'performance', 'baseline-%s.json' % report['app_version'])
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write(os.linesep)

    print(*format_report_lines(report, compare), sep=os.linesep)
    print('report written to %s' % out_path)

    if not args.keep_db and not args.db and not os.environ.get('EVE_BENCH_DATABASE_URL'):
        try:
            os.remove(owned_db)
        except OSError:
            pass

    if compare and compare['regressions'] and args.fail_on_regression is not None:
        print('REGRESSION: %d metric(s) above %.1f%%'
              % (len(compare['regressions']), compare['tolerance_pct']))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
