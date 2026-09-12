"""Optional Redis shared cache + in-memory server-data snapshot state.

When REDIS_URL is set AND reachable, one worker fetches panel data and writes
the processed snapshot to Redis; all workers read it from there. If Redis is
missing/unreachable, the app transparently falls back to per-worker fetching.
"""
import logging
import json
import os
import secrets
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

logger = logging.getLogger(__name__)

__all__ = [
    'GLOBAL_SERVER_DATA',
    'GLOBAL_REFRESH_LOCK',
    'GLOBAL_FETCH_LOCK',
    'fetch_guard',
    'REDIS_URL',
    'REDIS_SNAPSHOT_KEY',
    'REDIS_SNAPSHOT_MANIFEST_KEY',
    'REDIS_SERVER_SNAPSHOT_PREFIX',
    'REDIS_SNAPSHOT_VERSION_KEY',
    'REDIS_SNAPSHOT_TTL',
    'REDIS_SERVER_REVISION_PREFIX',
    'REDIS_REFRESH_QUEUE_KEY',
    'REDIS_REFRESH_PROCESSING_KEY',
    'REDIS_REFRESH_JOB_PREFIX',
    'REDIS_REFRESH_SCOPE_PREFIX',
    'REDIS_REFRESH_JOB_TTL',
    'get_redis',
    'redis_enabled',
    'publish_snapshot_to_redis',
    'load_snapshot_from_redis',
    'get_server_revision',
    'bump_server_revision',
    'snapshot_metrics',
    'reset_snapshot_metrics',
    'serialized_server_snapshot_write',
]

# کش برای نگهداری وضعیت سرورها در RAM
# این دیتا با هر بار ریستارت برنامه پاک می‌شود (امنیت بالا)
GLOBAL_SERVER_DATA = {
    'last_update': None,
    'inbounds': [],
    'stats': {},
    'servers_status': [],
    'is_updating': False
}

# Serializes all writes to GLOBAL_SERVER_DATA (fetch pipeline, ownership
# enrichment, snapshot publish). Moved here from app.py; identity is shared.
GLOBAL_REFRESH_LOCK = threading.RLock()

# Serializes panel fetches (one fan-out at a time) WITHOUT being held while the
# network I/O runs: readers take GLOBAL_REFRESH_LOCK only for the short in-memory
# commits. See docs/performance/REFRESH_LOCK.md.
GLOBAL_FETCH_LOCK = threading.Lock()

REDIS_URL = (os.environ.get('REDIS_URL') or '').strip()
REDIS_SNAPSHOT_KEY = 'eve:server_data_snapshot'
REDIS_SNAPSHOT_MANIFEST_KEY = 'eve:server_data_manifest'
REDIS_SERVER_SNAPSHOT_PREFIX = 'eve:server_data:'
REDIS_SERVER_REVISION_PREFIX = 'eve:server_revision:'
_REDIS_CLIENT = None
_REDIS_CHECKED = False
_REDIS_RETRY_AFTER = 0.0
_REDIS_LOCK = threading.Lock()


def get_redis():
    """Return a connected redis client, or None if unavailable.
    Result is cached; a failed connection disables Redis for the process."""
    global _REDIS_CLIENT, _REDIS_CHECKED, _REDIS_RETRY_AFTER
    if _REDIS_CHECKED:
        return _REDIS_CLIENT
    if _REDIS_CLIENT is None and time.monotonic() < _REDIS_RETRY_AFTER:
        return None
    with _REDIS_LOCK:
        if _REDIS_CHECKED:
            return _REDIS_CLIENT
        if _REDIS_CLIENT is None and time.monotonic() < _REDIS_RETRY_AFTER:
            return None
        _REDIS_CHECKED = True
        if not REDIS_URL:
            _REDIS_CLIENT = None
            return None
        try:
            import redis as _redis_lib
            client = _redis_lib.from_url(
                REDIS_URL, socket_connect_timeout=2, socket_timeout=2,
                decode_responses=False)
            client.ping()
            _REDIS_CLIENT = client
            logger.info("Redis connected: %s", REDIS_URL)
        except Exception as _re:
            logger.warning("Redis unavailable (%s); using per-worker in-memory cache.", _re)
            _REDIS_CLIENT = None
            _REDIS_CHECKED = False
            _REDIS_RETRY_AFTER = time.monotonic() + 10.0
        return _REDIS_CLIENT


def redis_enabled() -> bool:
    return get_redis() is not None


