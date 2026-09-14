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

    def set(self, key, value, ex=None):
        with self._lock():
            data = self._load()
            data[key] = {'v': value,
                         'exp': (time.time() + float(ex)) if ex else None}
            self._save(data)
        return True

    def get(self, key):
        with self._lock():
            data = self._load()
        entry = data.get(key)
        if not entry:
            return None
        if entry.get('exp') and time.time() > entry['exp']:
            return None
        return entry['v']

    def incr(self, key):
        with self._lock():
            data = self._load()
            entry = data.get(key) or {}
            value = int(entry.get('v') or 0) + 1
            data[key] = {'v': value, 'exp': None}
            self._save(data)
        return value

    def scan_iter(self, match=None, count=100):
        prefix = (match or '*').rstrip('*')
        with self._lock():
            data = self._load()
        for key, entry in list(data.items()):
            if not key.startswith(prefix):
                continue
            if entry.get('exp') and time.time() > entry['exp']:
                continue
            yield key.encode('utf-8')

    def eval(self, script, numkeys, key, value, ttl=None):
        if "redis.call('GET'" not in script:
            raise RuntimeError('stub refuses to fake an unknown script')
        with self._lock():
            data = self._load()
            entry = data.get(key) or {}
            current = entry.get('v')
            if current is not None and int(current) >= int(value):
                return 0
            data[key] = {'v': int(value),
                         'exp': (time.time() + float(ttl)) if ttl else None}
            self._save(data)
        return 1


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
