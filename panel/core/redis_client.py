"""Optional Redis shared cache + in-memory server-data snapshot state.

When REDIS_URL is set AND reachable, one worker fetches panel data and writes
the processed snapshot to Redis; all workers read it from there. If Redis is
missing/unreachable, the app transparently falls back to per-worker fetching.
"""
import os
import threading
import time
from collections import defaultdict

__all__ = [
    'GLOBAL_SERVER_DATA',
    'REDIS_URL',
    'REDIS_SNAPSHOT_KEY',
    'REDIS_SNAPSHOT_MANIFEST_KEY',
    'REDIS_SERVER_SNAPSHOT_PREFIX',
    'REDIS_SNAPSHOT_VERSION_KEY',
    'REDIS_SNAPSHOT_TTL',
    'REDIS_REFRESH_QUEUE_KEY',
    'REDIS_REFRESH_PROCESSING_KEY',
    'REDIS_REFRESH_JOB_PREFIX',
    'REDIS_REFRESH_SCOPE_PREFIX',
    'REDIS_REFRESH_JOB_TTL',
    'get_redis',
    'redis_enabled',
    'publish_snapshot_to_redis',
    'load_snapshot_from_redis',
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

REDIS_URL = (os.environ.get('REDIS_URL') or '').strip()
REDIS_SNAPSHOT_KEY = 'eve:server_data_snapshot'
REDIS_SNAPSHOT_MANIFEST_KEY = 'eve:server_data_manifest'
REDIS_SERVER_SNAPSHOT_PREFIX = 'eve:server_data:'
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
            print(f"[Redis] connected: {REDIS_URL}")
        except Exception as _re:
            print(f"[Redis] unavailable ({_re}); using per-worker in-memory cache.")
            _REDIS_CLIENT = None
            _REDIS_CHECKED = False
            _REDIS_RETRY_AFTER = time.monotonic() + 10.0
        return _REDIS_CLIENT


def redis_enabled() -> bool:
    return get_redis() is not None


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


def _redis_server_snapshot_key(server_id: int) -> str:
    return f'{REDIS_SERVER_SNAPSHOT_PREFIX}{int(server_id)}'


def publish_snapshot_to_redis(changed_server_ids=None) -> bool:
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
        import pickle, zlib
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

        # Other web processes can write-through one server after an edit. Merge
        # the authoritative versions on every publish so this process never
        # rolls such a newer server block back while publishing another server.
        try:
            old_manifest_blob = client.get(REDIS_SNAPSHOT_MANIFEST_KEY)
            if old_manifest_blob:
                old_manifest = pickle.loads(zlib.decompress(old_manifest_blob))
                _PUBLISHED_SERVER_VERSIONS.update({
                    int(k): str(v) for k, v in (old_manifest.get('server_versions') or {}).items()
                })
        except Exception:
            pass

        for status in (GLOBAL_SERVER_DATA.get('servers_status') or []):
            try:
                active_server_ids.add(int(status.get('server_id')))
            except Exception:
                continue
        _PUBLISHED_SERVER_VERSIONS = {
            sid: version for sid, version in _PUBLISHED_SERVER_VERSIONS.items()
            if sid in active_server_ids
        }

        version = str(time.time_ns())
        pipe = client.pipeline()
        for sid in changed:
            block_blob = zlib.compress(
                pickle.dumps(blocks.get(sid, []), protocol=pickle.HIGHEST_PROTOCOL), 1
            )
            pipe.set(_redis_server_snapshot_key(sid), block_blob, ex=REDIS_SNAPSHOT_TTL)
            _PUBLISHED_SERVER_VERSIONS[sid] = version

        # Refresh TTLs for unchanged blocks referenced by the manifest.
        for sid in _PUBLISHED_SERVER_VERSIONS:
            if sid not in changed:
                pipe.expire(_redis_server_snapshot_key(sid), REDIS_SNAPSHOT_TTL)

        manifest = {
            'format': 2,
            'version': version,
            'server_versions': _PUBLISHED_SERVER_VERSIONS,
            'stats': GLOBAL_SERVER_DATA.get('stats') or {},
            'servers_status': GLOBAL_SERVER_DATA.get('servers_status') or [],
            'last_update': GLOBAL_SERVER_DATA.get('last_update'),
        }
        manifest_blob = zlib.compress(
            pickle.dumps(manifest, protocol=pickle.HIGHEST_PROTOCOL), 1
        )
        pipe.set(REDIS_SNAPSHOT_MANIFEST_KEY, manifest_blob, ex=REDIS_SNAPSHOT_TTL)
        pipe.set(REDIS_SNAPSHOT_VERSION_KEY, version, ex=REDIS_SNAPSHOT_TTL)
        pipe.execute()
        return True
    except Exception as e:
        print(f"[Redis] publish snapshot failed: {e}")
        return False


def load_snapshot_from_redis(force: bool = False) -> bool:
    """Pull the shared snapshot from Redis into local GLOBAL_SERVER_DATA, but
    only when the version changed (cheap version check first). Returns True if
    the local cache was updated."""
    global _LAST_LOADED_SNAPSHOT_VERSION, _LAST_LOADED_SERVER_VERSIONS
    client = get_redis()
    if client is None:
        return False
    try:
        version = client.get(REDIS_SNAPSHOT_VERSION_KEY)
        if version is None:
            return False
        if not force and version == _LAST_LOADED_SNAPSHOT_VERSION:
            return False  # nothing new — skip the expensive decompress
        import pickle, zlib
        manifest_blob = client.get(REDIS_SNAPSHOT_MANIFEST_KEY)
        if manifest_blob:
            manifest = pickle.loads(zlib.decompress(manifest_blob))
            server_versions = {
                int(k): str(v) for k, v in (manifest.get('server_versions') or {}).items()
            }

            current_blocks = defaultdict(list)
            for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
                try:
                    current_blocks[int(inbound.get('server_id'))].append(inbound)
                except Exception:
                    continue

            new_blocks = {}
            for sid, server_version in server_versions.items():
                if (not force and _LAST_LOADED_SERVER_VERSIONS.get(sid) == server_version
                        and sid in current_blocks):
                    new_blocks[sid] = current_blocks[sid]
                    continue
                block_blob = client.get(_redis_server_snapshot_key(sid))
                if block_blob:
                    new_blocks[sid] = pickle.loads(zlib.decompress(block_blob))
                elif sid in current_blocks:
                    # Keep the last good local block if Redis is between writes.
                    new_blocks[sid] = current_blocks[sid]

            ordered_ids = []
            for status in (manifest.get('servers_status') or []):
                try:
                    ordered_ids.append(int(status.get('server_id')))
                except Exception:
                    continue
            ordered_ids.extend(sid for sid in new_blocks if sid not in ordered_ids)
            GLOBAL_SERVER_DATA['inbounds'] = [
                inbound for sid in ordered_ids for inbound in new_blocks.get(sid, [])
            ]
            GLOBAL_SERVER_DATA['stats'] = manifest.get('stats') or {}
            GLOBAL_SERVER_DATA['servers_status'] = manifest.get('servers_status') or []
            GLOBAL_SERVER_DATA['last_update'] = manifest.get('last_update')
            _LAST_LOADED_SERVER_VERSIONS = server_versions
        else:
            # Rolling-upgrade compatibility with snapshots written by v1 workers.
            blob = client.get(REDIS_SNAPSHOT_KEY)
            if not blob:
                return False
            payload = pickle.loads(zlib.decompress(blob))
            GLOBAL_SERVER_DATA['inbounds'] = payload.get('inbounds') or []
            GLOBAL_SERVER_DATA['stats'] = payload.get('stats') or {}
            GLOBAL_SERVER_DATA['servers_status'] = payload.get('servers_status') or []
            GLOBAL_SERVER_DATA['last_update'] = payload.get('last_update')
        _LAST_LOADED_SNAPSHOT_VERSION = version
        return True
    except Exception as e:
        print(f"[Redis] load snapshot failed: {e}")
        return False
