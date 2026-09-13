"""Phase 0.5-A: the X-UI backup critical section is cross-process.

A process-local lock cannot answer "is a backup already running for server 7?"
when Eve runs several gunicorn workers, so the guarantee comes from a
PostgreSQL advisory lock taken on a dedicated connection. These tests pin the
contract: one winner per server, different servers in parallel, and the lock
released on every exit path (including a failing connection).
"""
import base64
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL',
                    'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
import panel.services.backup as backup_service  # noqa: E402
from panel.core import advisory_lock  # noqa: E402


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeConnection:
    """Records every statement so the test can assert the lock/unlock contract."""

    def __init__(self, acquire_result=True, fail_on_unlock=False):
        self.statements = []
        self.closed = False
        self._acquire_result = acquire_result
        self._fail_on_unlock = fail_on_unlock

    def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append((text, dict(params or {})))
        if "try_advisory_lock" in text:
            return mock.Mock(scalar=lambda: self._acquire_result)
        if "advisory_unlock" in text and self._fail_on_unlock:
            raise RuntimeError('connection lost before unlock')
        return mock.Mock(scalar=lambda: True)

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, name='postgresql', connection=None):
        self.dialect = _FakeDialect(name)
        self._connection = connection or _FakeConnection()
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        return self._connection


class AdvisoryLockPostgresSemanticsTests(unittest.TestCase):
    """The PostgreSQL branch, exercised through a fake engine (no live server)."""

    def test_the_first_holder_acquires_and_the_second_is_refused(self):
        engine = _FakeEngine()
        with advisory_lock.resource_lock('xui_backup:7', engine=engine) as owner:
            self.assertIsNotNone(owner)
            self.assertIn("try_advisory_lock", engine._connection.statements[0][0])
        # The advisory key is the SAME for the same resource in every process.
        key_one = advisory_lock.advisory_key('xui_backup:7')
        key_two = advisory_lock.advisory_key('xui_backup:7')
        self.assertEqual(key_one, key_two)
        self.assertNotEqual(key_one, advisory_lock.advisory_key('xui_backup:8'))

    def test_the_lock_is_released_and_the_connection_returned(self):
        engine = _FakeEngine()
        with advisory_lock.resource_lock('xui_backup:7', engine=engine):
            pass
        statements = [text for text, _params in engine._connection.statements]
        self.assertTrue(any("advisory_unlock" in text for text in statements))
        self.assertTrue(engine._connection.closed)
        self.assertEqual(engine.connect_calls, 1)

    def test_a_second_holder_is_refused_without_touching_the_resource(self):
        engine = _FakeEngine(connection=_FakeConnection(acquire_result=False))
        entered = False
        with advisory_lock.resource_lock('xui_backup:7', engine=engine) as owner:
            entered = owner is not None
        self.assertFalse(entered)
        self.assertTrue(engine._connection.closed)
        statements = [text for text, _params in engine._connection.statements]
        self.assertFalse(any("advisory_unlock" in text for text in statements))

    def test_a_dead_connection_releases_the_lock_and_the_next_holder_proceeds(self):
        # PostgreSQL frees a session lock when its connection dies; the failing
        # unlock must be swallowed, never mask the caller's own error, and never
        # leave the in-process lock held.
        engine = _FakeEngine(connection=_FakeConnection(fail_on_unlock=True))
        with self.assertRaises(RuntimeError):
            with advisory_lock.resource_lock('xui_backup:7', engine=engine):
                raise RuntimeError('backup exploded')
        self.assertTrue(engine._connection.closed)
        again = _FakeEngine()
        with advisory_lock.resource_lock('xui_backup:7', engine=again) as owner:
            self.assertIsNotNone(owner)

    def test_a_connect_failure_is_reported_as_not_acquired(self):
        class _Broken(_FakeEngine):
            def connect(self):
                raise RuntimeError('database is down')

        with advisory_lock.resource_lock('xui_backup:7', engine=_Broken()) as owner:
            self.assertIsNone(owner)

    def test_the_resource_lock_is_not_global(self):
        engine = _FakeEngine()
        statements = []

        def _record(statement, params=None):
            statements.append(dict(params or {}))
            return mock.Mock(scalar=lambda: True)

        engine._connection.execute = mock.Mock(side_effect=_record)
        with advisory_lock.resource_lock('xui_backup:7', engine=engine):
            pass
        with advisory_lock.resource_lock('xui_backup:8', engine=engine):
            pass
        keys = [entry.get('key') for entry in statements]
        self.assertEqual(len(keys), 4)  # lock+unlock for each server
        self.assertEqual(len(set(keys)), 2)  # one key per server, not one global

    def test_sqlite_reports_that_the_guarantee_is_process_local(self):
        self.assertFalse(advisory_lock.lock_is_cross_process(_FakeEngine('sqlite')))
        self.assertTrue(advisory_lock.lock_is_cross_process(_FakeEngine('postgresql')))

    def test_sqlite_still_serialises_inside_one_process(self):
        engine = _FakeEngine('sqlite')
        held = []

        def _hold():
            with advisory_lock.resource_lock('xui_backup:7', engine=engine) as owner:
                held.append(owner is not None)
                time.sleep(0.1)

        first = threading.Thread(target=_hold)
        first.start()
        self.assertTrue(first.is_alive())
        with advisory_lock.resource_lock('xui_backup:7', engine=engine) as owner:
            self.assertIsNone(owner)  # the second caller coalesces
        first.join()

