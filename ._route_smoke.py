import json
import os
import tempfile
import unittest

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from app import Admin, app, db  # noqa: E402


class RouteSmoke(unittest.TestCase):
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

    def test_memory_endpoint_returns_the_contract(self):
        response = self.client.get('/api/system/memory')
        print('status', response.status_code)
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'])
        for key in ('host', 'eve', 'snapshot', 'redis_snapshot', 'caches', 'trend', 'health'):
            self.assertIn(key, payload, key)
        print('host.available', payload['host'].get('available'),
              payload['host'].get('reason', ''))
        print('eve.available', payload['eve'].get('available'))
        print('health', payload['health'])
        self.assertNotIn('password', json.dumps(payload).lower())

    def test_anonymous_cannot_read_it(self):
        anon = app.test_client()
        self.assertIn(anon.get('/api/system/memory').status_code, (302, 401, 403))

    def test_deep_analysis_is_guarded_and_bounded(self):
        response = self.client.post('/api/system/memory/analyze', json={'limit': 3})
        print('analyze status', response.status_code)
        payload = response.get_json() or {}
        print('analyze payload keys', sorted(payload.keys()))
        # A step-up guard may legitimately refuse; a refusal must be a clear error, never
        # a traceback and never a silent success.
        if response.status_code == 200:
            self.assertTrue(payload.get('success'))
            self.assertLessEqual(len(payload.get('entries') or []), 3)
        else:
            self.assertIn('error', payload)
            print('refused:', payload.get('error'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
