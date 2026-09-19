"""Measure the in-process snapshot's real footprint, and what inside a row costs what.

This is the measurement half of ``docs/performance/MEMORY.md``: the attribution payload
counts rows (``client_rows``, ``rows_with_raw_client``, ...) but counts are not bytes, and
the ranked optimization plan may not be acted on before the bytes exist.

How it measures, and why this way:

* The rows are built by the **production** builder, ``app.process_inbounds``, from
  synthetic 3x-ui inbound payloads - the same shape a panel returns. A hand-written row
  would measure the row I imagined, not the row the fetcher caches.
* The size is a **bounded deep size** (``sys.getsizeof`` plus the referents of containers,
  every object visited once). No new dependency (``pympler`` is not installed and was not
  added), and identical input gives identical output, so before/after numbers from this
  script are comparable.
* Attribution comes from **deleting a key and re-measuring**, not from summing the subtree
  in isolation. The isolated sum counts shared constants (interned literals such as
  ``'xtls-rprx-vision'``) once per row and therefore overstates what dropping the key
  would save; the deletion delta is what the change would actually save.
* The per-row cost is taken as a **slope**: the same shape is built at two fleet sizes and
  the difference divided by the difference in rows, so the fixed part of the snapshot
  (shared constants, inbound metadata) cancels out.
* JSON and gzip sizes are reported too, because the Redis snapshot is the compressed JSON
  and that is the copy that multiplies across processes.

It touches no network, no database and no Redis, and it never sleeps.

Usage::

    python scripts/measure_snapshot_footprint.py
    python scripts/measure_snapshot_footprint.py --servers 10 --inbounds 5 --clients 200
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
import os
import sys
import time
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The rows are built through the app's own builder, and starting the worker threads that
# an import would otherwise launch only adds noise to a footprint measurement.
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')


def deep_size(obj, _seen=None):
    """Bytes retained by ``obj``: its own size plus everything it reaches, once each."""
    if _seen is None:
        _seen = set()
    oid = id(obj)
    if oid in _seen:
        return 0
    _seen.add(oid)
    size = sys.getsizeof(obj, 0)
    if isinstance(obj, dict):
        for key, value in obj.items():
            size += deep_size(key, _seen) + deep_size(value, _seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            size += deep_size(item, _seen)
    return size


def _raw_client(index):
    return {
        'id': str(uuid.UUID(int=index)),
        'email': 'user%05d' % index,
        'enable': True,
        'expiryTime': 1790000000000 + (index % 90) * 86400000,
        'totalGB': (10 + (index % 40)) * 1024 ** 3,
        'limitIp': 0,
        'subId': 'sub%05d' % index,
        'flow': 'xtls-rprx-vision',
        'tgId': '' if index % 3 else str(100000000 + index),
        'reset': 0,
        'comment': 'customer %d' % index,
        'created_at': 1750000000000 + index * 1000,
        'updated_at': 1755000000000 + index * 1000,
    }


def build_inbounds(server_index, inbound_count, client_count, *, mirror):
    """Inbound payloads in the shape the panel returns.

    ``client_count`` accounts per server are spread over ``inbound_count`` inbounds. With
    ``mirror=False`` each account sits on exactly one inbound (a typical v2 install); with
    ``mirror=True`` every account sits on every inbound, which is the v3 arrangement the
    snapshot's ``duplication_ratio`` is about.
    """
    inbounds = []
    for inbound_index in range(inbound_count):
        clients = []
        stats = []
        for client_index in range(client_count):
            if not mirror and (client_index % inbound_count) != inbound_index:
                continue
            raw = _raw_client(server_index * 100000 + client_index)
            clients.append(raw)
            stats.append({
                'email': raw['email'],
                'up': 1_000_000 * (client_index + 1),
                'down': 4_000_000 * (client_index + 1),
                'enable': True,
                'total': 5_000_000 * (client_index + 1),
            })
        inbounds.append({
            'id': 100 + inbound_index,
            'remark': 'inbound-%d' % inbound_index,
            'protocol': 'vless',
            'port': 20000 + inbound_index,
            'enable': True,
            'settings': json.dumps({'clients': clients}, separators=(',', ':')),
            'clientStats': stats,
        })
    return inbounds


def _fake_server(server_id, *, v3):
    return SimpleNamespace(
        id=server_id,
        name='server-%d' % server_id,
        host='http://127.0.0.1:2053',
        panel_type='sanaei' if v3 else 'xui',
        api_token='token' if v3 else None,
        sub_port=None,
        subscription_port=None,
        sub_path=None,
        sub_json_path=None,
        sub_domain=None,
        subscription_domain=None,
        subscription_path=None,
        subscription_json_path=None,
        panel_lifecycle_automation=False,
        v3_clients=v3,
        is_active=True,
    )


def build_snapshot(*, servers, inbounds, clients, mirror, v3=True):
    """The snapshot as the fetcher caches it, built by the production builder."""
    import app  # noqa: E402  (deferred: importing starts the app core)

    from panel.adapters import xui
    from panel.services.xui_compat import COMPAT_CACHE, PanelCompatibility

    user = SimpleNamespace(id=1, role='superadmin', username='bench')
    far_future = time.time() + 3600

    all_inbounds = []
    with app.app.app_context():
        for server_index in range(servers):
            server = _fake_server(server_index + 1, v3=v3)
            # Pre-seed the caches so nothing probes the network (server_is_v3() would
            # otherwise treat an api_token as a hint, and a probe needs a session).
            xui.XUI_CAPABILITY_CACHE[int(server.id)] = {'expiry': far_future, 'v3_clients': v3}
            COMPAT_CACHE[int(server.id)] = {
                'expiry': far_future,
                'value': PanelCompatibility(server_id=server.id, detection_source='probe',
                                            confidence='high'),
            }
            payload = build_inbounds(server_index, inbounds, clients, mirror=mirror)
            processed, _stats = app.process_inbounds(payload, server, user)
            for inbound in processed:
                inbound['server_id'] = server.id
            all_inbounds.extend(processed)

    return {
        'inbounds': all_inbounds,
        'servers_status': [{'id': s + 1, 'active': True} for s in range(servers)],
        'last_update': 1755000000,
    }


def _sizes(snapshot):
    work = snapshot
    text = json.dumps(work, default=str, separators=(',', ':'))
    encoded = text.encode('utf-8')
    deep = deep_size(work)
    rows = sum(len(inbound.get('clients') or [])
               for inbound in (snapshot.get('inbounds') or []))
    return {
        'deep_bytes': deep,
        'json_bytes': len(encoded),
        'gzip_bytes': len(gzip.compress(encoded, 6)),
        'rows': rows,
    }


def _variant(snapshot, *, drop_raw=False, drop_formatted=False):
    """Measure a counterfactual representation by deleting keys, not by imagining it."""
    if not (drop_raw or drop_formatted):
        return _sizes(snapshot)
    work = copy.deepcopy(snapshot)
    for inbound in work.get('inbounds') or []:
        for client in inbound.get('clients') or []:
            if drop_raw:
                client.pop('raw_client', None)
            if drop_formatted:
                for key in [k for k in client if k.endswith('_formatted')]:
                    client.pop(key, None)
    return _sizes(work)


def measure(*, servers, inbounds, clients, mirror, v3=True):
    """Build the fleet through the production builder and return the attribution."""
    snapshot = build_snapshot(servers=servers, inbounds=inbounds, clients=clients,
                              mirror=mirror, v3=v3)
    full = _sizes(snapshot)

    seen_uids = set()
    isolated_row_bytes = 0
    isolated_raw_bytes = 0
    rows_with_raw = 0
    rows_with_formatted = 0
    formatted_keys = {}
    for inbound in snapshot['inbounds']:
        for client in inbound.get('clients') or []:
            seen_uids.add(str(client.get('id') or client.get('email')))
            isolated_row_bytes += deep_size(client)
            raw = client.get('raw_client')
            if isinstance(raw, dict):
                rows_with_raw += 1
                isolated_raw_bytes += deep_size(raw)
            for key, value in client.items():
                if key.endswith('_formatted') and isinstance(value, str):
                    formatted_keys[key] = formatted_keys.get(key, 0) + 1
                    rows_with_formatted += 1

    variants = {
        'full': full,
        'without_raw_client': _variant(snapshot, drop_raw=True),
        'without_formatted': _variant(snapshot, drop_formatted=True),
        'without_both': _variant(snapshot, drop_raw=True, drop_formatted=True),
    }
    rows = full['rows']

    return {
        'shape': {'servers': servers, 'inbounds_per_server': inbounds,
                  'accounts_per_server': clients, 'mirror': mirror, 'v3': v3},
        'totals': {
            'rows': rows,
            'unique_clients': len(seen_uids),
            'duplication_ratio': round(rows / len(seen_uids), 3) if seen_uids else None,
            'deep_bytes': full['deep_bytes'],
            'json_bytes': full['json_bytes'],
            'gzip_bytes': full['gzip_bytes'],
            'deep_mb': round(full['deep_bytes'] / 1024 / 1024, 3),
            'json_mb': round(full['json_bytes'] / 1024 / 1024, 3),
            'gzip_mb': round(full['gzip_bytes'] / 1024 / 1024, 3),
        },
        'per_row': {
            'bytes': round(full['deep_bytes'] / rows, 1) if rows else 0,
            # Isolated sizes: useful as "how heavy is one row", but they count shared
            # constants once per row, so they are not the saving a deletion would give.
            'isolated_bytes': round(isolated_row_bytes / rows, 1) if rows else 0,
            'isolated_raw_client_bytes': round(isolated_raw_bytes / rows, 1) if rows else 0,
            'rows_with_raw_client': rows_with_raw,
            'rows_with_formatted_strings': rows_with_formatted,
            'formatted_keys': formatted_keys,
        },
        'variants': variants,
    }


def marginal_bytes_per_row(*, servers, inbounds, clients, mirror):
    """Measured slope: the cost of one more row once the snapshot's fixed part cancels."""
    small = measure(servers=servers, inbounds=inbounds, clients=clients, mirror=mirror)
    large = measure(servers=servers, inbounds=inbounds, clients=clients * 2, mirror=mirror)
    rows_gained = large['totals']['rows'] - small['totals']['rows']
    if rows_gained <= 0:
        return None
    return (large['totals']['deep_bytes'] - small['totals']['deep_bytes']) / rows_gained


