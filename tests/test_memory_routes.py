"""The memory endpoint's route contract: reachable by a superadmin, shut to everyone else.

The module that produces the payload is unit-tested next door (`test_memory_report.py`),
but nothing exercised the route itself - the only file that did was `._route_smoke.py` at
the repository root, whose name (macOS AppleDouble convention) keeps unittest discovery
from ever running it. These tests are that file's contract, promoted to where it runs, plus
the keys the attribution gained afterwards (`accounting`, `eve.services`).
"""
import json
import os
import tempfile
import unittest

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
# Importing the app otherwise runs the migration runner, which needs a real Alembic (the
# repository's own alembic/ scaffold shadows the package when it is not installed). The
# suite's own runner sets this too; the route contract does not depend on migrations.
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')

import app as app_module  # noqa: E402,F401  (importing it is what builds the app)
from app import Admin, app, db  # noqa: E402

REQUIRED_KEYS = ('host', 'eve', 'accounting', 'snapshot', 'snapshot_copies',
                 'redis_snapshot', 'caches', 'trend', 'health')


class MemoryRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(username='mem-admin', role='superadmin', is_superadmin=True,
                           enabled=True)
        self.admin.set_password('CorrectHorseBattery1!')
        db.session.add(self.admin)
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = self.admin.id
            sess['role'] = self.admin.role
            sess['is_superadmin'] = True

    def test_the_payload_carries_the_whole_contract(self):
        response = self.client.get('/api/system/memory')
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'])
        for key in REQUIRED_KEYS:
            self.assertIn(key, payload, key)
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
        # The two groupings the attribution is read through, and the reconciliation.
        self.assertIn('roles', payload['eve'])
        self.assertIn('services', payload['eve'])
        self.assertIn('total_bytes', payload['accounting'])
        # "How many full snapshot copies exist" travels in its own block, so it is present
        # even when this process holds no snapshot of its own.
        self.assertIn('available', payload['snapshot_copies'])

    def test_the_payload_carries_no_credentials_or_customer_data(self):
        payload = self.client.get('/api/system/memory').get_json()
        text = json.dumps(payload).lower()
        for forbidden in ('password', 'token', 'secret', 'api_key', 'authorization'):
            self.assertNotIn(forbidden, text)

    def test_anonymous_and_non_superadmin_cannot_read_it(self):
        self.assertIn(app.test_client().get('/api/system/memory').status_code,
                      (302, 401, 403))
        plain = Admin(username='mem-admin2', role='admin', is_superadmin=False, enabled=True)
        plain.set_password('CorrectHorseBattery1!')
        db.session.add(plain)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = plain.id
            sess['role'] = plain.role
            sess['is_superadmin'] = False
        self.assertIn(client.get('/api/system/memory').status_code, (302, 401, 403))

    def test_deep_analysis_is_guarded_and_bounded(self):
        response = self.client.post('/api/system/memory/analyze', json={'limit': 3})
        payload = response.get_json() or {}
        # The step-up guard may legitimately refuse. A refusal must be a clear, non-200
        # answer - never a traceback and never a silent success.
        if response.status_code == 200:
            self.assertTrue(payload.get('success'))
            self.assertLessEqual(len(payload.get('entries') or []), 3)
        else:
            self.assertIn(response.status_code, (400, 401, 403, 503), payload)


if __name__ == '__main__':
    unittest.main(verbosity=2)