@contextmanager
def fetch_guard(wait_seconds: float = 0.0):
    """Yield True when this caller owns the panel-fetch slot, False otherwise.

    The lock is released as soon as the caller leaves the block, so it must only
    cover a fetch and never surround a reader.
    """
    acquired = GLOBAL_FETCH_LOCK.acquire(
        blocking=bool(wait_seconds), timeout=wait_seconds if wait_seconds else -1)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                GLOBAL_FETCH_LOCK.release()
            except Exception:
                pass


REDIS_SNAPSHOT_VERSION_KEY = 'eve:server_data_version'
REDIS_SNAPSHOT_TTL = 600  # survives panel backoff while still expiring stale cache
REDIS_REFRESH_QUEUE_KEY = 'eve:refresh:queue'
REDIS_REFRESH_PROCESSING_KEY = 'eve:refresh:processing'
REDIS_REFRESH_JOB_PREFIX = 'eve:refresh:job:'
REDIS_REFRESH_SCOPE_PREFIX = 'eve:refresh:scope:'
REDIS_REFRESH_JOB_TTL = 900
_LAST_LOADED_SNAPSHOT_VERSION = None
_LAST_LOADED_SERVER_VERSIONS = {}
_PUBLISHED_SERVER_VERSIONS = {}
_LOCAL_SERVER_WRITE_LOCKS = defaultdict(threading.RLock)

# Per-server cache traffic counters (diagnostics + performance comparisons).
_SNAPSHOT_METRICS = {
    'publishes': 0,
    'blocks_encoded': 0,
    'blocks_decoded': 0,
    'bytes_encoded': 0,
    'bytes_decoded': 0,
    'full_loads': 0,
    'targeted_loads': 0,
}


def snapshot_metrics() -> dict:
    """Snapshot cache counters for the current process."""
    return dict(_SNAPSHOT_METRICS)


def reset_snapshot_metrics() -> None:
    for key in _SNAPSHOT_METRICS:
        _SNAPSHOT_METRICS[key] = 0


def _encode_snapshot(value) -> bytes:
    """Serialize cache data without executable object deserialization."""
    import zlib
    payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return zlib.compress(payload, 1)


def _decode_snapshot(blob: bytes):
    import zlib
    return json.loads(zlib.decompress(blob).decode('utf-8'))


@contextmanager
def serialized_server_snapshot_write(server_id: int, *, wait_seconds: float = 5.0,
                                     lease_seconds: int = 30):
    """Serialize one server's read-modify-publish cache cycle across workers.

    The caller receives ``GLOBAL_REFRESH_LOCK`` while its local snapshot has
    already been refreshed from Redis.  This prevents two workers that changed
    different clients on the same server from publishing stale whole-server
    blocks over one another.
    """
    sid = int(server_id)
    client = get_redis()
    if client is None:
        with _LOCAL_SERVER_WRITE_LOCKS[sid]:
            with GLOBAL_REFRESH_LOCK:
                yield
        return

    key = f'eve:server_snapshot_write:{sid}'
    token = secrets.token_urlsafe(24)
    deadline = time.monotonic() + max(0.1, float(wait_seconds))
    acquired = False
    while time.monotonic() < deadline:
        try:
            acquired = bool(client.set(key, token, nx=True, ex=max(5, int(lease_seconds))))
        except Exception:
            acquired = False
            break
        if acquired:
            break
        time.sleep(0.05)
    if not acquired:
        raise TimeoutError(f'timed out waiting for server snapshot write lock: {sid}')

    stop = threading.Event()

    def _heartbeat():
        interval = max(2, int(lease_seconds) // 3)
        while not stop.wait(interval):
            try:
                renewed = client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                    1, key, token, max(5, int(lease_seconds)),
                )
                if not renewed:
                    break
            except Exception:
                break

    heartbeat = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat.start()
    try:
        with GLOBAL_REFRESH_LOCK:
            # Only this server's block has to be fresh: a full forced load would
            # decompress every other server's block on every client mutation
            # (measured 441 ms vs 30 ms at 12 servers, see docs/performance/PER_SERVER_CACHE.md).
            _load_snapshot_from_redis_unlocked(force=True, server_ids=[sid])
            yield
    finally:
        stop.set()
        try:
            client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1, key, token,
            )
        except Exception:
            pass


def _redis_server_snapshot_key(server_id: int) -> str:
    return f'{REDIS_SERVER_SNAPSHOT_PREFIX}{int(server_id)}'


def _redis_server_revision_key(server_id: int) -> str:
    return f'{REDIS_SERVER_REVISION_PREFIX}{int(server_id)}'


