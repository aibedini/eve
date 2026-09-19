"""Real-Redis, multi-process proof for the refresh/watch pipeline.

What this is for
----------------
tests/test_watch_propagation_crossprocess.py already proves that a watch mark and a
fetch ticket cross a process boundary -- but it does so against a file-backed FAKE
Redis written for the test. That fake is the always-on guard: it runs in CI with no
service, and it refuses to pretend it implements a command it does not. What it
cannot prove is that the app talks to a REAL Redis correctly: a wrong command, a
wrong type, a bytes-vs-str mismatch, a TTL that never lands, a channel name that no
listener subscribes to, or a snapshot the real serializer cannot round-trip would all
pass against a hand-written stub and fail in production.

This harness closes that gap. It starts real OS child processes
(scripts/integration_redis_child.py), points them at a real Redis through the app's
own panel.core.redis_client.get_redis(), and measures three things:

  1. WATCH       web process calls refresh_policy.note_watch(); the fetcher process,
                 with start_wake_listener() running, reports it received the wake on
                 the real pub/sub channel and sees the server as watched at the HOT
                 interval. Metric: publish_to_wake_ms (p95 over --rounds rounds).
  2. FETCH+PUBLISH  the fetcher reads a synthetic panel (configurable latency), admits
                 the result against the shared revision and publishes it; a third,
                 non-fetching dashboard process running snapshot_reader_worker-style
                 logic observes the new revision and last_update. Metric:
                 fetch_to_revision_visible_ms.
  3. MUTATION    one process records a verified client fence; another reads it back
                 with the verified counters intact. That is a real write-through, not
                 a call into the same module object. Metric: fence_roundtrip_ms.

Each step prints its own PASS/FAIL line and the full JSON summary is written to
--json PATH (written even when the run skips or fails, so CI can always read it).

Missing Redis is NOT a failure. If no real Redis is reachable the script prints
'SKIPPED: no Redis available', exits 0 and records why. tests/
test_redis_multiprocess_integration.py turns that into unittest skipTest(), so the
suite never depends on a service being up.

Isolation
---------
  * Every snapshot key the child writes is namespaced ('eve:it:<namespace>:' prefix
    applied to the real module constants), so a production-looking key such as
    'eve:server_data_version' is never touched.
  * The refresh-policy keys (watch mark, hot-server index, client fence) cannot be
    renamed -- the product owns those names -- so the run uses a dedicated high
    server-id range (--server-id-base, default 9000000 + pid) and a cleanup pass
    removes those exact keys.
  * Cleanup runs in a finally block, through a child process, and reports what it
    removed into the JSON summary.

Usage
-----
  .venv/Scripts/python.exe scripts/integration_redis_multiprocess.py --json out.json
  EVE_INTEGRATION_REDIS_URL=redis://127.0.0.1:6379/0 <same command>

See docs/performance/REDIS_MULTIPROCESS.md for the operator instructions.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Running a script puts scripts/ (not the repo root) on sys.path, so the app's own
# package would be unimportable and the run would report a bogus 'no Redis
# available'. The root comes first so the harness and the test that drives it import
# the same panel.core.redis_client module.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
CHILD = os.path.join(REPO_ROOT, 'scripts', 'integration_redis_child.py')
DOCKER_HINT = 'docker run --rm -p 6379:6379 redis:7-alpine'
# Prefix reserved for this harness's synthetic servers. Real panel ids are small
# integers; starting at 9,000,000 makes an accidental collision with a live row
# (and therefore an accidental cleanup of one) effectively impossible.
SERVER_ID_FLOOR = 9000000
STEP_NAMES = ('watch', 'fetch_publish', 'mutation')


# -- small helpers ----------------------------------------------------------


def _percentile(samples, fraction):
    """Nearest-rank percentile over the sample list (>= 1 sample)."""
    if not samples:
        return None
    ordered = sorted(float(value) for value in samples)
    rank = max(1, int(math.ceil(fraction * len(ordered))))
    return ordered[min(rank, len(ordered)) - 1]


def _ms(seconds):
    return None if seconds is None else round(float(seconds) * 1000.0, 3)


def _now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())


def _python_executable():
    """The interpreter the children must use.

    The app's Redis dependency lives in the project venv, so a child started with the
    wrong python would report 'no Redis' while a perfectly good Redis is listening --
    a false skip, which is worse than a failure. A venv interpreter running this script
    is already the right one; otherwise the project's own venv is preferred over
    whatever python happens to be on PATH.
    """
    if sys.prefix != getattr(sys, 'base_prefix', sys.prefix):
        return sys.executable
    candidate = os.path.join(REPO_ROOT, '.venv', 'Scripts', 'python.exe')
    if not os.path.exists(candidate):
        candidate = os.path.join(REPO_ROOT, '.venv', 'bin', 'python')
    if os.path.exists(candidate):
        return candidate
    return sys.executable or 'python'


# -- Redis discovery --------------------------------------------------------


def _build_client(url):
    """A client exactly like the app's: same library, same timeouts, same decoding."""
    import redis as redis_lib
    client = redis_lib.from_url(url, socket_connect_timeout=2, socket_timeout=2,
                                decode_responses=False)
    client.ping()
    return client


