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
from panel.core import build_identity  # noqa: E402
from panel.routes import settings as settings_routes  # noqa: E402
from panel.routes import system as system_routes  # noqa: E402


class SystemUpdateApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        # Every response carries the build identity, which shells out to `git rev-parse`
        # ONCE PER PROCESS (panel.core.build_identity memoizes it). These tests patch
        # subprocess.run and assert on the calls they expect, so whether an unrelated git
        # call lands in that window depended on whether an earlier test in the same process
        # had already warmed the cache: the suite passed and the module alone failed.
        # Pinning the identity from the environment removes the git call entirely, so the
        # assertion tests the probe it is about and nothing else.
        cls._identity_env = mock.patch.dict(
            os.environ, {build_identity.ENV_VAR: 'test-build-sha'})
        cls._identity_env.start()
        build_identity.reset_cache()

    @classmethod
    def tearDownClass(cls):
        build_identity.reset_cache()
        cls._identity_env.stop()
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

        # The systemd probe reads real host state; force a failed probe so the
        # stored status is left untouched, independent of the runner's systemd.
        probe = mock.Mock(returncode=1, stdout='', stderr='')
        with mock.patch.object(system_routes.subprocess, 'run', return_value=probe):
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
        self.assertEqual(payload['current_version'], app_module.APP_VERSION)
        self.assertEqual(payload['status']['version'], '2.5.20')

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

    def test_start_normalizes_version_and_persists_target_for_runner(self):
        completed = mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch.object(app_module.subprocess, 'run', return_value=completed) as run:
            response = self.client.post(
                '/api/system-update/start',
                json={'confirm': 'UPDATE', 'ref': 'v.2.5.86'},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()['target_ref'], 'v2.5.86')
        self.assertEqual(
            (self.state_dir / 'requested-ref').read_text(encoding='utf-8').strip(),
            'v2.5.86',
        )
        run.assert_called_once_with(
            list(app_module.SYSTEM_UPDATE_START_COMMAND),
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_start_rejects_unsafe_version_ref(self):
        with mock.patch.object(app_module.subprocess, 'run') as run:
            response = self.client.post(
                '/api/system-update/start',
                json={'confirm': 'UPDATE', 'ref': '../../etc/passwd'},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('X-Eve-Status'), '400')
        self.assertFalse((self.state_dir / 'requested-ref').exists())
        run.assert_not_called()

    def test_versions_lists_git_history_with_current_marker(self):
        git_log = mock.Mock(returncode=0, stdout='abc123\n', stderr='')
        git_show = mock.Mock(
            returncode=0, stdout='APP_VERSION = "2.5.86"\n', stderr='')
        with mock.patch.object(
                system_routes.subprocess, 'run', side_effect=[git_log, git_show]):
            response = self.client.get('/api/system-update/versions')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['current_version'], app_module.APP_VERSION)
        self.assertEqual(payload['versions'][0]['version'], app_module.APP_VERSION)
        self.assertEqual(payload['versions'][1], {
            'version': '2.5.86', 'ref': 'abc123', 'current': False,
        })

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
        self.assertEqual(
            regular_client.get('/api/system-update/versions').status_code, 403)
        self.assertNotIn(
            'id="system-update-version"',
            regular_client.get('/').get_data(as_text=True))
        super_html = self.client.get('/').get_data(as_text=True)
        self.assertIn('id="system-update-version"', super_html)
        self.assertIn('id="system-update-log"', super_html)
        self.assertIn('id="system-update-version-select"', super_html)
        self.assertIn("/api/system-update/versions", super_html)
        self.assertIn("body: JSON.stringify({confirm:'UPDATE', ref:targetRef})", super_html)
        self.assertIn('const currentVersion = data.current_version;', super_html)
        self.assertIn('versionEl.textContent = `v${currentVersion}`;', super_html)

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


