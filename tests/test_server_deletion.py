"""Server deletion preserves Pulse history with foreign keys enforced."""
import os
import tempfile
import unittest
from sqlalchemy import event

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import app, db, Admin, Server  # noqa: E402
from panel.models import PulseRun, PulseResultRecord  # noqa: E402


class ServerDeletionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = app.app_context()
        cls.context.push()
        db.create_all()
        cls.engine = db.engine

        def enforce_foreign_keys(connection, record):
            connection.execute('PRAGMA foreign_keys=ON')

        cls.foreign_keys_listener = enforce_foreign_keys
        event.listen(cls.engine, 'connect', cls.foreign_keys_listener)
        cls.engine.dispose()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        event.remove(cls.engine, 'connect', cls.foreign_keys_listener)
        cls.engine.dispose()
        cls.context.pop()

    def test_delete_preserves_pulse_history_and_other_server(self):
        admin = Admin(username='server-deletion-admin', password_hash='x',
                      role='superadmin', is_superadmin=True)
        server = Server(name='Test panel', host='https://panel.example',
                        username='test', password='test')
        other = Server(name='Other panel', host='https://other.example',
                       username='test', password='test')
        db.session.add_all([admin, server, other])
        db.session.flush()
        run = PulseRun(server_id=server.id, server_name=server.name, status='done')
        other_run = PulseRun(server_id=other.id, server_name=other.name, status='done')
        db.session.add_all([run, other_run])
        db.session.flush()
        result = PulseResultRecord(run_id=run.id, verdict='healthy')
        db.session.add(result)
        db.session.commit()
        server_id, run_id, result_id = server.id, run.id, result.id
        other_id, other_run_id = other.id, other_run.id
        client = app.test_client()
        with client.session_transaction() as session:
            session['admin_id'] = admin.id
            session['username'] = admin.username
            session['role'] = admin.role
        response = client.delete('/api/servers/%d' % server_id)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['success'])
        db.session.expire_all()
        self.assertIsNone(db.session.get(Server, server_id))
        preserved = db.session.get(PulseRun, run_id)
        self.assertIsNone(preserved.server_id)
        self.assertEqual(preserved.server_name, 'Test panel')
        self.assertEqual(db.session.get(PulseResultRecord, result_id).run_id, run_id)
        self.assertEqual(db.session.get(PulseRun, other_run_id).server_id, other_id)
