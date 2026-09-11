"""Reproducible measurement of the per-server snapshot cache.

Phase 13 measurement tool: it builds a synthetic set of server blocks, publishes
them into an in-memory Redis stand-in, and compares

* a forced full load (what a per-server read-modify-write used to do: decode every
  server block), with
* a targeted load (decode only the server being written).

No Redis, no database and no app import: it exercises panel.core.redis_client
directly, so the numbers can be reproduced anywhere.

Usage:
    python scripts/benchmark_cache.py --quick
    python scripts/benchmark_cache.py --json docs/performance/per-server-cache.json
"""
import argparse
import json
import os
import statistics
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

QUICK_SIZES = {'servers': 2, 'inbounds': 3, 'clients': 4, 'rounds': 2, 'targeted_rounds': 4}
DEFAULT_SIZES = {'servers': 12, 'inbounds': 30, 'clients': 50, 'rounds': 5,
                 'targeted_rounds': 20}


class FakeRedis:
    """Only the read path is needed to measure a load."""

    def __init__(self, values):
        self.values = values

    def get(self, key):
        return self.values.get(key)


def synthetic_blocks(servers=12, inbounds=30, clients=50):
    """Build servers x inbounds x clients rows shaped like the real snapshot."""
    now_ms = int(time.time() * 1000)
    blocks = {}
    for server_id in range(1, servers + 1):
        rows = []
        for inbound_id in range(1, inbounds + 1):
            client_rows = []
            for index in range(clients):
                serial = server_id * 100000 + inbound_id * 1000 + index
                client_rows.append({
                    'server_id': server_id,
                    'inbound_id': inbound_id,
                    'email': 'client%d@example.test' % serial,
                    'id': 'uuid-%d' % serial,
                    'up': index * 1024,
                    'down': index * 4096,
                    'totalGB': 100 * (2 ** 30),
                    'expiryTimestamp': now_ms + index * 86400000,
                    'enable': bool(index % 3),
                    'is_online': bool(index % 2),
                    'expiryType': 'fixed',
                    'totalGB_formatted': '100 GB',
                    'raw_client': {'id': 'uuid-%d' % serial,
                                   'email': 'client%d@example.test' % serial,
                                   'enable': bool(index % 3), 'totalGB': 100 * (2 ** 30)},
                })
            rows.append({'server_id': server_id, 'id': inbound_id, 'protocol': 'vless',
                         'remark': 'in %d/%d' % (server_id, inbound_id), 'clients': client_rows})
        blocks[server_id] = rows
    return blocks


def build_fixture(blocks):
    from panel.core import redis_client
    values = {
        redis_client.REDIS_SNAPSHOT_VERSION_KEY: b'v2',
        redis_client.REDIS_SNAPSHOT_MANIFEST_KEY: redis_client._encode_snapshot({
            'format': 2,
            'version': 'v2',
            'server_versions': {sid: 'v1' for sid in blocks},
            'stats': {},
            'servers_status': [{'server_id': sid} for sid in sorted(blocks)],
            'last_update': 'fixture',
        }),
    }
    for sid, rows in blocks.items():
        values[redis_client._redis_server_snapshot_key(sid)] = \
            redis_client._encode_snapshot(rows)
    return FakeRedis(values)


def _timed(call, rounds):
    values = []
    for _ in range(rounds):
        started = time.perf_counter()
        call()
        values.append((time.perf_counter() - started) * 1000.0)
    return values


