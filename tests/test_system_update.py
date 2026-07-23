"""Browser-triggered system update API and dashboard UI tests."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from app import Admin, app, db  # noqa: E402


class SystemUpdateApiTest(unittest.TestCase):
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
        self.superadmin = Admin(
            username='root-owner', password_hash='x', role='superadmin',
            is_superadmin=True,
        )
        self.regular = Admin(
            username='regular-admin', password_hash='x', role='admin',
            is_superadmin=False,
        )
        db.session.add_all([self.superadmin, self.regular])
        db.session.commit()
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / 'state'
        self.state_dir.mkdir()
        self.unit_path = Path(self.temp.name) / 'eve-web-update.service'
        self.unit_path.write_text('[Service]\n', encoding='utf-8')
        self.xray_unit_path = Path(self.temp.name) / 'eve-xray-install.service'
        self.xray_unit_path.write_text('[Service]\n', encoding='utf-8')
        self.patches = [
            mock.patch.object(app_module, 'SYSTEM_UPDATE_STATE_DIR', str(self.state_dir)),
            mock.patch.object(app_module, 'SYSTEM_UPDATE_UNIT_PATH', str(self.unit_path)),
            mock.patch.object(app_module, 'XRAY_INSTALL_UNIT_PATH', str(self.xray_unit_path)),
        ]
        for patcher in self.patches:
            patcher.start()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['admin_id'] = self.superadmin.id
            session['role'] = 'superadmin'
            session['is_superadmin'] = True

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def test_status_streams_log_chunks_and_strips_terminal_colors(self):
        (self.state_dir / 'status.json').write_text(json.dumps({
            'state': 'running', 'message': 'Installing',
            'started_at': '2026-07-22T00:00:00Z', 'version': '2.5.20',
        }), encoding='utf-8')
        (self.state_dir / 'update.log').write_bytes(
            b'first line\n\x1b[0;32msecond line\x1b[0m\n')

        response = self.client.get('/api/system-update/status?offset=0')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['available'])
        self.assertEqual(payload['status']['state'], 'running')
        self.assertIn('second line', payload['log'])
        self.assertNotIn('\x1b', payload['log'])
        self.assertEqual(payload['next_offset'], len(
            (self.state_dir / 'update.log').read_bytes()))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_start_uses_only_the_fixed_systemd_command(self):
        completed = mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch.object(app_module.subprocess, 'run', return_value=completed) as run:
            response = self.client.post(
                '/api/system-update/start', json={'confirm': 'UPDATE'})
        self.assertEqual(response.status_code, 202)
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0], list(app_module.SYSTEM_UPDATE_START_COMMAND))
        self.assertEqual(run.call_args.kwargs['timeout'], 10)

    def _write_status(self, state, **extra):
        payload = {'state': state}
        payload.update(extra)
        (self.state_dir / 'status.json').write_text(
            json.dumps(payload), encoding='utf-8')

    @staticmethod
    def _systemd_probe(active_state):
        return mock.Mock(returncode=0, stdout=f'{active_state}\n', stderr='')

    def test_stale_running_state_is_reported_as_interrupted(self):
        self._write_status('running', message='old run')
        with mock.patch.object(
                app_module.subprocess, 'run',
                return_value=self._systemd_probe('inactive')):
            payload = self.client.get('/api/system-update/status').get_json()
        self.assertEqual(payload['status']['state'], 'interrupted')
        self.assertIn('stopped', payload['status']['message'])

    def test_failed_unit_is_reported_as_interrupted(self):
        self._write_status('running')
        with mock.patch.object(
                app_module.subprocess, 'run',
                return_value=self._systemd_probe('failed')):
            payload = self.client.get('/api/system-update/status').get_json()
        self.assertEqual(payload['status']['state'], 'interrupted')

    def test_activating_oneshot_unit_keeps_running_state(self):
        # Regression: eve-web-update.service is Type=oneshot, so it reports
        # ActiveState=activating for its entire run; that must read as alive.
        self._write_status('running', message='Installing')
        with mock.patch.object(
                app_module.subprocess, 'run',
                return_value=self._systemd_probe('activating')) as run:
            payload = self.client.get('/api/system-update/status').get_json()
        self.assertEqual(payload['status']['state'], 'running')
        run.assert_called_once_with(
            ['/bin/systemctl', 'show', '--property=ActiveState', '--value',
             'eve-web-update.service'],
            capture_output=True, timeout=3, check=False, text=True)

    def test_active_unit_keeps_running_state(self):
        self._write_status('running')
        with mock.patch.object(
                app_module.subprocess, 'run',
                return_value=self._systemd_probe('active')):
            payload = self.client.get('/api/system-update/status').get_json()
        self.assertEqual(payload['status']['state'], 'running')

    def test_systemd_probe_timeout_fails_open(self):
        self._write_status('running')
        with mock.patch.object(
                app_module.subprocess, 'run',
                side_effect=app_module.subprocess.TimeoutExpired('systemctl', 3)):
            payload = self.client.get('/api/system-update/status').get_json()
        self.assertEqual(payload['status']['state'], 'running')

    def test_unparseable_active_state_fails_open(self):
        self._write_status('running')
        with mock.patch.object(
                app_module.subprocess, 'run',
                return_value=self._systemd_probe('')):
            payload = self.client.get('/api/system-update/status').get_json()
        self.assertEqual(payload['status']['state'], 'running')

    def test_terminal_status_skips_systemd_probe(self):
        for state in ('succeeded', 'failed', 'rolled_back'):
            self._write_status(state)
            with mock.patch.object(app_module.subprocess, 'run') as run:
                payload = self.client.get('/api/system-update/status').get_json()
            self.assertEqual(payload['status']['state'], state)
            run.assert_not_called()

    def test_running_update_cannot_be_started_twice(self):
        self._write_status('running')
        with mock.patch.object(
                app_module.subprocess, 'run',
                return_value=self._systemd_probe('activating')) as run:
            response = self.client.post(
                '/api/system-update/start', json={'confirm': 'UPDATE'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('X-Eve-Status'), '409')
        self.assertFalse(response.get_json()['success'])
        run.assert_called_once_with(
            ['/bin/systemctl', 'show', '--property=ActiveState', '--value',
             'eve-web-update.service'],
            capture_output=True, timeout=3, check=False, text=True)

    def test_update_endpoints_and_dashboard_card_are_superadmin_only(self):
        regular_client = app.test_client()
        with regular_client.session_transaction() as session:
            session['admin_id'] = self.regular.id
            session['role'] = 'admin'
            session['is_superadmin'] = False
        self.assertEqual(
            regular_client.get('/api/system-update/status').status_code, 403)
        self.assertNotIn(
            'id="system-update-version"',
            regular_client.get('/').get_data(as_text=True))
        super_html = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="system-update-version"', super_html)
        self.assertIn('id="system-update-log"', super_html)
        self.assertIn("body: JSON.stringify({confirm:'UPDATE'})", super_html)

    def test_xray_status_reports_installed_version_without_exposing_path(self):
        version = mock.Mock(returncode=0, stdout='Xray 25.7.26 (Eve)\n', stderr='')
        with mock.patch.object(app_module, 'find_xray_binary', return_value='/private/xray'), \
                mock.patch.object(app_module.subprocess, 'run', return_value=version):
            response = self.client.get('/api/settings/telegram-bots/xray-runtime')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['installed'])
        self.assertEqual(payload['state'], 'installed')
        self.assertEqual(payload['version'], 'Xray 25.7.26 (Eve)')
        self.assertNotIn('/private/xray', str(payload))

    def test_xray_install_uses_only_fixed_systemd_command(self):
        inactive = mock.Mock(returncode=3, stdout='success\n', stderr='')
        started = mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch.object(app_module, 'find_xray_binary', return_value=None), \
                mock.patch.object(app_module.subprocess, 'run', side_effect=[inactive, inactive, started]) as run:
            response = self.client.post(
                '/api/settings/telegram-bots/xray-runtime/install',
                json={'confirm': 'INSTALL'},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(run.call_args.args[0], list(app_module.XRAY_INSTALL_START_COMMAND))
        self.assertEqual(run.call_args.kwargs['timeout'], 10)

    def test_xray_runtime_endpoints_are_superadmin_only(self):
        regular_client = app.test_client()
        with regular_client.session_transaction() as session:
            session['admin_id'] = self.regular.id
            session['role'] = 'admin'
            session['is_superadmin'] = False
        self.assertEqual(
            regular_client.get('/api/settings/telegram-bots/xray-runtime').status_code, 403)
        html = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('id="tgb-xray-runtime"', html)
        self.assertIn("body:JSON.stringify({confirm:'INSTALL'})", html)


class RenewalHistoryMarkupTest(unittest.TestCase):
    def test_history_date_annotation_includes_renewal_volume(self):
        template = (Path(__file__).parents[1] / 'templates' / 'subscription.html').read_text(
            encoding='utf-8')
        self.assertIn('let _historyRenewalsByLabel = new Map()', template)
        self.assertIn("${IS_FA?'تمدید':'Renewed'} · ${renewalVolume}", template)


if __name__ == '__main__':
    unittest.main()
