"""Child process for scripts/integration_redis_multiprocess.py.

One real OS process, one role, talking to a REAL Redis. The parent harness
(scripts/integration_redis_multiprocess.py) starts several of these at once
because the pipeline this proves is split across processes: the browser's watch
mark is written in a web process, the wake nudge is received by the process that
owns the fetch loop, and a third process only ever reads the published snapshot.
No in-process test can observe that, which is why the child exists as a real
interpreter with its own module state.

Output contract: the LAST line of stdout is one JSON object; every other line is
an event line prefixed with 'child:'. The parent parses only the last line, so a
chatty library on stdout cannot be mistaken for the result.

Namespace safety: this script must never read or write a production-looking key.
EVE_TEST_REDIS_NAMESPACE prepends a test-only segment to every snapshot key the
real code publishes or loads, so a run against a Redis that also serves a live
panel cannot overwrite that panel's snapshot. The refresh-policy keys (watch
marks, wake channel, client fences) cannot be renamed -- the product code owns
those names -- so the harness restricts itself to a dedicated high server-id
range and removes those keys in a cleanup pass.

Environment:
  EVE_TEST_REPO              repository root to import ``panel`` from
  EVE_TEST_REDIS_NAMESPACE   snapshot key namespace, e.g. 'eve-it-1234abcd'
  REDIS_URL                  the real Redis to talk to (set by the harness)

Usage (argv):
  <op> [args...]       see main() for the op list

Exit code is 0 on success. A failing op raises, so the parent sees a non-zero
exit and the traceback on stderr instead of a plausible-looking JSON line.
"""
import argparse
import json
import os
import sys
import threading
import time

# The repo root must be importable before any panel import. The harness passes
# EVE_TEST_REPO; the fallback keeps the script usable when it is launched by hand
# from the repository, because scripts/ is one level below the root.
_REPO = os.environ.get('EVE_TEST_REPO') or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# The CONTROL role needs the product's no-Redis branch. panel.core.redis_client reads
# REDIS_URL at import time, so the variable has to go before the import below -- there
# is no way to make the module forget a URL it already captured. This is the app's own
# fallback path (a single-process install), not a patched-out client.
_NO_REDIS = '--no-redis' in sys.argv
if _NO_REDIS:
    for _name in ('REDIS_URL', 'EVE_INTEGRATION_REDIS_URL'):
        os.environ.pop(_name, None)
    # Consumed here so it never reaches argparse: it describes HOW this process must
    # be configured, and the configuration already happened.
    sys.argv = [item for item in sys.argv if item != '--no-redis']

import panel.core.redis_client as redis_client  # noqa: E402
import panel.core.refresh_policy as refresh_policy  # noqa: E402


def event(message):
    """Progress line. Never parseable as the result: the parent reads the last line."""
    try:
        sys.stdout.write('child: %s\n' % message)
        sys.stdout.flush()
    except Exception:
        pass


# -- snapshot key namespace -------------------------------------------------
# The real module builds snapshot keys from its own constants. Prefixing those
# constants BEFORE the first publish/load is what keeps a live panel's snapshot
# out of reach: a key of the form 'eve:server_data:...' is never touched. The
# per-server helpers are functions built from the prefixes, so they cannot be
# patched directly -- the constant is the single point of control and every
# helper reads it at call time.
_NAMESPACE = (os.environ.get('EVE_TEST_REDIS_NAMESPACE') or '').strip()


def _apply_key_namespace():
    if not _NAMESPACE:
        return None
    prefix = 'eve:it:%s:' % _NAMESPACE
    renames = {
        'REDIS_SNAPSHOT_KEY': 'server_data_snapshot',
        'REDIS_SNAPSHOT_MANIFEST_KEY': 'server_data_manifest',
        'REDIS_SERVER_SNAPSHOT_PREFIX': 'server_data:',
        'REDIS_SERVER_REVISION_PREFIX': 'server_revision:',
        'REDIS_SNAPSHOT_VERSION_KEY': 'server_data_version',
    }
    for constant, suffix in renames.items():
        setattr(redis_client, constant, prefix + suffix)
    return prefix


# -- redis handle -----------------------------------------------------------


def _redis():
    """The app's own client, or exit loudly.

    A silent None here would turn 'Redis is down' into 'the mark did not cross',
    which is the exact confusion this harness exists to remove, so the child
    refuses to run without a backend.
    """
    client = redis_client.get_redis()
    if client is None:
        raise SystemExit('child: no Redis from panel.core.redis_client.get_redis()')
    return client


