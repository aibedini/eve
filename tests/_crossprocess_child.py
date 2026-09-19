"""Child process for tests/test_watch_propagation_crossprocess.py.

Runs ONE policy operation in a fresh interpreter, optionally behind a shared
file-backed Redis, and prints the resulting state as JSON on the last line. It is
a real OS process on purpose: the bug it proves fixed (a dashboard watch that
never reached the fetching process) is invisible to any single-process test.

Usage: python _crossprocess_child.py <op> <server_id> [ticket]
Env:   EVE_TEST_REDIS_FILE=<path>   shared store (required when EVE_TEST_SHARED=1)
       EVE_TEST_SHARED=1            install the shared backend
"""
import contextlib
import json
import os
import sys
import time

sys.path.insert(0, os.environ['EVE_TEST_REPO'])

import panel.core.fetch_sequence  # noqa: E402
import panel.core.redis_client  # noqa: E402
import panel.core.refresh_policy as refresh_policy  # noqa: E402


class FileRedis:
    """The subset of the Redis client the policy modules call, file-backed.

    It is a real shared store with an exclusive lock per operation, and its eval()
    refuses to fake a script it does not recognise -- so the tests exercise the
    compare-and-set path instead of a convenient fiction.

    Every entry records its own kind, because the policy uses three of them: a string
    (an activity timestamp, a watch mark), a hash (the watch reasons, the client
    fences) and a sorted set (the index that replaced the keyspace scan). A fake that
    silently accepted a call it does not implement would turn "the mark crossed the
    process boundary" into "the call was swallowed", which is the one result these
    tests exist to rule out.
    """

    def __init__(self, path):
        self.path = path

    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save(self, data):
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def _lock(self):
        lock = self.path + '.lock'
        deadline = time.time() + 15
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                if time.time() > deadline:
                    raise TimeoutError('stub redis lock timeout')
                time.sleep(0.005)
        try:
            yield
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass

    @staticmethod
    def _live(entry):
        if not entry:
            return False
        exp = entry.get('exp')
        return not (exp and time.time() > exp)

    def _alive(self, data, key):
        entry = data.get(key)
        if not self._live(entry):
            data.pop(key, None)
            return None
        return entry

    def set(self, key, value, ex=None):
        with self._lock():
            data = self._load()
            data[key] = {'t': 'string', 'v': value,
                         'exp': (time.time() + float(ex)) if ex else None}
            self._save(data)
        return True

    def get(self, key):
        with self._lock():
            data = self._load()
            entry = self._alive(data, key)
            if entry is None:
                self._save(data)
                return None
            return entry.get('v')

    def incr(self, key):
        with self._lock():
            data = self._load()
            entry = data.get(key) or {}
            value = int(entry.get('v') or 0) + 1
            data[key] = {'t': 'string', 'v': value, 'exp': None}
            self._save(data)
        return value

    def publish(self, channel, message):
        # The policy treats a nudge as best effort: a lost message costs latency, not
        # correctness. The stub therefore records nothing and reports no subscriber,
        # which is exactly the "durable marks still drive the cadence" case.
        return 0

    def ttl(self, key):
        with self._lock():
            data = self._load()
            entry = self._alive(data, key)
        if entry is None:
            return -2
        exp = entry.get('exp')
        if not exp:
            return -1
        return max(0, int(round(exp - time.time())))

    def expire(self, key, seconds):
        with self._lock():
            data = self._load()
            entry = self._alive(data, key)
            if entry is None:
                return False
            entry['exp'] = time.time() + float(seconds)
            self._save(data)
        return True

    def delete(self, *keys):
        with self._lock():
            data = self._load()
            removed = 0
            for key in keys:
                if data.pop(key, None) is not None:
                    removed += 1
            self._save(data)
        return removed

    def hset(self, key, field, value):
        with self._lock():
            data = self._load()
            entry = self._alive(data, key) or {'t': 'hash', 'h': {}, 'exp': None}
            entry.setdefault('h', {})[str(field)] = value
            data[key] = entry
            self._save(data)
        return 1

    def hmget(self, key, fields):
        with self._lock():
            data = self._load()
            entry = self._alive(data, key)
            self._save(data)
        bucket = (entry or {}).get('h') or {}
        return [bucket.get(str(field)) for field in fields]

    def hgetall(self, key):
        with self._lock():
            data = self._load()
            entry = self._alive(data, key)
            self._save(data)
        return dict((entry or {}).get('h') or {})

    def hdel(self, key, *fields):
        with self._lock():
            data = self._load()
            entry = self._alive(data, key)
            if entry is None:
                self._save(data)
                return 0
            removed = 0
            for field in fields:
                if entry.get('h', {}).pop(str(field), None) is not None:
                    removed += 1
            self._save(data)
        return removed

    def zadd(self, key, mapping):
        with self._lock():
            data = self._load()
            entry = self._alive(data, key) or {'t': 'zset', 'z': {}, 'exp': None}
            bucket = entry.setdefault('z', {})
            for member, score in mapping.items():
                bucket[str(member)] = float(score)
            data[key] = entry
            self._save(data)
        return len(mapping)

    def zremrangebyscore(self, key, minimum, maximum):
        low, high = _score(minimum), _score(maximum)
        with self._lock():
            data = self._load()
            entry = self._alive(data, key)
            if entry is None:
                self._save(data)
                return 0
            bucket = entry.get('z') or {}
            doomed = [m for m, s in bucket.items() if low <= s <= high]
            for member in doomed:
                bucket.pop(member, None)
            self._save(data)
        return len(doomed)

    def zrangebyscore(self, key, minimum, maximum):
        low, high = _score(minimum), _score(maximum)
        with self._lock():
            data = self._load()
            entry = self._alive(data, key)
            self._save(data)
        bucket = (entry or {}).get('z') or {}
        members = [m for m, s in bucket.items() if low <= s <= high]
        members.sort(key=lambda member: (bucket[member], member))
        return [member.encode('utf-8') for member in members]

    def scan_iter(self, match=None, count=100):
        prefix = (match or '*').rstrip('*')
        with self._lock():
            data = self._load()
        for key, entry in list(data.items()):
            if not key.startswith(prefix):
                continue
            if not self._live(entry):
                continue
            yield key.encode('utf-8')

    def pipeline(self):
        return _FilePipeline(self)

    def eval(self, script, numkeys, key, value, ttl=None):
        if "redis.call('GET'" not in script:
            raise RuntimeError('stub refuses to fake an unknown script')
        with self._lock():
            data = self._load()
            entry = data.get(key) or {}
            current = entry.get('v')
            if current is not None and int(current) >= int(value):
                return 0
            data[key] = {'t': 'string', 'v': int(value),
                         'exp': (time.time() + float(ttl)) if ttl else None}
            self._save(data)
        return 1


