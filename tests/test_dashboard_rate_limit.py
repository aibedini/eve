"""The dashboard's request budget must fit the dashboard's own polling rate.

One visible dashboard tab asks ``/api/refresh`` every ~10 s: 350-430 requests per
hour and 9-14k per day. The limiter's global default (``5000 per day`` / ``500 per
hour``) is right for the rest of the panel but below that design rate, so a single
admin who left the door open exhausted the daily budget partway through the day.
Every later poll got 429, and the UI rendered that as "Connection Error / Failed to
load data from servers" - indistinguishable from a panel outage - until the window
rolled over up to 24 hours later.

These tests pin both halves of the fix: the polling endpoints get their own budget,
and the browser stops hammering a spent one.
"""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault('SERVER_PASSWORD_KEY',
                      base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from panel.extensions import db, limiter  # noqa: E402
from panel.models import Admin  # noqa: E402
from panel.routes import dashboard as dashboard_routes  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
REFRESH_URL = '/api/refresh?mode=cache&poll=false&wire=2'

#: More than the old default (500/hour), enough to prove the budget moved. 520
#: requests cost ~3 s in this harness, so the boundary stays inside Tier 1.
BURST = 520


class DashboardPollBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        # Counters are process-wide storage; a leftover from another module would
        # make the burst fail for the wrong reason.
        limiter.reset()
        Admin.query.delete()
        db.session.commit()
        admin = Admin(username='rate-limit-admin', password_hash='x',
                      role='superadmin', is_superadmin=True)
        db.session.add(admin)
        db.session.commit()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session.update(admin_id=admin.id,
                                   admin_username=admin.username,
                                   role=admin.role, is_superadmin=True)

    def tearDown(self):
        limiter.reset()

    def test_one_dashboard_tab_can_poll_through_the_old_hourly_default(self):
        codes = [self.client.get(REFRESH_URL).status_code for _ in range(BURST)]
        self.assertNotIn(429, codes,
                         'the dashboard polled past its budget after %d requests: %s'
                         % (codes.count(429), sorted(set(codes))))

    def test_the_limiter_still_limits(self):
        # The fix must not switch rate limiting off: /login keeps its own 10/minute.
        codes = [self.client.get('/login').status_code for _ in range(12)]
        self.assertIn(429, codes, 'a per-route limit stopped being enforced')


class DashboardPollWiringTests(unittest.TestCase):
    """Both polling routes carry the budget; both sides of the client honour it."""

    def _source(self, relative):
        return (REPO / relative).read_text(encoding='utf-8').replace('\r\n', '\n')

    def test_the_two_polling_routes_declare_their_own_budget(self):
        source = self._source('panel/routes/dashboard.py')
        for route, view in (("'/api/refresh'", 'api_refresh'),
                            ("'/api/refresh/stream'", 'api_refresh_stream')):
            block = "@bp.route(%s)\n@limiter.limit(DASHBOARD_POLL_LIMIT)\n@login_required\ndef %s(" % (
                route, view)
            self.assertIn(block, source, '%s lost its poll budget' % view)

    def test_the_budget_is_above_the_designed_polling_rate(self):
        limit = dashboard_routes.DASHBOARD_POLL_LIMIT
        self.assertIn('per hour', limit)
        hourly = int(limit.split('per hour')[0].strip().split(';')[-1])
        # One tab at the default 10 s cadence is ~360/h; several tabs must fit.
        self.assertGreaterEqual(hourly, 360 * 4, limit)
        self.assertLessEqual(hourly, 20000, 'a runaway loop must still be stopped')

    def test_the_client_pauses_instead_of_retrying_a_spent_budget(self):
        live = self._source('static/dashboard_live.js')
        self.assertIn('noteRateLimited(seconds)', live)
        self.assertIn('isRateLimited()', live)
        self.assertIn('clearRateLimit()', live)
        # The poll timer must respect the block, not the cadence.
        self.assertIn('blocked > 0 ? blocked : interval', live)

        dashboard = self._source('static/dashboard.js')
        self.assertIn('response.status === 429', dashboard)
        self.assertIn('dashboardLive.noteRateLimited(', dashboard)
        self.assertIn('dashboardLive.clearRateLimit()', dashboard)
        self.assertIn('if (!force && dashboardLive.isRateLimited())', dashboard)
        # The error card must name the real cause instead of "check your connection".
        self.assertIn("_t('Too many requests')", dashboard)


def _node_script(source, expression):
    node = shutil.which('node')
    if not node:
        raise unittest.SkipTest('node is required to evaluate the poll coordinator')
    handle, path = tempfile.mkstemp(suffix='.cjs', prefix='eve_rate_limit_')
    os.close(handle)
    try:
        with open(path, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(source)
        result = subprocess.run([node, path], capture_output=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError('node failed:\n' + result.stderr.decode('utf-8', 'replace'))
        return json.loads(result.stdout.decode('utf-8', 'replace'))
    finally:
        os.remove(path)


class RateLimitPauseTests(unittest.TestCase):
    """The coordinator parks the timer for the Retry-After window, then resumes."""

    def test_the_poll_timer_waits_for_the_window_instead_of_the_cadence(self):
        script = """
const path = require('path');
const scheduled = [];
const realSetTimeout = global.setTimeout;
global.setTimeout = function (fn, ms) { scheduled.push(ms); return realSetTimeout(fn, ms); };
const { DashboardLiveSync } = require(path.join(%s, 'static', 'dashboard_live.js'));
const live = new DashboardLiveSync({
    hidden: () => false, interval: () => 10000, watched: () => [],
    status: () => {}, reconcile: async () => {}, sseEnabled: false,
});
const out = {};
live.noteRateLimited(120);
out.limited = live.isRateLimited();
out.blockedDelay = scheduled[scheduled.length - 1];
live.clearRateLimit();
out.unblocked = !live.isRateLimited();
out.normalDelay = scheduled[scheduled.length - 1];
live.noteRateLimited(0);                       // no Retry-After header -> 1 minute
out.defaultWait = Math.round((live.rateLimitedUntil - Date.now()) / 1000);
live.noteRateLimited(0);                       // still blocked -> the wait doubles
out.secondWait = Math.round((live.rateLimitedUntil - Date.now()) / 1000);
live.noteRateLimited(0);
out.thirdWait = Math.round((live.rateLimitedUntil - Date.now()) / 1000);
live.noteRateLimited(999999);                  // clamped
out.clampWait = Math.round((live.rateLimitedUntil - Date.now()) / 1000);
live.clearRateLimit();
live.noteRateLimited(0);                       // a success resets the escalation
out.afterResetWait = Math.round((live.rateLimitedUntil - Date.now()) / 1000);
live.pause();
process.stdout.write(JSON.stringify(out));
""" % json.dumps(str(REPO))
        out = _node_script(script, None)
        self.assertTrue(out['limited'])
        self.assertEqual(out['blockedDelay'], 120000,
                         'a 120 s Retry-After must park the poller for 120 s')
        self.assertTrue(out['unblocked'], 'clearing the block must end the pause')
        self.assertEqual(out['normalDelay'], 10000, 'clearing the block restores the cadence')
        self.assertEqual(out['defaultWait'], 60,
                         'with no Retry-After the first pause is a minute')
        self.assertEqual(out['secondWait'], 120,
                         'a second 429 in a row must double the wait, not repeat it')
        self.assertEqual(out['thirdWait'], 240)
        self.assertEqual(out['clampWait'], 3600, 'a bogus header cannot park the panel for a day')
        self.assertEqual(out['afterResetWait'], 60,
                         'a successful poll starts the escalation over')
        self.assertGreaterEqual(out['normalDelay'], 5000,
                                'a spent budget must never collapse into a tight retry loop')


if __name__ == '__main__':
    unittest.main()