def measure(blocks, *, rounds=5, targeted_rounds=20):
    from panel.core import redis_client
    client = build_fixture(blocks)
    snapshot = {'inbounds': [row for sid in sorted(blocks) for row in blocks[sid]],
                'servers_status': [{'server_id': sid} for sid in sorted(blocks)],
                'stats': {}, 'last_update': 'local'}
    original_get_redis = redis_client.get_redis
    original_data = dict(redis_client.GLOBAL_SERVER_DATA)
    original_versions = dict(redis_client._LAST_LOADED_SERVER_VERSIONS)
    original_version = redis_client._LAST_LOADED_SNAPSHOT_VERSION
    redis_client.get_redis = lambda: client
    redis_client.GLOBAL_SERVER_DATA.clear()
    redis_client.GLOBAL_SERVER_DATA.update(snapshot)
    redis_client._LAST_LOADED_SERVER_VERSIONS = {}
    redis_client._LAST_LOADED_SNAPSHOT_VERSION = None
    target = sorted(blocks)[0]
    try:
        redis_client.reset_snapshot_metrics()
        full_ms = _timed(lambda: redis_client._load_snapshot_from_redis_unlocked(force=True),
                         rounds)
        full_metrics = redis_client.snapshot_metrics()
        redis_client.reset_snapshot_metrics()
        targeted_ms = _timed(
            lambda: redis_client._load_snapshot_from_redis_unlocked(
                force=True, server_ids=[target]),
            targeted_rounds)
        targeted_metrics = redis_client.snapshot_metrics()
    finally:
        redis_client.get_redis = original_get_redis
        redis_client.GLOBAL_SERVER_DATA.clear()
        redis_client.GLOBAL_SERVER_DATA.update(original_data)
        redis_client._LAST_LOADED_SERVER_VERSIONS = original_versions
        redis_client._LAST_LOADED_SNAPSHOT_VERSION = original_version
        redis_client.reset_snapshot_metrics()
    return {
        'servers': len(blocks),
        'inbounds_per_server': len(blocks[target]),
        'clients_per_inbound': len(blocks[target][0]['clients']),
        'full_ms': round(statistics.fmean(full_ms), 2),
        'targeted_ms': round(statistics.fmean(targeted_ms), 2),
        'speedup': round(statistics.fmean(full_ms) / max(0.001, statistics.fmean(targeted_ms)), 1),
        # Per-call averages: the counters accumulate over the timing rounds.
        'full_blocks_decoded': int(full_metrics['blocks_decoded'] / max(1, rounds)),
        'targeted_blocks_decoded': int(
            targeted_metrics['blocks_decoded'] / max(1, targeted_rounds)),
        'full_bytes': int(full_metrics['bytes_decoded'] / max(1, rounds)),
        'targeted_bytes': int(targeted_metrics['bytes_decoded'] / max(1, targeted_rounds)),
        'traffic_reduction': round(
            (full_metrics['bytes_decoded'] / max(1, rounds))
            / max(1.0, targeted_metrics['bytes_decoded'] / max(1, targeted_rounds)), 1),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='Per-server snapshot cache measurement')
    parser.add_argument('--quick', action='store_true', help='tiny fixture')
    parser.add_argument('--json', default=None, help='write the result as JSON')
    parser.add_argument('--rounds', type=int, default=None)
    parser.add_argument('--targeted-rounds', dest='targeted_rounds', type=int, default=None)
    args = parser.parse_args(argv)
    sizes = dict(QUICK_SIZES if args.quick else DEFAULT_SIZES)
    if args.rounds:
        sizes['rounds'] = args.rounds
    if args.targeted_rounds:
        sizes['targeted_rounds'] = args.targeted_rounds
    blocks = synthetic_blocks(sizes['servers'], sizes['inbounds'], sizes['clients'])
    result = measure(blocks, rounds=sizes['rounds'],
                     targeted_rounds=sizes['targeted_rounds'])
    print('servers=%d inbounds/server=%d clients/inbound=%d' % (
        result['servers'], result['inbounds_per_server'], result['clients_per_inbound']))
    print('forced full load : %8.2f ms  blocks_decoded=%d  bytes=%d' % (
        result['full_ms'], result['full_blocks_decoded'], result['full_bytes']))
    print('targeted load    : %8.2f ms  blocks_decoded=%d  bytes=%d' % (
        result['targeted_ms'], result['targeted_blocks_decoded'], result['targeted_bytes']))
    print('speedup=%.1fx  traffic reduction=%.1fx' % (
        result['speedup'], result['traffic_reduction']))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