class _FilePipeline:
    """Queued commands, executed in order under the store's own locking."""

    def __init__(self, store):
        self._store = store
        self._queued = []

    def set(self, *args, **kwargs):
        self._queued.append(('set', args, kwargs))
        return self

    def zadd(self, *args, **kwargs):
        self._queued.append(('zadd', args, kwargs))
        return self

    def hset(self, *args, **kwargs):
        self._queued.append(('hset', args, kwargs))
        return self

    def expire(self, *args, **kwargs):
        self._queued.append(('expire', args, kwargs))
        return self

    def delete(self, *args, **kwargs):
        self._queued.append(('delete', args, kwargs))
        return self

    def execute(self):
        results = []
        for name, args, kwargs in self._queued:
            method = getattr(self._store, name, None)
            if method is None:
                raise RuntimeError('stub refuses to fake %s' % name)
            results.append(method(*args, **kwargs))
        self._queued = []
        return results


def _score(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8')
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ('-inf', 'inf'):
            return float('-inf')
        if lowered == '+inf':
            return float('inf')
    return float(value)


def _install_shared_backend():
    if os.environ.get('EVE_TEST_SHARED') != '1':
        return 'process'
    stub = FileRedis(os.environ['EVE_TEST_REDIS_FILE'])
    panel.core.redis_client.get_redis = lambda: stub
    # fetch_sequence imported the function directly, so the module attribute is
    # what has to be replaced -- the same reason a stale import here would make the
    # ticket CAS silently fall back to process-local state.
    panel.core.fetch_sequence.get_redis = lambda: stub
    return 'redis'


def main():
    op = sys.argv[1]
    sid = int(sys.argv[2])
    backend = _install_shared_backend()
    if op == 'watch-set':
        refresh_policy.note_watched_servers([sid])
        out = {
            'backend': backend,
            'watched': refresh_policy.is_server_watched(sid),
            'shared_backend': refresh_policy.server_watch_marks()['shared_backend'],
        }
    elif op == 'watch-check':
        out = {
            'backend': backend,
            'watched': refresh_policy.is_server_watched(sid),
            'ids': refresh_policy.watched_server_ids(),
            'interval': refresh_policy.server_interval(sid),
            'idle_interval': refresh_policy.server_idle_seconds(),
        }
    elif op == 'seq-begin':
        out = {'ticket': panel.core.fetch_sequence.begin(sid)}
    elif op == 'seq-accept':
        ticket = int(sys.argv[3])
        out = {
            'accepted': panel.core.fetch_sequence.accept(sid, ticket),
            'last': panel.core.fetch_sequence.last_accepted(sid),
        }
    else:
        raise SystemExit('unknown op: %s' % op)
    print(json.dumps(out))


if __name__ == '__main__':
    main()