# Per-process fallback revision. Without Redis there is no shared snapshot either, so a
# mutation only has to invalidate *this* process's in-flight refresh -- and the guard
# used to be a no-op there (both sides read 0), which let a refresh that started before
# an edit overwrite the edit.
_LOCAL_SERVER_REVISIONS = defaultdict(int)


def get_server_revision(server_id: int) -> int:
    """Return the shared mutation revision for one server (local without Redis)."""
    client = get_redis()
    if client is None:
        return int(_LOCAL_SERVER_REVISIONS.get(int(server_id), 0))
    try:
        raw = client.get(_redis_server_revision_key(server_id))
        return int(raw or 0)
    except Exception:
        return 0


def bump_server_revision(server_id: int) -> int:
    """Mark an authoritative panel/cache mutation for stale-refresh detection."""
    client = get_redis()
    if client is None:
        sid = int(server_id)
        _LOCAL_SERVER_REVISIONS[sid] = int(_LOCAL_SERVER_REVISIONS.get(sid, 0)) + 1
        return _LOCAL_SERVER_REVISIONS[sid]
    try:
        key = _redis_server_revision_key(server_id)
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, REDIS_SNAPSHOT_TTL)
        result = pipe.execute()
        return int(result[0] or 0)
    except Exception as exc:
        logger.warning("Redis server revision bump failed for %s: %s", server_id, exc)
        return 0


def _format_bytes(value) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        size = 0
    units = ('B', 'KB', 'MB', 'GB', 'TB', 'PB')
    amount = float(size)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == 'B':
                return f'{int(amount)} B'
            return f'{amount:.2f} {unit}'
        amount /= 1024.0
    return '0 B'


def _aggregate_stats(server_statuses):
    """Build manifest-wide totals from the merged per-server status rows."""
    keys = (
        'total_inbounds', 'active_inbounds', 'total_clients', 'online_clients',
        'active_clients', 'inactive_clients', 'not_started_clients',
        'unlimited_expiry_clients', 'unlimited_volume_clients', 'upload_raw',
        'download_raw', 'remaining_raw', 'limited_clients',
    )
    total = {key: 0 for key in keys}
    for status in server_statuses or []:
        stats = status.get('stats') if isinstance(status, dict) and status.get('success') else None
        if not isinstance(stats, dict):
            continue
        for key in keys:
            value = stats.get(key, 0)
            if isinstance(value, int):
                total[key] += value
    total['total_upload'] = _format_bytes(total['upload_raw'])
    total['total_download'] = _format_bytes(total['download_raw'])
    total['total_traffic'] = _format_bytes(total['upload_raw'] + total['download_raw'])
    total['total_remaining'] = _format_bytes(total['remaining_raw'])
    return total