class DomainSslUpdatePersistenceTest(unittest.TestCase):
    def test_nginx_builder_preserves_large_backup_route(self):
        config = settings_routes._build_nginx_config(
            'panel.example.com', '5000', '/cert.pem', '/key.pem')
        self.assertIn('listen 443 ssl;', config)
        self.assertIn('client_max_body_size 2048m;', config)
        self.assertIn('location /protected-backups/', config)

    def test_nginx_apply_persists_domain_after_successful_reload(self):
        completed = mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch.object(
                settings_routes.subprocess, 'run', return_value=completed) as run:
            ok, error = settings_routes._apply_nginx_config(
                'panel.example.com', '/cert.pem', '/key.pem')
        self.assertTrue(ok, error)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(
            run.call_args_list[-1].args[0],
            ['sudo', 'tee', settings_routes.PERSISTED_DOMAIN_PATH],
        )
        self.assertEqual(run.call_args_list[-1].kwargs['input'], 'panel.example.com')

    def test_nginx_apply_rejects_directive_injection(self):
        with mock.patch.object(settings_routes.subprocess, 'run') as run:
            ok, error = settings_routes._apply_nginx_config(
                'panel.example.com; return 444')
        self.assertFalse(ok)
        self.assertIn('Invalid', error)
        run.assert_not_called()

    def test_nginx_apply_restores_previous_config_on_validation_failure(self):
        success = mock.Mock(returncode=0, stdout='', stderr='')
        failure = mock.Mock(returncode=1, stdout='', stderr='bad nginx config')
        with mock.patch('builtins.open', mock.mock_open(read_data='old config')), \
                mock.patch.object(
                    settings_routes.subprocess, 'run',
                    side_effect=[success, failure, success, success, success]) as run:
            ok, error = settings_routes._apply_nginx_config('panel.example.com')
        self.assertFalse(ok)
        self.assertIn('bad nginx config', error)
        self.assertEqual(run.call_count, 5)
        self.assertEqual(run.call_args_list[2].kwargs['input'], 'old config')

    def test_update_runner_backs_up_domain_and_ssl_material(self):
        root = Path(__file__).parents[1]
        runner = (root / 'eve_web_update_runner.sh').read_text(encoding='utf-8')
        setup = (root / 'setup.sh').read_text(encoding='utf-8')
        self.assertIn('/etc/eve-manager/domain', runner)
        self.assertIn('/etc/ssl/eve-manager', runner)
        self.assertIn('requested-ref', runner)
        self.assertIn('EVE_UPDATE_REF', runner)
        self.assertIn('/usr/local/bin/eve', runner)
        self.assertIn('verify_panel_proxy', setup)
        self.assertIn('resolve_update_target', setup)
        self.assertIn('EVE_LATEST_UPDATE_RUNNER', setup)
        self.assertIn('APP_VERSION', setup)
        self.assertIn('Recovered panel domain from installed TLS certificate', setup)

    def test_setup_migrations_are_bounded_and_use_single_runner(self):
        setup = (Path(__file__).parents[1] / 'setup.sh').read_text(encoding='utf-8')
        migration_fn = setup.split('run_migrations() {', 1)[1].split(
            '\nsetup_python_env() {', 1)[0]

        self.assertIn('EVE_MIGRATION_TIMEOUT_SECONDS:-600', migration_fn)
        self.assertIn('timeout --foreground --signal=TERM --kill-after=30s', migration_fn)
        self.assertIn('python3 -m panel.migrate', migration_fn)
        self.assertIn('systemctl stop "${active_units[@]}"', migration_fn)
        self.assertIn('systemctl kill --kill-who=all --signal=KILL', migration_fn)
        self.assertIn('systemctl start "$unit"', migration_fn)
        self.assertNotIn('python3 init_db.py', migration_fn)
        self.assertNotIn('python3 migrations.py', migration_fn)

    def test_maintenance_preflight_is_visible_bounded_and_does_not_remigrate(self):
        root = Path(__file__).parents[1]
        setup = (root / 'setup.sh').read_text(encoding='utf-8')
        maintenance = (root / 'maintenance.py').read_text(encoding='utf-8')
        preflight_fn = setup.split('show_maintenance_preflight() {', 1)[1].split(
            '\nstart_maintenance_service() {', 1)[0]

        self.assertIn('maximum 30 seconds', preflight_fn)
        self.assertIn('timeout --foreground --signal=TERM --kill-after=5s 30s', preflight_fn)
        self.assertIn('plan --skip-schema-migrations', preflight_fn)
        self.assertIn('update will continue', preflight_fn)
        self.assertIn('if not args.skip_schema_migrations:', maintenance)
        self.assertIn('maintenance.py run --skip-schema-migrations', setup)

    def test_systemd_runtimes_never_repeat_updater_migrations(self):
        setup = (Path(__file__).parents[1] / 'setup.sh').read_text(encoding='utf-8')
        systemd_fn = setup.split('setup_systemd() {', 1)[1].split(
            '\nrestart_eve_runtime_services() {', 1)[0]

        self.assertEqual(
            systemd_fn.count('Environment="EVE_SKIP_IMPORT_MIGRATIONS=1"'),
            4,
        )

    def test_renew_progress_copy_is_localized_and_hides_internal_cleanup(self):
        dashboard = (Path(__file__).parents[1] / 'templates' / 'dashboard.html').read_text(
            encoding='utf-8',
        )
        renew_fn = dashboard.split('async function submitRenewal() {', 1)[1].split(
            '\n    function showCreditAwareError', 1,
        )[0]

        self.assertIn('در حال اعمال تغییرات در 3x-ui…', renew_fn)
        self.assertIn('Applying changes to 3x-ui…', renew_fn)
        self.assertIn('در حال تأیید نهایی…', renew_fn)
        self.assertIn('Verifying renewed account…', renew_fn)
        self.assertNotIn('پاک‌سازی', renew_fn)
        self.assertNotIn('cleanup', renew_fn.lower())


if __name__ == '__main__':
    unittest.main()