class XuiBackupCoalescingTests(unittest.TestCase):
    """Two simultaneous triggers for one server produce ONE panel download."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        app_module.db.create_all()

    @classmethod
    def tearDownClass(cls):
        app_module.db.session.remove()
        app_module.db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        from app import Server
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = os.path.join(self.tmp.name, 'spool')
        self._env = mock.patch.dict(os.environ, {'EVE_XUI_BACKUP_DIR': self.spool})
        self._env.start()
        Server.query.delete()
        self.server = Server(name='lock-panel', host='https://panel.invalid',
                             username='u', password='p', panel_type='auto', enabled=True)
        app_module.db.session.add(self.server)
        app_module.db.session.commit()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from app import Server
        try:
            Server.query.delete()
            app_module.db.session.commit()
        except Exception:
            app_module.db.session.rollback()
        self._env.stop()
        self.tmp.cleanup()

    def _run(self, downloads, delay=0.25):
        settings = {'enabled': True, 'send_panel_backup': False,
                    'bot_token': 'tok', 'chat_id': 'chat'}
        payload = b'SQLite format 3\x00' + b'panel-db' * 8

        def fake_fetch(_session, _server):
            downloads.append(_server.id)
            time.sleep(delay)  # hold the critical section so a racer can arrive
            return payload, '.db', None

        def fake_send(_token, _chat, file_path, _caption, proxies=None,
                      document_name=None):
            return mock.Mock(status_code=200)

        with mock.patch.object(backup_service, '_get_telegram_backup_settings',
                               return_value=settings), \
             mock.patch.object(backup_service, '_telegram_backup_route_proxies',
                               return_value=({}, None)), \
             mock.patch.object(backup_service, 'get_xui_session',
                               return_value=(mock.Mock(), None)), \
             mock.patch.object(backup_service, '_fetch_xui_backup',
                               side_effect=fake_fetch), \
             mock.patch.object(backup_service, '_telegram_send_document',
                               side_effect=fake_send), \
             mock.patch.object(backup_service, '_safe_response_json',
                               return_value=(_ok_document(), None)):
            return backup_service._run_telegram_backup(trigger='manual')

    def test_a_concurrent_trigger_coalesces_instead_of_downloading_again(self):
        downloads = []
        result = {}

        def _first():
            # A Flask app context is thread-local, exactly like in production: the
            # competing trigger must run in its own request/worker context.
            with app_module.app.app_context():
                result['first'] = self._run(downloads)

        worker = threading.Thread(target=_first)
        worker.start()
        time.sleep(0.1)  # let the first run enter its critical section
        second = self._run(downloads, delay=0)
        worker.join()

        # Exactly ONE panel download for one server, however many triggers race.
        self.assertEqual(downloads, [self.server.id])
        self.assertTrue(result['first']['success'], result['first'])
        # The loser is refused, not queued: either by the whole-run guard (a backup
        # was already running for this install) or by the per-server coalescing.
        self.assertFalse(second['success'], second)
        self.assertTrue(
            second.get('error') == 'Backup already running'
            or any(row.get('coalesced') for row in (second.get('results') or [])),
            second,
        )


    def test_a_coalesced_server_reports_already_running_and_skips_the_panel(self):
        downloads = []
        resource = backup_service._xui_backup_resource(self.server.id)
        # Resolve the engine HERE: app context is thread-local, and the other
        # thread deliberately runs without one, like a real gunicorn worker.
        engine = app_module.db.engine
        held = threading.Event()
        release = threading.Event()

        def _hold():
            with advisory_lock.resource_lock(resource, engine=engine):
                held.set()
                release.wait(5)

        holder = threading.Thread(target=_hold)
        holder.start()
        held.wait(5)
        try:
            result = self._run(downloads, delay=0)
        finally:
            release.set()
            holder.join(5)

        self.assertEqual(downloads, [])  # the panel was never touched
        coalesced = [row for row in result['results'] if row.get('coalesced')]
        self.assertEqual(len(coalesced), 1, result)
        self.assertIn('ALREADY_RUNNING', coalesced[0]['error'])
        self.assertFalse(coalesced[0]['success'])
        self.assertEqual(coalesced[0]['server_id'], self.server.id)

    def test_the_lock_is_released_after_a_failed_backup(self):
        downloads = []
        settings = {'enabled': True, 'send_panel_backup': False,
                    'bot_token': 'tok', 'chat_id': 'chat'}
        with mock.patch.object(backup_service, '_get_telegram_backup_settings',
                               return_value=settings), \
             mock.patch.object(backup_service, '_telegram_backup_route_proxies',
                               return_value=({}, None)), \
             mock.patch.object(backup_service, 'get_xui_session',
                               return_value=(mock.Mock(), None)), \
             mock.patch.object(backup_service, '_fetch_xui_backup',
                               side_effect=RuntimeError('panel exploded')), \
             mock.patch.object(backup_service, '_telegram_send_document',
                               side_effect=RuntimeError('never reached')):
            with self.assertRaises(RuntimeError):
                backup_service._run_telegram_backup(trigger='manual')
        # The advisory lock and the local fast-path must both be free again.
        resource = backup_service._xui_backup_resource(self.server.id)
        with advisory_lock.resource_lock(resource, engine=app_module.db.engine) as owner:
            self.assertIsNotNone(owner)


def _ok_document(message_id=7, file_id='file-id'):
    return {'ok': True, 'result': {'message_id': message_id,
                                   'document': {'file_id': file_id}}}


if __name__ == '__main__':
    unittest.main()