def publish_snapshot_to_redis(changed_server_ids=None, *, expected_server_revisions=None) -> bool:
    """Publish a small manifest plus independently compressed server blocks.

    ``changed_server_ids`` limits expensive serialization to servers replaced by
    the latest fetch. ``None`` performs a full publish for write-through callers;
    an empty iterable updates only manifest metadata.
    """
    global _PUBLISHED_SERVER_VERSIONS
    client = get_redis()
    if client is None:
        return False
    try:
        publish_all = changed_server_ids is None
        changed = set()
        if not publish_all:
            for sid in changed_server_ids:
                try:
                    changed.add(int(sid))
                except Exception:
                    continue

        blocks = defaultdict(list)
        active_server_ids = set()
        for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
            try:
                sid = int(inbound.get('server_id'))
                active_server_ids.add(sid)
                if publish_all or sid in changed:
                    blocks[sid].append(inbound)
            except Exception:
                continue
        if publish_all:
            changed = set(blocks)

        for status in (GLOBAL_SERVER_DATA.get('servers_status') or []):
            try:
                active_server_ids.add(int(status.get('server_id')))
            except Exception:
                continue
        expected = {
            int(sid): int(revision or 0)
            for sid, revision in (expected_server_revisions or {}).items()
            if int(sid) in changed
        }
        revision_keys = [_redis_server_revision_key(sid) for sid in sorted(expected)]

        # WATCH makes the revision check and snapshot publication one atomic CAS.
        # The manifest is watched too, so concurrent publishers merge rather than
        # silently overwriting one another's server versions/status rows.
        for _attempt in range(3):
            pipe = client.pipeline()
            try:
                watch_keys = [REDIS_SNAPSHOT_MANIFEST_KEY] + revision_keys
                pipe.watch(*watch_keys)
                for sid in expected:
                    current = pipe.get(_redis_server_revision_key(sid))
                    if int(current or 0) != expected[sid]:
                        pipe.unwatch()
                        logger.info(
                            "Discarded stale refresh snapshot for server %s (revision %s -> %s)",
                            sid, expected[sid], int(current or 0),
                        )
                        return False

                old_manifest = {}
                old_manifest_blob = pipe.get(REDIS_SNAPSHOT_MANIFEST_KEY)
                if old_manifest_blob:
                    try:
                        old_manifest = _decode_snapshot(old_manifest_blob)
                    except Exception:
                        old_manifest = {}

                published_versions = {
                    int(k): str(v)
                    for k, v in (old_manifest.get('server_versions') or {}).items()
                }
                old_status_map = {}
                for status in (old_manifest.get('servers_status') or []):
                    try:
                        old_status_map[int(status.get('server_id'))] = status
                    except Exception:
                        continue
                local_status_map = {}
                local_status_order = []
                for status in (GLOBAL_SERVER_DATA.get('servers_status') or []):
                    try:
                        sid = int(status.get('server_id'))
                    except Exception:
                        continue
                    local_status_map[sid] = status
                    local_status_order.append(sid)

                merged_status_map = dict(old_status_map)
                metadata_only = not publish_all and not changed
                if metadata_only:
                    for sid, local_status in local_status_map.items():
                        old_status = old_status_map.get(sid) or {}
                        merged = dict(old_status)
                        merged.update(local_status)
                        # Reachability/status refreshes must not replace newer
                        # client counters written through by another worker.
                        if isinstance(old_status.get('stats'), dict):
                            merged['stats'] = old_status['stats']
                        merged_status_map[sid] = merged
                else:
                    for sid in changed:
                        if sid in local_status_map:
                            merged_status_map[sid] = local_status_map[sid]
                for sid in local_status_order:
                    if sid not in merged_status_map:
                        merged_status_map[sid] = local_status_map[sid]
                status_order = local_status_order + [
                    sid for sid in merged_status_map if sid not in local_status_order
                ]
                merged_statuses = [merged_status_map[sid] for sid in status_order]

                published_versions = {
                    sid: version for sid, version in published_versions.items()
                    if sid in active_server_ids or sid in merged_status_map
                }
                version = str(time.time_ns())
                for sid in changed:
                    published_versions[sid] = version

                manifest = {
                    'format': 2,
                    'version': version,
                    'server_versions': published_versions,
                    'stats': _aggregate_stats(merged_statuses),
                    'servers_status': merged_statuses,
                    'last_update': GLOBAL_SERVER_DATA.get('last_update'),
                }
                manifest_blob = _encode_snapshot(manifest)

                pipe.multi()
                encoded_blocks = {}
                for sid in changed:
                    block_blob = _encode_snapshot(blocks.get(sid, []))
                    encoded_blocks[sid] = block_blob
                    pipe.set(_redis_server_snapshot_key(sid), block_blob, ex=REDIS_SNAPSHOT_TTL)
                for sid in published_versions:
                    if sid not in changed:
                        pipe.expire(_redis_server_snapshot_key(sid), REDIS_SNAPSHOT_TTL)
                pipe.set(REDIS_SNAPSHOT_MANIFEST_KEY, manifest_blob, ex=REDIS_SNAPSHOT_TTL)
                pipe.set(REDIS_SNAPSHOT_VERSION_KEY, version, ex=REDIS_SNAPSHOT_TTL)
                pipe.execute()
                _PUBLISHED_SERVER_VERSIONS = published_versions
                _SNAPSHOT_METRICS['publishes'] += 1
                _SNAPSHOT_METRICS['blocks_encoded'] += len(encoded_blocks)
                _SNAPSHOT_METRICS['bytes_encoded'] += sum(
                    len(blob) for blob in encoded_blocks.values())
                return True
            except Exception as exc:
                try:
                    pipe.reset()
                except Exception:
                    pass
                if exc.__class__.__name__ == 'WatchError' and _attempt < 2:
                    continue
                raise
        return False
    except Exception as e:
        logger.warning("Redis publish snapshot failed: %s", e)
        return False