def _decode(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def _cleanup(args):
    """Remove everything this run could have written. Idempotent; safe in finally."""
    client = _redis()
    removed = []
    namespace_prefix = _NAMESPACE and 'eve:it:%s:' % _NAMESPACE
    if namespace_prefix:
        # Only keys carrying this run's namespace: a concurrent manual run against
        # the same Redis must not delete another namespace's snapshot.
        for key in client.scan_iter(match=namespace_prefix + '*', count=200):
            name = _decode(key)
            client.delete(name)
            removed.append(name)

    # Refresh-policy keys are owned by the product and carry no namespace, so they
    # are removed by their exact names / server ids instead of a wildcard scan.
    client.delete(refresh_policy.ACTIVITY_KEY)
    removed.append(refresh_policy.ACTIVITY_KEY)
    for sid in args.server_id:
        client.delete(refresh_policy.WATCH_KEY_PREFIX + str(sid))
        client.delete(refresh_policy.MUTATION_FENCE_PREFIX + str(sid))
        removed.append(refresh_policy.WATCH_KEY_PREFIX + str(sid))
        removed.append(refresh_policy.MUTATION_FENCE_PREFIX + str(sid))
        client.zrem(refresh_policy.HOT_SERVERS_KEY, str(sid))
        client.hdel(refresh_policy.WATCH_REASON_KEY, str(sid))
    _write_result({'op': 'cleanup', 'namespace': namespace_prefix, 'removed': removed,
                   'count': len(removed)})


# -- wake capture -----------------------------------------------------------
# _handle_wake_payload is module state, so replacing the module attribute is the
# only reliable hook: refresh_policy's own listener thread calls it by name at
# message time. That gives the harness the real pub/sub arrival instant AND the
# payload the product actually acted on, instead of the harness's assumption
# about which server the fetcher was told about.
_LAST_WAKE = {'at': None, 'server_ids': None, 'reason': None}
_WAKE_LOCK = threading.Lock()
_ORIGINAL_HANDLE_WAKE = refresh_policy._handle_wake_payload


def _capturing_handle_wake(raw):
    payload = {}
    try:
        text = _decode(raw)
        parsed = json.loads(text or '{}')
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:
        payload = {}
    with _WAKE_LOCK:
        _LAST_WAKE['at'] = time.time()
        _LAST_WAKE['server_ids'] = payload.get('server_ids')
        _LAST_WAKE['reason'] = payload.get('reason')
    return _ORIGINAL_HANDLE_WAKE(raw)


refresh_policy._handle_wake_payload = _capturing_handle_wake


def _snapshot_wake():
    with _WAKE_LOCK:
        return dict(_LAST_WAKE)


def _reset_wake():
    with _WAKE_LOCK:
        _LAST_WAKE['at'] = None
        _LAST_WAKE['server_ids'] = None
        _LAST_WAKE['reason'] = None


# -- ops --------------------------------------------------------------------


def op_watch_write(args):
    """What a web process does when a dashboard declares a server on screen."""
    sid = int(args.server_id[0])
    before = time.time()
    refresh_policy.note_watch(sid, reason=args.reason)
    after = time.time()
    _write_result({
        'op': 'watch-write',
        'server_id': sid,
        'before': before,
        'after': after,
        'shared_backend': refresh_policy.server_watch_marks()['shared_backend'],
        'wake_published': True,
    })


def op_watch_watch(args):
    """The process that owns the fetch loop: subscribe, then report each wake.

    The handshake is on stdin so 'ready to receive' is a real statement about the
    listener thread being alive and subscribed, not a sleep the parent hopes was
    long enough. Without it a fast publish could land before SUBSCRIBE and the
    measured latency would be a measurement of nothing.
    """
    started = False
    for _attempt in range(3):
        if refresh_policy.start_wake_listener() and refresh_policy.wake_listener_active():
            started = True
            break
        time.sleep(0.2)
    if not started:
        raise SystemExit('child: wake listener did not start')
    # 'No wake arrived' has two very different causes: the message never reached this
    # process, or it arrived and the product's handler refused the payload. The
    # wrapper below is applied to the module attribute the listener thread resolves at
    # message time, so it records what was genuinely delivered -- captured before the
    # listener can act on it -- and the parent can tell the two apart.
    if os.environ.get('EVE_INTEGRATION_DEBUG') == '1':
        _install_wake_trace()
    _write_result({'op': 'watch-watch', 'event': 'ready',
                   'listener_active': True,
                   'shared_backend': refresh_policy.server_watch_marks()['shared_backend']})

    for line in sys.stdin:
        parts = (line or '').strip().split()
        if not parts:
            continue
        if parts[0] == 'expect':
            label = parts[1] if len(parts) > 1 else ''
            # Drain any stale nudge first: a message left over from the previous
            # round would make the next round look instant.
            _reset_wake()
            refresh_policy._wake.clear()
            listening_at = time.time()
            _write_result({'op': 'watch-watch', 'event': 'listening',
                           'label': label, 'listening_at': listening_at})
            woken = refresh_policy._wake.wait(timeout=args.timeout)
            received = time.time()
            sample = _snapshot_wake()
            _write_result({
                'op': 'watch-watch',
                'event': 'wake',
                'label': label,
                'woken': bool(woken),
                'listening_at': listening_at,
                'received_at': received,
                'payload_at': sample.get('at'),
                'payload_server_ids': sample.get('server_ids'),
                'payload_reason': sample.get('reason'),
            })
        elif parts[0] == 'check':
            moment = time.time()
            try:
                sids = [int(value) for value in parts[1:]]
            except ValueError:
                raise SystemExit('child: check needs integer server ids')
            client = _redis()
            _write_result({
                'op': 'watch-watch',
                'event': 'check',
                'at': moment,
                'watched': {str(sid): refresh_policy.is_server_watched(sid) for sid in sids},
                'interval': {str(sid): refresh_policy.server_interval(sid) for sid in sids},
                'mode': {str(sid): refresh_policy.server_mode(sid) for sid in sids},
                # The mark's expiry is what releases a closed tab on its own. A mark
                # with no TTL would keep the panel hot forever, so the reader checks
                # the TTLs too instead of only the membership.
                'mark_ttl': {str(sid): client.ttl(refresh_policy.WATCH_KEY_PREFIX + str(sid))
                             for sid in sids},
                'idle_interval': refresh_policy.server_idle_seconds(),
                'active_interval': refresh_policy.server_active_seconds(),
                'warm_interval': refresh_policy.server_warm_seconds(),
                # Does THIS process actually hold a subscription right now? A ready
                # listener that later lost its connection would otherwise look like a
                # publisher that never sent anything.
                'wake_subscribers': _wake_subscribers(client),
            })
        elif parts[0] == 'stop':
            break
    refresh_policy.stop_wake_listener()


def op_fence_write(args):
    """A verified EVE mutation records the state it just read back from the panel."""
    sid = int(args.server_id[0])
    state = {
        'used_up': args.used_up,
        'used_down': args.used_down,
        'total_bytes': args.total_bytes,
        'expiry_time': args.expiry_time,
    }
    before = time.time()
    ok = refresh_policy.record_client_fence(sid, args.email, state)
    after = time.time()
    _write_result({
        'op': 'fence-write',
        'server_id': sid,
        'email': args.email,
        'ok': bool(ok),
        'before': before,
        'after': after,
        'expected': state,
    })


def op_fence_read(args):
    """A different process must see the fence, with the verified counters intact."""
    sid = int(args.server_id[0])
    before = time.time()
    fences = refresh_policy.client_fences(sid)
    after = time.time()
    row = fences.get(args.email) or {}
    _write_result({
        'op': 'fence-read',
        'server_id': sid,
        'email': args.email,
        'before': before,
        'after': after,
        'found': bool(row),
        'emails': sorted(fences.keys()),
        'fence': row,
    })


def op_fetch_publish(args):
    """The fetcher's write-through: read a panel, admit the result, publish it.

    The panel read is synthetic (a sleep of --latency-ms) rather than an HTTP call
    to a real X-UI. That is deliberate: an offline harness must be deterministic,
    and a panel that is merely unreachable would prove the backoff path instead of
    the publish path. What is NOT synthetic is everything after the read -- the
    revision re-check that discards a result invalidated by a concurrent
    mutation, the local commit, and publish_snapshot_to_redis() itself -- because
    those are the parts that cross the process boundary.
    """
    _apply_key_namespace()
    sid = int(args.server_id[0])
    read_started = time.time()
    time.sleep(max(0.0, args.latency_ms / 1000.0))
    read_finished = time.time()

    revision_before = redis_client.get_server_revision(sid)
    inbound = {
        'server_id': sid,
        'id': args.inbound_id,
        'remark': 'eve-integration-synthetic',
        'clients': [{
            'email': args.email,
            'up': args.used_up,
            'down': args.used_down,
            'totalGB': args.total_bytes,
            'enable': True,
        }],
    }
    status = {
        'server_id': sid,
        'success': True,
        'reachable': True,
        'reachable_error': None,
        'stats': {
            'total_clients': 1,
            'online_clients': 1,
            'upload_raw': args.used_up,
            'download_raw': args.used_down,
        },
    }
    redis_client.GLOBAL_SERVER_DATA['inbounds'] = [inbound]
    redis_client.GLOBAL_SERVER_DATA['servers_status'] = [status]
    redis_client.GLOBAL_SERVER_DATA['last_update'] = args.last_update

    publish_started = time.time()
    published = redis_client.publish_snapshot_to_redis(
        [sid], expected_server_revisions={sid: revision_before})
    publish_finished = time.time()

    client = _redis()
    version = _decode(client.get(redis_client.REDIS_SNAPSHOT_VERSION_KEY))
    # An untested TTL is the failure mode this step must not miss: a snapshot cache
    # entry with no expiry leaks keyspace forever and serves stale data after the
    # fetcher dies. Reading the TTLs back from the server is the only way to know the
    # expiries were really applied (a client-side stub that drops `ex=` would hide it).
    ttls = {
        'version': client.ttl(redis_client.REDIS_SNAPSHOT_VERSION_KEY),
        'manifest': client.ttl(redis_client.REDIS_SNAPSHOT_MANIFEST_KEY),
        'block': client.ttl(redis_client.REDIS_SERVER_SNAPSHOT_PREFIX + str(sid)),
        'revision': client.ttl(redis_client.REDIS_SERVER_REVISION_PREFIX + str(sid)),
    }
    _write_result({
        'op': 'fetch-publish',
        'server_id': sid,
        'read_started': read_started,
        'read_finished': read_finished,
        'read_ms': round((read_finished - read_started) * 1000.0, 3),
        'publish_started': publish_started,
        'publish_finished': publish_finished,
        'publish_ms': round((publish_finished - publish_started) * 1000.0, 3),
        'published': bool(published),
        'revision_before': revision_before,
        'version': version,
        'last_update': args.last_update,
        'ttls': ttls,
        'manifest_key': redis_client.REDIS_SNAPSHOT_MANIFEST_KEY,
        'namespace': (redis_client.REDIS_SNAPSHOT_MANIFEST_KEY
                      if not _NAMESPACE else 'eve:it:%s:' % _NAMESPACE),
        'inbounds': len(redis_client.GLOBAL_SERVER_DATA['inbounds']),
    })


def op_read_snapshot(args):
    """What snapshot_reader_worker does in a worker that never fetches.

    It primes once, then polls load_snapshot_from_redis() and only decompresses when
    the shared version actually changed -- the same calls in the same order as the
    real worker, at a scaled-down interval (the worker sleeps 10 s; --poll-interval
    defaults to 0.2 s so a run does not take a minute). Reproducing the worker's shape
    is what makes the reported latency the latency a dashboard process really pays,
    rather than a bespoke read invented for the measurement.
    """
    _apply_key_namespace()
    client = _redis()
    primed = redis_client.load_snapshot_from_redis(force=True)
    prime_finished = time.time()
    if not args.version:
        # Prime-only mode: the harness starts this child BEFORE the publish so the
        # process that will observe the revision is already up and has already paid
        # its first load. Timing process start-up instead would measure interpreter
        # imports, not the pipeline.
        _write_result({'op': 'read-snapshot', 'server_id': int(args.server_id[0]),
                       'visible': False, 'primed': bool(primed),
                       'observed_at': None, 'primed_at': prime_finished,
                       'version': '', 'seen_version': None, 'polls': 1,
                       'waiting_for': None, 'last_update': None,
                       'last_update_matches': False, 'inbounds': 0, 'clients': 0,
                       'metrics': redis_client.snapshot_metrics()})
        return

    deadline = prime_finished + max(1.0, args.timeout)
    polls = 0
    observed_at = None
    raw = None
    while time.time() < deadline:
        polls += 1
        try:
            redis_client.load_snapshot_from_redis()
        except Exception:
            pass
        raw = _decode(client.get(redis_client.REDIS_SNAPSHOT_VERSION_KEY))
        if raw == args.version:
            observed_at = time.time()
            break
        time.sleep(args.poll_interval)

    last_update = redis_client.GLOBAL_SERVER_DATA.get('last_update')
    inbounds = redis_client.GLOBAL_SERVER_DATA.get('inbounds') or []
    _write_result({
        'op': 'read-snapshot',
        'server_id': int(args.server_id[0]),
        'version': args.version,
        'waiting_for': args.version,
        'seen_version': raw,
        'observed_at': observed_at,
        'primed_at': prime_finished,
        'polls': polls,
        'visible': observed_at is not None,
        'last_update': last_update,
        'last_update_matches': last_update == args.last_update,
        'inbounds': len(inbounds),
        'clients': sum(len(row.get('clients') or []) for row in inbounds),
        'metrics': redis_client.snapshot_metrics(),
    })


def _install_wake_trace():
    """Print every wake payload the listener thread actually receives.

    Enabled by EVE_INTEGRATION_DEBUG=1. A harness run that reports 'never woken' is
    otherwise ambiguous between 'Redis never delivered' and 'the handler rejected the
    payload', which are different product bugs.
    """
    current = refresh_policy._handle_wake_payload
    if getattr(current, '_eve_traced', False):
        return

    def traced(raw):
        event('wake payload received raw=%r last=%r' % (raw, _snapshot_wake()))
        result = current(raw)
        event('wake handler returned %r last=%r' % (result, _snapshot_wake()))
        return result

    traced._eve_traced = True
    refresh_policy._handle_wake_payload = traced


def _wake_subscribers(client):
    """How many connections the SERVER believes are subscribed to the wake channel.

    Best effort diagnostics: it answers "is the listener really subscribed" from the
    server's point of view, which a client-side check cannot.
    """
    try:
        reply = client.execute_command('PUBSUB', 'NUMSUB', refresh_policy.WAKE_CHANNEL)
    except Exception:
        return None
    try:
        return int(reply[1])
    except Exception:
        return None


def op_watch_control(args):
    """A process with no Redis backend: the control for the watch proof.

    Same code, same shape, no shared store. If this reported the server as watched
    the harness would be measuring its own fake, so it must report the idle
    interval -- that negative is what turns the positive result into evidence.
    """
    sid = int(args.server_id[0])
    _write_result({
        'op': 'watch-control',
        'server_id': sid,
        'backend': refresh_policy.server_watch_marks()['shared_backend'],
        'watched': refresh_policy.is_server_watched(sid),
        'interval': refresh_policy.server_interval(sid),
        'idle_interval': refresh_policy.server_idle_seconds(),
    })


def _write_result(payload):
    """The one machine-readable line: last on stdout, so nothing can follow it."""
    sys.stdout.write(json.dumps(payload, sort_keys=True) + '\n')
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description='real-Redis pipeline child role')
    parser.add_argument('op')
    parser.add_argument('server_id', nargs='*')
    parser.add_argument('--email', default='fence@example.invalid')
    parser.add_argument('--used-up', type=int, default=7)
    parser.add_argument('--used-down', type=int, default=11)
    parser.add_argument('--total-bytes', type=int, default=1024)
    parser.add_argument('--expiry-time', type=int, default=1893456000)
    parser.add_argument('--latency-ms', type=float, default=250.0)
    parser.add_argument('--inbound-id', type=int, default=1)
    parser.add_argument('--last-update', default='2024-01-01T00:00:00')
    parser.add_argument('--version', default='')
    parser.add_argument('--reason', default='dashboard')
    parser.add_argument('--timeout', type=float, default=20.0)
    parser.add_argument('--poll-interval', type=float, default=0.05)
    args = parser.parse_args()

    ops = {
        'watch-write': op_watch_write,
        'watch-watch': op_watch_watch,
        'watch-control': op_watch_control,
        'fence-write': op_fence_write,
        'fence-read': op_fence_read,
        'fetch-publish': op_fetch_publish,
        'read-snapshot': op_read_snapshot,
        'cleanup': _cleanup,
    }
    handler = ops.get(args.op)
    if handler is None:
        raise SystemExit('child: unknown op %r (known: %s)'
                         % (args.op, ', '.join(sorted(ops))))
    if args.op not in ('cleanup', 'watch-control'):
        _redis()
    if args.op == 'watch-control' and not _NO_REDIS:
        # Without this the control would silently inherit the real Redis and report
        # the server as watched, i.e. it would no longer be a control.
        raise SystemExit('child: watch-control requires --no-redis')
    handler(args)


if __name__ == '__main__':
    main()
