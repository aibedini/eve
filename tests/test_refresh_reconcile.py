"""Phase 5: the Refresh button must be a real, tracked reconcile.

The click enqueues a full refresh, the response carries a job id, the browser polls
`/api/refresh/job/<id>` until the job is done or failed, and only then applies the new
snapshot revision. A spinner with no relation to the job would let an operator believe a
reconcile happened when it did not.
"""
import os
import tempfile
import unittest

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
import panel.jobs.refresh as refresh_jobs  # noqa: E402
from app import Admin, Server, db  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO_ROOT, 'templates', 'dashboard.html')


class RefreshJobEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(username='refresh-op', password_hash='x', role='superadmin',
                           is_superadmin=True)
        db.session.add(self.admin)
        db.session.commit()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
            sess['role'] = 'superadmin'
            sess['is_superadmin'] = True

    def test_the_click_returns_a_trackable_job(self):
        # The real loop, unmocked: enqueue -> id -> the endpoint tracks that id.
        resp = self.client.get('/api/refresh?mode=full&enqueue=1')

        payload = resp.get_json()
        self.assertTrue(payload['success'], payload)
        job = payload.get('refresh_job')
        self.assertIsNotNone(job, payload)
        self.assertTrue(job.get('id'), job)
        self.assertIn(job.get('state'), ('queued', 'running', 'done', 'error'), job)
        self.assertEqual(job.get('mode'), 'full')

        tracked = self.client.get(f"/api/refresh/job/{job['id']}").get_json()
        self.assertTrue(tracked['success'], tracked)
        self.assertEqual(tracked['job']['id'], job['id'])

    def test_the_job_endpoint_reports_progress_then_completion(self):
        job = {'id': 'job-refresh-2', 'state': 'running', 'mode': 'full', 'server_id': None,
               'force': True, 'progress': {'total': 2, 'processed': 1}}
        refresh_jobs._store_refresh_job(job)

        resp = self.client.get('/api/refresh/job/job-refresh-2')
        payload = resp.get_json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['job']['state'], 'running')
        self.assertEqual(payload['job']['progress'], {'total': 2, 'processed': 1})
        self.assertIn('no-store', resp.headers.get('Cache-Control', ''))

        job['state'] = 'done'
        refresh_jobs._store_refresh_job(job)
        done = self.client.get('/api/refresh/job/job-refresh-2').get_json()
        self.assertEqual(done['job']['state'], 'done')

    def test_a_failed_job_reports_its_error(self):
        refresh_jobs._store_refresh_job({'id': 'job-refresh-3', 'state': 'error',
                                         'mode': 'full', 'error': 'panel unreachable'})
        payload = self.client.get('/api/refresh/job/job-refresh-3').get_json()
        self.assertEqual(payload['job']['state'], 'error')
        self.assertEqual(payload['job']['error'], 'panel unreachable')

    def test_an_unknown_job_is_not_found(self):
        self.assertEqual(self.client.get('/api/refresh/job/does-not-exist').status_code, 404)


class RefreshButtonWiringTests(unittest.TestCase):
    """The template half: the click, the tracking, and what happens on completion."""

    @classmethod
    def setUpClass(cls):
        with open(DASHBOARD, encoding='utf-8') as handle:
            cls.dashboard = handle.read()

    def test_the_button_runs_a_full_reconcile(self):
        self.assertIn('onclick="refreshData()"', self.dashboard)
        # refreshData() without options is not silent -> mode 'full' -> enqueue.
        self.assertIn("(isSilent ? 'cache' : 'full')", self.dashboard)
        self.assertIn("(mode !== 'cache')", self.dashboard)

    def test_the_ui_polls_the_job_it_was_given(self):
        self.assertIn('/api/refresh/job/${encodeURIComponent(activeRefreshJobId)}', self.dashboard)
        self.assertIn("const jobId = data.refresh_job && data.refresh_job.id;", self.dashboard)
        self.assertIn('startRefreshJobPolling(jobId', self.dashboard)

    def test_both_terminal_states_are_handled(self):
        self.assertIn("state === 'done'", self.dashboard)
        self.assertIn("state === 'error'", self.dashboard)
        self.assertIn('handleRefreshError()', self.dashboard)

    def test_the_new_revision_is_applied_after_completion(self):
        self.assertIn("await refreshData(true, { mode: 'cache', poll: false });", self.dashboard)


if __name__ == '__main__':
    unittest.main()

