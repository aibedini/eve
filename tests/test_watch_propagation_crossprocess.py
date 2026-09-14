"""Cross-PROCESS proof that a dashboard watch reaches the polling loop.

The bug this file exists for: the browser talks to a WEB process, the fetch loop
lives in the BACKGROUND process, and per-server watch state used to be a plain dict
in module memory. Opening a dashboard therefore made a panel hot in a process that
does not poll, while the loop that does poll kept its idle interval -- and no test
that stays inside one process can see that, which is why this one starts REAL child
processes (tests/_crossprocess_child.py) that share a Redis.

Each mechanism is asserted twice: with the shared backend a mark or a ticket set by
process A is visible to process B, and WITHOUT it the very same code in the same
shape sees nothing. The second case is the control that turns the first into
evidence: it is the old behaviour, reproduced on demand.

Redis is not required on the test machine -- the children install a small
file-backed client that implements exactly the calls the code makes, including a
faithful compare-and-set for the ticket script (and it refuses to fake a script it
does not recognise). The real backend is covered by the live production proof
recorded in docs/TELEMETRY_STATE_TRANSITIONS.md.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHILD = os.path.join(REPO_ROOT, 'tests', '_crossprocess_child.py')


class CrossProcessPropagationTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        handle.close()
        self.store = handle.name
        os.unlink(self.store)

    def tearDown(self):
        for suffix in ('', '.tmp', '.lock'):
            try:
                os.unlink(self.store + suffix)
            except OSError:
                pass

    def _run(self, op, sid, ticket=None, shared=True):
        env = dict(os.environ)
        env['EVE_TEST_REPO'] = REPO_ROOT
        env['EVE_TEST_REDIS_FILE'] = self.store
        env['EVE_TEST_SHARED'] = '1' if shared else '0'
        env['DISABLE_BACKGROUND_THREADS'] = '1'
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        args = [sys.executable, CHILD, op, str(sid)]
        if ticket is not None:
            args.append(str(ticket))
        proc = subprocess.run(args, cwd=REPO_ROOT, env=env, capture_output=True,
                              text=True, timeout=240)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_a_watch_mark_crosses_the_process_boundary(self):
        writer = self._run('watch-set', 77)
        self.assertEqual(writer['backend'], 'redis')
        self.assertEqual(writer['shared_backend'], 'redis')
        reader = self._run('watch-check', 77)
        self.assertTrue(reader['watched'])
        self.assertIn(77, reader['ids'])
        # Knowing about the mark is not enough: the loop must SCHEDULE it fast.
        self.assertEqual(reader['interval'], 2.0)
        self.assertEqual(reader['idle_interval'], 45.0)

    def test_without_the_shared_backend_the_mark_does_not_cross(self):
        writer = self._run('watch-set', 78, shared=False)
        self.assertEqual(writer['backend'], 'process')
        reader = self._run('watch-check', 78, shared=False)
        self.assertFalse(reader['watched'])
        self.assertNotIn(78, reader['ids'])
        self.assertEqual(reader['interval'], 45.0)

    def test_fetch_tickets_are_monotonic_across_processes(self):
        first = self._run('seq-begin', 91)
        self.assertEqual(first['ticket'], 1)
        self.assertTrue(self._run('seq-accept', 91, first['ticket'])['accepted'])
        second = self._run('seq-begin', 91)
        self.assertEqual(second['ticket'], 2)
        self.assertTrue(self._run('seq-accept', 91, second['ticket'])['accepted'])
        # A third process arrives carrying the OLDER ticket (a slow panel read that
        # started before the newer one). It must be refused, and the shared watermark
        # must not move backwards.
        stale = self._run('seq-accept', 91, first['ticket'])
        self.assertFalse(stale['accepted'])
        self.assertEqual(stale['last'], 2)

    def test_a_local_only_process_cannot_see_another_process_ticket(self):
        # Control for the ticket CAS: without the shared backend each process owns
        # its own counter, so the same lower ticket is accepted again -- exactly the
        # reordering the sequence exists to prevent.
        self._run('seq-accept', 92, 5, shared=False)
        stale = self._run('seq-accept', 92, 3, shared=False)
        self.assertTrue(stale['accepted'])


if __name__ == '__main__':
    unittest.main()