def _variant_line(name, variant, full):
    saved = full['deep_bytes'] - variant['deep_bytes']
    return ('    %-19s deep %8.3f MB  json %8.3f MB  gzip %7.3f MB   saves '
            '%5.1f%% deep, %5.1f%% json, %5.1f%% gzip'
            % (name, variant['deep_mb'] if 'deep_mb' in variant
               else variant['deep_bytes'] / 1024 / 1024,
               variant['json_bytes'] / 1024 / 1024, variant['gzip_bytes'] / 1024 / 1024,
               100.0 * saved / max(1, full['deep_bytes']),
               100.0 * (full['json_bytes'] - variant['json_bytes']) / max(1, full['json_bytes']),
               100.0 * (full['gzip_bytes'] - variant['gzip_bytes'])
               / max(1, full['gzip_bytes'])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--servers', type=int, default=3)
    parser.add_argument('--inbounds', type=int, default=4)
    parser.add_argument('--clients', type=int, default=50,
                        help='accounts per server (spread over the inbounds)')
    parser.add_argument('--json', action='store_true', help='print the result as JSON')
    args = parser.parse_args()

    shapes = [('each account on one inbound', False)]
    if args.inbounds > 1:
        shapes.append(('v3, every account on every inbound', True))

    results = []
    for label, mirror in shapes:
        result = measure(servers=args.servers, inbounds=args.inbounds,
                         clients=args.clients, mirror=mirror)
        result['marginal_bytes_per_row'] = marginal_bytes_per_row(
            servers=args.servers, inbounds=args.inbounds, clients=args.clients, mirror=mirror)
        results.append((label, result))

    if args.json:
        print(json.dumps({label: result for label, result in results}, indent=2))
        return 0

    for label, result in results:
        shape = result['shape']
        totals = result['totals']
        per_row = result['per_row']
        variants = result['variants']
        full = variants['full']
        print('=' * 80)
        print(label)
        print('  %d servers x %d inbounds, %d accounts/server -> %d rows, %d unique '
              '(ratio %s)'
              % (shape['servers'], shape['inbounds_per_server'],
                 shape['accounts_per_server'], totals['rows'], totals['unique_clients'],
                 totals['duplication_ratio']))
        print('  retained (deep size) : %8.3f MB   %7.1f bytes/row in this snapshot'
              % (totals['deep_mb'], per_row['bytes']))
        print('  json (what is stored) : %8.3f MB' % totals['json_mb'])
        print('  json gzipped         : %8.3f MB  (%.1f%% of json)'
              % (totals['gzip_mb'], 100.0 * totals['gzip_bytes'] / max(1, totals['json_bytes'])))
        print('  inside a row (isolated measurement; over-counts shared constants):')
        print('    one row            : %7.1f bytes'
              % per_row['isolated_bytes'])
        print('    its raw_client     : %7.1f bytes  (%d/%d rows carry one)'
              % (per_row['isolated_raw_client_bytes'],
                 per_row['rows_with_raw_client'], totals['rows']))
        print('    its *_formatted    : %d/%d rows'
              % (per_row['rows_with_formatted_strings'], totals['rows']))
        if per_row['formatted_keys']:
            keys = sorted(per_row['formatted_keys'].items(), key=lambda kv: -kv[1])
            print('      keys: ' + ', '.join('%s x%d' % (k, v) for k, v in keys))
        print('  attribution by deletion (what the change would actually save):')
        for name in ('without_raw_client', 'without_formatted', 'without_both'):
            print(_variant_line(name, variants[name], full))

    print('=' * 80)
    print('Measured marginal cost of one more row (two fleet sizes, fixed part cancels):')
    for label, result in results:
        marginal = result['marginal_bytes_per_row']
        print('  %-38s %7.1f bytes/row  -> %6.0f MB at 10k rows, %6.0f MB at 30k, '
              '%6.0f MB at 60k'
              % (label, marginal, marginal * 10000 / 1024 / 1024,
                 marginal * 30000 / 1024 / 1024, marginal * 60000 / 1024 / 1024))

    if len(results) == 2:
        unique_rows = results[0][1]['totals']['rows']
        mirror_rows = results[1][1]['totals']['rows']
        unique_bytes = results[0][1]['totals']['deep_bytes']
        mirror_bytes = results[1][1]['totals']['deep_bytes']
        print('=' * 80)
        print('The v3 mirror, measured on the same account fleet:')
        print('  %d unique rows -> %d mirrored rows (%.2fx); %.3f MB -> %.3f MB (%.2fx)'
              % (unique_rows, mirror_rows, mirror_rows / max(1, unique_rows),
                 unique_bytes / 1024 / 1024, mirror_bytes / 1024 / 1024,
                 mirror_bytes / max(1, unique_bytes)))
        print('  collapsing the mirror to one entity per account would remove %.3f MB of '
              'it, %.1f%% of the mirrored snapshot'
              % ((mirror_bytes - unique_bytes) / 1024 / 1024,
                 100.0 * (mirror_bytes - unique_bytes) / max(1, mirror_bytes)))
    print('=' * 80)
    print('Rows built by the production builder (app.process_inbounds) from synthetic '
          '3x-ui payloads; no network, no database, no Redis, no sleeps.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