def _load_snapshot_from_redis_unlocked(force: bool = False, server_ids=None) -> bool:
    """Pull the shared snapshot from Redis into local GLOBAL_SERVER_DATA.

    Only blocks whose published version changed are decompressed, and the cheap
    version key short-circuits an unchanged snapshot. server_ids restricts the
    read to those servers (a per-server read-modify-write only needs its own
    block): every other block keeps the local copy. Returns True when the local
    cache was updated.
    """
    global _LAST_LOADED_SNAPSHOT_VERSION, _LAST_LOADED_SERVER_VERSIONS
    client = get_redis()
    if client is None:
        return False
    targets = None
    if server_ids is not None:
        targets = set()
        for sid in server_ids:
            try:
                targets.add(int(sid))
            except (TypeError, ValueError):
                continue
    try:
        version = client.get(REDIS_SNAPSHOT_VERSION_KEY)
        if version is None:
            return False
        if not force and targets is None and version == _LAST_LOADED_SNAPSHOT_VERSION:
            return False  # nothing new — skip the expensive decompress
        manifest_blob = client.get(REDIS_SNAPSHOT_MANIFEST_KEY)
        if manifest_blob:
            manifest = _decode_snapshot(manifest_blob)
            server_versions = {
                int(k): str(v) for k, v in (manifest.get('server_versions') or {}).items()
            }

            with GLOBAL_REFRESH_LOCK:
                current_blocks = defaultdict(list)
                for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
                    try:
                        current_blocks[int(inbound.get('server_id'))].append(inbound)
                    except Exception:
                        continue

            if targets is None:
                _SNAPSHOT_METRICS['full_loads'] += 1
            else:
                _SNAPSHOT_METRICS['targeted_loads'] += 1

            new_blocks = {}
            for sid, server_version in server_versions.items():
                local = current_blocks.get(sid)
                if targets is not None and sid not in targets and local:
                    # A per-server write cycle: leave every other block as it is.
                    new_blocks[sid] = local
                    continue
                if (not force and _LAST_LOADED_SERVER_VERSIONS.get(sid) == server_version
                        and local):
                    new_blocks[sid] = local
                    continue
                block_blob = client.get(_redis_server_snapshot_key(sid))
                if block_blob:
                    new_blocks[sid] = _decode_snapshot(block_blob)
                    _SNAPSHOT_METRICS['blocks_decoded'] += 1
                    _SNAPSHOT_METRICS['bytes_decoded'] += len(block_blob)
                elif local:
                    # Keep the last good local block if Redis is between writes.
                    new_blocks[sid] = local

            ordered_ids = []
            for status in (manifest.get('servers_status') or []):
                try:
                    ordered_ids.append(int(status.get('server_id')))
                except Exception:
                    continue
            ordered_ids.extend(sid for sid in new_blocks if sid not in ordered_ids)
            with GLOBAL_REFRESH_LOCK:
                GLOBAL_SERVER_DATA['inbounds'] = [
                    inbound for sid in ordered_ids for inbound in new_blocks.get(sid, [])
                ]
                GLOBAL_SERVER_DATA['stats'] = manifest.get('stats') or {}
                GLOBAL_SERVER_DATA['servers_status'] = manifest.get('servers_status') or []
                GLOBAL_SERVER_DATA['last_update'] = manifest.get('last_update')
                if targets is None:
                    _LAST_LOADED_SERVER_VERSIONS = server_versions
                else:
                    refreshed = dict(_LAST_LOADED_SERVER_VERSIONS)
                    for sid in targets:
                        if sid in server_versions:
                            refreshed[sid] = server_versions[sid]
                    _LAST_LOADED_SERVER_VERSIONS = refreshed
        else:
            # The old pickle format is intentionally rejected. Redis is an
            # ephemeral cache and a current worker will republish safe JSON.
            blob = client.get(REDIS_SNAPSHOT_KEY)
            if not blob:
                return False
            payload = _decode_snapshot(blob)
            with GLOBAL_REFRESH_LOCK:
                GLOBAL_SERVER_DATA['inbounds'] = payload.get('inbounds') or []
                GLOBAL_SERVER_DATA['stats'] = payload.get('stats') or {}
                GLOBAL_SERVER_DATA['servers_status'] = payload.get('servers_status') or []
                GLOBAL_SERVER_DATA['last_update'] = payload.get('last_update')
        if targets is None:
            # A targeted load deliberately leaves the global version unmarked so
            # the next full load still merges servers changed by other workers.
            _LAST_LOADED_SNAPSHOT_VERSION = version
        return True
    except Exception as e:
        logger.warning("Redis load snapshot failed: %s", e)
        return False


def load_snapshot_from_redis(force: bool = False, *, server_ids=None) -> bool:
    """Atomically hydrate the local snapshot under the shared RLock.

    server_ids limits the read to those servers, which is what a per-server
    read-modify-write cycle needs.
    """
    with GLOBAL_REFRESH_LOCK:
        return _load_snapshot_from_redis_unlocked(force=force, server_ids=server_ids)