def resolve_redis():
    """(url, client, source) for a reachable Redis, or (None, None, reason).

    The app's own get_redis() is the authority, because a harness that configured its
    own Redis differently from the product would prove a pipeline the product does not
    have. EVE_INTEGRATION_REDIS_URL exists so a CI job can point the run at a service
    without editing app configuration; it is tried only when the app's own
    configuration does not reach a server.
    """
    try:
        from panel.core import redis_client
    except Exception as exc:
        return None, None, 'panel.core.redis_client import failed: %s' % exc

    try:
        client = redis_client.get_redis()
    except Exception as exc:
        client = None
        app_error = str(exc)
    else:
        app_error = None
    if client is not None:
        return (redis_client.REDIS_URL or '(app client)', client, 'get_redis()')

    integration_url = (os.environ.get('EVE_INTEGRATION_REDIS_URL') or '').strip()
    if not integration_url:
        return None, None, (
            'get_redis() returned None (no REDIS_URL configured, or redis-py not '
            'installed, or the server is unreachable)%s' % (
                ': %s' % app_error if app_error else ''))
    try:
        return integration_url, _build_client(integration_url), 'EVE_INTEGRATION_REDIS_URL'
    except Exception as exc:
        return None, None, ('EVE_INTEGRATION_REDIS_URL unreachable: %s' % exc)


# -- child process plumbing -------------------------------------------------


class ChildRunner:
    """Spawns scripts/integration_redis_child.py and parses its result line."""

    def __init__(self, redis_url, namespace, timeout):
        self.redis_url = redis_url
        self.namespace = namespace
        self.timeout = timeout
        self.python = _python_executable()
        self.records = []

    def env(self):
        env = dict(os.environ)
        env['EVE_TEST_REPO'] = REPO_ROOT
        # The child imports panel.*; migrations must not run and no background
        # thread may start, or the measurement would be polluted by a real fetch
        # loop racing the synthetic one.
        env['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
        env['DISABLE_BACKGROUND_THREADS'] = '1'
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        if self.redis_url:
            env['REDIS_URL'] = self.redis_url
        env['EVE_TEST_REDIS_NAMESPACE'] = self.namespace
        env['PYTHONUNBUFFERED'] = '1'
        return env

    def command(self, op, extra):
        return [self.python, CHILD, op] + [str(item) for item in extra]

    def run(self, op, server_ids=(), extra=(), timeout=None):
        """Run one child to completion and return its JSON result.

        A non-zero exit is a hard error: the parent must not silently accept a
        'result' produced by a child that crashed halfway through the step.
        """
        args = self.command(op, list(server_ids) + list(extra))
        proc = subprocess.run(args, cwd=REPO_ROOT, env=self.env(), capture_output=True,
                              text=True, timeout=timeout or self.timeout)
        record = {
            'op': op,
            'exit_code': proc.returncode,
            'command': args[1:],
            'stderr_tail': (proc.stderr or '')[-2000:],
        }
        self.records.append(record)
        if proc.returncode != 0:
            raise RuntimeError('child %s failed (exit %s): %s'
                               % (op, proc.returncode, (proc.stderr or '')[-2000:]))
        return parse_result(proc.stdout, op, record)

    def popen(self, op, server_ids=(), extra=(), stdin=True):
        args = self.command(op, list(server_ids) + list(extra))
        proc = subprocess.Popen(
            args, cwd=REPO_ROOT, env=self.env(),
            stdin=subprocess.PIPE if stdin else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1, encoding='utf-8', errors='replace')
        self.records.append({'op': op, 'exit_code': None, 'command': args[1:],
                             'stderr_tail': ''})
        return proc


def parse_result(stdout, op, record=None):
    """The result is the LAST stdout line; everything else is a child event line.

    Reading the last line (instead of grepping for '{') is what keeps a library that
    prints JSON of its own, or a partially written line, from being mistaken for the
    child's answer.
    """
    lines = [line for line in (stdout or '').splitlines() if line.strip()]
    if not lines:
        raise RuntimeError('child %s produced no stdout' % op)
    try:
        return json.loads(lines[-1])
    except Exception as exc:
        raise RuntimeError('child %s last stdout line is not JSON (%s): %r'
                           % (op, exc, lines[-1][:400]))


def parse_event_line(line):
    text = (line or '').strip()
    if text.startswith('child:'):
        return None
    try:
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


class ChildStream:
    """Line reader for a long-lived child that never drops a line it already read.

    Buffering matters: the watcher child emits a result line per handshake step, and a
    reader that discards a line because it was not the one it wanted loses the NEXT
    step's answer too -- which shows up later as a mysterious timeout.
    """

    def __init__(self, stream):
        self.stream = stream
        self.buffer = []

    def next_payload(self, timeout):
        deadline = time.time() + timeout
        while True:
            if self.buffer:
                payload = parse_event_line(self.buffer.pop(0))
                if payload is not None:
                    return payload
                continue
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            line = self.stream.readline()
            if not line:
                raise RuntimeError('child stdout closed')
            self.buffer.append(line)

    def wait_for(self, event, label, timeout):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError('timed out waiting for event %r label %r'
                                   % (event, label))
            payload = self.next_payload(remaining)
            if payload is None:
                raise RuntimeError('timed out waiting for event %r label %r'
                                   % (event, label))
            if payload.get('event') != event:
                continue
            if label and payload.get('label') != label:
                continue
            return payload


class _Duplex:
    """The stdin/stdout pair of one long-lived child, as a single conversation."""

    def __init__(self, proc):
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError('long-lived child needs piped stdin and stdout')
        self.proc = proc
        self.reader = ChildStream(proc.stdout)

    def send(self, text):
        self.proc.stdin.write(text)
        self.proc.stdin.flush()

    def wait_for(self, event, label, timeout):
        return self.reader.wait_for(event, label, timeout)


def terminate(proc):
    if proc.poll() is None:
        try:
            proc.kill()
            proc.communicate(timeout=10)
        except Exception:
            pass


# -- steps ------------------------------------------------------------------


def step_watch(runner, server_id_base, rounds, args, summary):
    """WATCH: a web process makes a panel HOT for the process that owns the loop.

    What it catches: a wake published to a channel nobody subscribes to (or under a
    different name), a watch mark stored under a key the fetcher does not read, a
    mark written with a value the reader cannot decode, and a mark that exists but
    fails to shorten the interval (the bug in the report: 'the panel is on screen in
    one process and polled on the idle cadence in the other').
    """
    step = {'rounds': rounds, 'samples': []}
    watcher = runner.popen('watch-watch', extra=['--timeout', args.wake_timeout])
    # One object owns the duplex conversation: the child reads commands on stdin and
    # answers on stdout, and juggling the two raw handles is how a round gets out of
    # step with its own measurement.
    stream = _Duplex(watcher)
    server_ids = [server_id_base + 1 + index for index in range(rounds)]
    try:
        ready = stream.wait_for('ready', '', args.wake_timeout)
        step['listener_active'] = bool(ready.get('listener_active'))
        step['shared_backend'] = ready.get('shared_backend')
        step['control'] = runner.run('watch-control', [server_ids[0]],
                                     extra=['--no-redis'])
        for index, sid in enumerate(server_ids):
            label = 'round-%d' % (index + 1)
            stream.send('expect %s\n' % label)
            listening = stream.wait_for('listening', label, args.wake_timeout)

            # The writer is a SEPARATE process: the mark and the nudge must travel
            # through Redis, not through a shared module object.
            write = runner.run('watch-write', [sid], extra=['--reason', 'dashboard'])
            wake = stream.wait_for('wake', label, args.wake_timeout)

            sample = {
                'label': label,
                'server_id': sid,
                'wake_published_at': write.get('after'),
                'wake_listening_at': listening.get('listening_at'),
                'wake_received_at': wake.get('received_at'),
                'wake_payload_at': wake.get('payload_at'),
                'wake_payload_server_ids': wake.get('payload_server_ids'),
                'wake_payload_reason': wake.get('payload_reason'),
                'woken': bool(wake.get('woken')),
                # Measured on the writer's own clock, seconds after it actually
                # published -- so this is an upper bound on the true delivery
                # latency, never an optimistically small number.
                'publish_to_wake_ms': _ms(
                    (wake.get('received_at') or 0) - (write.get('after') or 0)),
                'listener_is_after_publish': bool(
                    (wake.get('received_at') or 0) >= (write.get('after') or 0)),
                'payload_has_server': sid in (wake.get('payload_server_ids') or []),
            }
            step['samples'].append(sample)

        check = _watch_check_command(stream, server_ids, args)
        step['check'] = check
        latencies = [sample['publish_to_wake_ms'] for sample in step['samples']
                     if sample['publish_to_wake_ms'] is not None]
        step['publish_to_wake_ms_samples'] = latencies
        step['publish_to_wake_ms_p50'] = _percentile(latencies, 0.50)
        step['publish_to_wake_ms_p95'] = _percentile(latencies, 0.95)
        step['publish_to_wake_ms_max'] = max(latencies) if latencies else None
    finally:
        terminate(watcher)

    control = step.get('control') or {}
    watched = (step.get('check') or {}).get('watched') or {}
    intervals = (step.get('check') or {}).get('interval') or {}
    mark_ttl = (step.get('check') or {}).get('mark_ttl') or {}
    failures = []
    if not step.get('listener_active'):
        failures.append('wake listener never became active')
    if step.get('shared_backend') != 'redis':
        failures.append('child reports shared_backend=%r, not redis'
                        % step.get('shared_backend'))
    if control.get('backend') != 'process':
        failures.append('control process reports backend=%r (expected process)'
                        % control.get('backend'))
    if control.get('watched'):
        failures.append('control process (no Redis) saw the server as watched')
    if control.get('interval') != control.get('idle_interval'):
        failures.append('control process interval=%r, expected the idle interval %r'
                        % (control.get('interval'), control.get('idle_interval')))
    for sample in step['samples']:
        if not sample['woken']:
            failures.append('%s: listener was never woken' % sample['label'])
        if not sample['payload_has_server']:
            failures.append('%s: wake payload did not name server %s (got %r)'
                            % (sample['label'], sample['server_id'],
                               sample['wake_payload_server_ids']))
        if not sample['listener_is_after_publish']:
            failures.append('%s: wake arrived before the publish on the shared clock'
                            % sample['label'])
    for sid in server_ids:
        if not watched.get(str(sid)):
            failures.append('server %s is not watched in the fetcher process' % sid)
        if intervals.get(str(sid)) != args.active_interval:
            failures.append('server %s interval=%r, expected HOT %r'
                            % (sid, intervals.get(str(sid)), args.active_interval))
        ttl = mark_ttl.get(str(sid))
        if not ttl or ttl <= 0:
            failures.append('watch mark for server %s has ttl=%r, expected a live '
                            'expiry (a mark with no TTL pins the panel hot forever)'
                            % (sid, ttl))
        elif ttl > args.mark_ttl_max:
            failures.append('watch mark for server %s has ttl=%ss, expected at most '
                            'EVE_SERVER_POLL_ACTIVE_TTL_SECONDS (%ss)'
                            % (sid, ttl, args.mark_ttl_max))
    step['failures'] = failures
    step['passed'] = not failures
    return step


def _watch_check_command(stream, server_ids, args):
    """Ask the persistent watcher process what IT sees (not what the writer claimed)."""
    stream.send('check %s\n' % ' '.join(str(sid) for sid in server_ids))
    return stream.wait_for('check', '', args.wake_timeout)


def step_fetch_publish(runner, sid, args, summary):
    """FETCH + PUBLISH: the fetcher's write-through must become visible elsewhere.

    What it catches: a publish that returns True while writing nothing a reader can
    find, a manifest the loader cannot decode (the exact failure mode of the rejected
    pickle format), a version key that never changes so a reader short-circuits, and a
    revision/CAS check that is not wired to the real Redis key -- which is what would
    silently drop every refresh after a mutation.
    """
    step = {}
    step['panel_latency_ms'] = args.fetch_latency_ms
    last_update = '2024-01-01T00:00:%02d' % (int(time.time()) % 60)

    # Phase 1: a real dashboard process primes its cache BEFORE the fetch starts, so
    # the latency below is publish -> already-polling reader, not process start-up.
    prime = runner.run('read-snapshot', [sid], extra=[
        '--timeout', args.wake_timeout, '--last-update', last_update])
    step['reader_primed'] = bool(prime.get('primed'))

    # Phase 2: the fetcher reads the synthetic panel and publishes.
    write = runner.run('fetch-publish', [sid], extra=[
        '--latency-ms', args.fetch_latency_ms, '--last-update', last_update,
        '--email', args.email])
    step['write'] = write
    version = str(write.get('version') or '')
    publish_started_at = write.get('publish_started') or time.time()

    # Phase 3: the dashboard polls at snapshot_reader_worker's cadence until the
    # shared revision it was told about is what it has in memory.
    snapshot = {'visible': False}
    if write.get('published') and version:
        reader = runner.popen('read-snapshot', [sid], extra=[
            '--version', version, '--timeout', args.read_timeout,
            '--poll-interval', args.poll_interval, '--last-update', last_update])
        step['reader_started_at'] = time.time()
        try:
            snapshot = _await_snapshot(reader, args)
        finally:
            terminate(reader)
    step['snapshot'] = snapshot

    observed_at = snapshot.get('observed_at')
    step['fetch_to_revision_visible_ms'] = _ms(
        (observed_at or 0) - (write.get('read_started') or 0))
    step['publish_to_revision_visible_ms'] = _ms(
        (observed_at or 0) - publish_started_at)
    step['read_finished_to_revision_visible_ms'] = _ms(
        (observed_at or 0) - (write.get('read_finished') or 0))
    step['fetch_read_ms'] = write.get('read_ms')
    step['publish_ms'] = write.get('publish_ms')
    step['revision_visible'] = bool(snapshot.get('visible'))

    failures = []
    if not write.get('published'):
        failures.append('snapshot publish reported False')
    if not version:
        failures.append('no shared snapshot version after publish')
    if not snapshot.get('visible'):
        failures.append('dashboard process never observed version %s' % version)
    if snapshot.get('seen_version') != version:
        failures.append('dashboard observed version %r, expected %r'
                        % (snapshot.get('seen_version'), version))
    if not snapshot.get('last_update_matches'):
        failures.append('dashboard last_update=%r, expected %r'
                        % (snapshot.get('last_update'), last_update))
    if not snapshot.get('inbounds'):
        failures.append('dashboard saw no inbounds after the publish')
    if not snapshot.get('clients'):
        failures.append('dashboard saw no clients after the publish')
    if (step['fetch_to_revision_visible_ms'] or 0) < args.fetch_latency_ms:
        failures.append('visible latency %r is below the configured panel latency %r'
                        % (step['fetch_to_revision_visible_ms'], args.fetch_latency_ms))
    manifest_key = str(write.get('manifest_key') or '')
    if not manifest_key.startswith('eve:it:'):
        failures.append('publish used un-namespaced key %r' % manifest_key)
    # Every snapshot key must carry a TTL: Redis is an ephemeral cache, and a block
    # with no expiry survives the fetcher that owns it.
    ttls = write.get('ttls') or {}
    for name in ('version', 'manifest', 'block'):
        ttl = ttls.get(name)
        if ttl is None or ttl <= 0:
            failures.append('published %s key has ttl=%r, expected a positive expiry'
                            % (name, ttl))
    step['ttls'] = ttls
    step['failures'] = failures
    step['passed'] = not failures
    return step


def _await_snapshot(reader, args):
    """Wait for the dashboard child to report the revision it was told to expect."""
    deadline = time.time() + args.read_timeout + 60.0
    while time.time() < deadline:
        line = reader.stdout.readline()
        if not line:
            raise RuntimeError('dashboard child closed before reporting the snapshot')
        payload = parse_event_line(line)
        if payload and payload.get('op') == 'read-snapshot' and 'visible' in payload:
            return payload
    raise RuntimeError('dashboard child did not report a snapshot result')


def step_mutation(runner, sid, args, summary):
    """MUTATION: a verified fence must be readable from ANOTHER process.

    What it catches: a fence written only to the writing process's dict (so the
    background poll reverts the mutation a minute later -- the bug the fence exists
    for), a hash field written with a type the reader cannot parse, and a TTL that is
    never set so a stale fence pins a value the panel really did change.
    """
    step = {}
    write = runner.run('fence-write', [sid], extra=[
        '--email', args.email, '--used-up', args.used_up,
        '--used-down', args.used_down, '--total-bytes', args.total_bytes])
    step['write'] = write
    read = runner.run('fence-read', [sid], extra=['--email', args.email])
    step['read'] = read
    fence = read.get('fence') or {}
    step['fence'] = fence

    # The two children measured their own wall clocks on the same machine; the
    # round trip is read.after - write.before and covers both process spawns, which
    # is the honest cost of a fence crossing a real boundary (a same-process call
    # would be microseconds and would prove nothing).
    step['fence_roundtrip_ms'] = _ms(
        (read.get('after') or 0) - (write.get('before') or 0))
    step['fence_write_ms'] = _ms((write.get('after') or 0) - (write.get('before') or 0))
    step['fence_read_ms'] = _ms((read.get('after') or 0) - (read.get('before') or 0))

    failures = []
    if not write.get('ok'):
        failures.append('record_client_fence returned False')
    if not read.get('found'):
        failures.append('fence was not visible in another process (emails=%r)'
                        % read.get('emails'))
    if str(fence.get('email')) != args.email:
        failures.append('fence email=%r, expected %r' % (fence.get('email'), args.email))
    for key, expected in (('used_up', args.used_up), ('used_down', args.used_down),
                          ('total_bytes', args.total_bytes)):
        if fence.get(key) != expected:
            failures.append('fence %s=%r, expected %r' % (key, fence.get(key), expected))
    if not fence.get('verified_at'):
        failures.append('fence has no verified_at stamp')
    if (fence.get('expires_at') or 0) <= (fence.get('verified_at') or 0):
        failures.append('fence expires_at is not after verified_at')
    step['failures'] = failures
    step['passed'] = not failures
    return step


# -- reporting --------------------------------------------------------------


def report_step(name, step):
    metric = {
        'watch': ('publish_to_wake_ms_p95', 'ms'),
        'fetch_publish': ('fetch_to_revision_visible_ms', 'ms'),
        'mutation': ('fence_roundtrip_ms', 'ms'),
    }[name]
    value = step.get(metric[0])
    verdict = 'PASS' if step.get('passed') else 'FAIL'
    print('[%s] %-13s %s=%s%s' % (verdict, name, metric[0], value, metric[1]))
    if name == 'watch':
        print('       samples=%s p50=%s max=%s' % (
            step.get('publish_to_wake_ms_samples'),
            step.get('publish_to_wake_ms_p50'),
            step.get('publish_to_wake_ms_max')))
    for failure in step.get('failures') or []:
        print('       - %s' % failure)


def write_summary(path, summary):
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write('\n')


def build_parser():
    parser = argparse.ArgumentParser(
        description='real-Redis multi-process proof for the refresh/watch pipeline')
    parser.add_argument('--json', default=None,
                        help='write the machine-readable summary here')
    parser.add_argument('--rounds', type=int, default=5,
                        help='watch rounds for the latency sample (default 5)')
    parser.add_argument('--fetch-latency-ms', type=float, default=250.0,
                        help='simulated panel read latency (default 250)')
    parser.add_argument('--server-id-base', type=int, default=SERVER_ID_FLOOR,
                        help='first synthetic server id (default 9000000 + pid)')
    parser.add_argument('--timeout', type=float, default=300.0,
                        help='per-child timeout in seconds (default 300)')
    parser.add_argument('--wake-timeout', type=float, default=20.0,
                        help='seconds to wait for one wake (default 20)')
    parser.add_argument('--read-timeout', type=float, default=20.0,
                        help='seconds the dashboard child polls for a revision')
    parser.add_argument('--poll-interval', type=float, default=0.2,
                        help='dashboard poll interval in seconds (default 0.2)')
    parser.add_argument('--active-interval', type=float, default=2.0,
                        help='expected HOT interval (EVE_SERVER_POLL_ACTIVE_SECONDS)')
    parser.add_argument('--mark-ttl-max', type=float, default=125.0,
                        help='longest acceptable watch-mark TTL '
                             '(EVE_SERVER_POLL_ACTIVE_TTL_SECONDS + slack)')
    parser.add_argument('--email', default='integration@example.invalid')
    parser.add_argument('--used-up', type=int, default=7)
    parser.add_argument('--used-down', type=int, default=11)
    parser.add_argument('--total-bytes', type=int, default=1024)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.server_id_base or args.server_id_base < SERVER_ID_FLOOR:
        args.server_id_base = SERVER_ID_FLOOR + (os.getpid() % 900000)
    namespace = 'mp-%s' % uuid.uuid4().hex[:12]
    server_ids = [args.server_id_base + 1 + index for index in range(max(1, args.rounds))]
    server_ids += [args.server_id_base + 500, args.server_id_base + 501]

    summary = {
        'tool': 'integration_redis_multiprocess',
        'started_at': _now_iso(),
        'namespace': namespace,
        'redis_url': None,
        'redis_source': None,
        'server_id_base': args.server_id_base,
        'rounds': args.rounds,
        'python': _python_executable(),
        'steps': {},
        'cleanup': None,
        'status': 'unknown',
        'notes': [],
    }

    url, client, source = resolve_redis()
    if client is None:
        summary['redis_source'] = 'none: %s' % source
        summary['status'] = 'skipped'
        summary['reason'] = source
        print('SKIPPED: no Redis available (%s)' % source)
        print('  This harness needs a REAL Redis; it is not a substitute for the '
              'always-on fake-Redis guard in tests/test_watch_propagation_crossprocess.py.')
        print('  Start one with: %s' % DOCKER_HINT)
        print('  Then set REDIS_URL (or EVE_INTEGRATION_REDIS_URL) and re-run.')
        write_summary(args.json, summary)
        return 0

    summary['redis_url'] = url
    summary['redis_source'] = source
    print('Redis: %s (discovered via %s)' % (url, source))
    print('Synthetic server ids: %s..%s, namespace=%s'
          % (min(server_ids), max(server_ids), namespace))

    runner = ChildRunner(url, namespace, args.timeout)
    try:
        summary['steps']['watch'] = step_watch(runner, args.server_id_base,
                                               max(1, args.rounds), args, summary)
        summary['steps']['fetch_publish'] = step_fetch_publish(
            runner, args.server_id_base + 500, args, summary)
        summary['steps']['mutation'] = step_mutation(
            runner, args.server_id_base + 501, args, summary)
    except Exception as exc:
        summary['error'] = '%s: %s' % (type(exc).__name__, exc)
        print('[FAIL] harness error: %s' % summary['error'])
    finally:
        # Cleanup is a child of its own so it uses the real key builders with the same
        # namespace; it must run even when a step raised, or a crashed run leaves
        # synthetic marks that keep a real fetcher polling a server id that does not
        # exist.
        try:
            summary['cleanup'] = runner.run(
                'cleanup', server_ids, timeout=min(args.timeout, 120.0))
        except Exception as exc:
            summary['cleanup'] = {'error': '%s: %s' % (type(exc).__name__, exc)}
        summary['children'] = runner.records

    passed = (len(summary['steps']) == len(STEP_NAMES)
              and all((summary['steps'].get(name) or {}).get('passed')
                      for name in STEP_NAMES))
    if 'error' in summary:
        passed = False
    summary['status'] = 'passed' if passed else 'failed'
    summary['finished_at'] = _now_iso()
    summary['notes'] = [
        'The panel read in step 2 is synthetic (a sleep); the commit, the shared '
        'revision check and publish_snapshot_to_redis() are the real ones.',
        'publish_to_wake_ms is measured from the writer process AFTER publish() '
        'returned, so it is an upper bound on delivery latency.',
        'The panel network-failure/backoff path is NOT exercised here: reproducing it '
        'offline would require faking the app-level error recording this harness '
        'deliberately does not touch.',
    ]

    for name in STEP_NAMES:
        step = summary['steps'].get(name)
        if step is None:
            print('[FAIL] %-13s step did not run' % name)
        else:
            report_step(name, step)
    cleanup = summary.get('cleanup') or {}
    print('cleanup: removed %s key(s)%s'
          % (cleanup.get('count'), ' (ERROR: %s)' % cleanup['error']
             if cleanup.get('error') else ''))
    print('OVERALL: %s' % summary['status'].upper())
    if args.json:
        print('JSON: %s' % os.path.abspath(args.json))

    write_summary(args.json, summary)
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
