"""Tests for pulse_runner.py — mocked panel access and mocked pulse.run_probe."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import pulse  # noqa: E402
import pulse_runner  # noqa: E402
from app import PulseResultRecord, PulseRun, Server, app, db  # noqa: E402


def _server(name='panel-1', enabled=True):
    return Server(
        name=name, host='https://panel.example:8443/base',
        username='u', password='p', enabled=enabled, panel_type='auto',
    )


def _client(email, **overrides):
    raw = {'id': f'uuid-{email}', 'email': email, 'enable': True, 'subId': 'sub123'}
    raw.update(overrides)
    return raw


def _inbound(iid=1, remark='main', clients=None, enable=True):
    return {
        'id': iid, 'remark': remark, 'protocol': 'vless', 'port': 443,
        'enable': enable,
        'settings': json.dumps({'clients': clients or []}),
    }


def _manual_uri(name='Manual-One', host='example.com'):
    return (
        'vless://11111111-1111-1111-1111-111111111111@'
        f'{host}:443?type=tcp&security=none#{name}'
    )


def _fake_probe(verdict_by_label=None):
    verdict_by_label = verdict_by_label or {}

    def fake(config, profile):
        verdict = verdict_by_label.get(config.label, pulse.VERDICT_HEALTHY)
        result = pulse.ProbeResult(label=config.label, scheme='vless', verdict=verdict)
        result.tests = {
            'latency': {'avg_ms': 123.4, 'successes': 5},
            'loss': {'loss_pct': 0.0},
        }
        if verdict == pulse.VERDICT_DOWN:
            result.error = 'xray died'
        return result

    return fake


def _run_cli(argv):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = pulse_runner.main(argv)
    return code, json.loads(out.getvalue())


class PulseRunnerTestBase(unittest.TestCase):
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
        PulseResultRecord.query.delete()
        PulseRun.query.delete()
        Server.query.delete()
        db.session.commit()
        self.server = _server()
        db.session.add(self.server)
        db.session.commit()
        self._patches = [
            mock.patch.object(pulse_runner.app_module, 'get_xui_session',
                              return_value=(object(), None)),
            mock.patch.object(pulse_runner, 'find_xray_binary',
                              return_value='/fake/xray'),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._patches:
            patcher.stop()

    def _mock_inbounds(self, inbounds):
        return mock.patch.object(
            pulse_runner.app_module, 'fetch_inbounds',
            return_value=(inbounds, None, 'sanaei'))


class ListServersTest(PulseRunnerTestBase):
    def test_lists_enabled_servers_only(self):
        db.session.add(_server(name='disabled-one', enabled=False))
        db.session.commit()
        code, payload = _run_cli(['list-servers'])
        self.assertEqual(code, 0)
        names = [s['name'] for s in payload['servers']]
        self.assertEqual(names, ['panel-1'])
        self.assertEqual(payload['servers'][0]['id'], self.server.id)


class ListInboundsTest(PulseRunnerTestBase):
    def test_lists_inbounds_with_client_counts(self):
        inbounds = [
            _inbound(1, 'main', [_client('a'), _client('b')]),
            _inbound(2, 'empty', []),
        ]
        with self._mock_inbounds(inbounds):
            code, payload = _run_cli(['list-inbounds', '--server-id', str(self.server.id)])
        self.assertEqual(code, 0)
        self.assertEqual(payload['server']['name'], 'panel-1')
        by_id = {i['id']: i for i in payload['inbounds']}
        self.assertEqual(by_id[1]['clients'], 2)
        self.assertEqual(by_id[1]['protocol'], 'vless')
        self.assertEqual(by_id[2]['remark'], 'empty')

    def test_unknown_server_is_json_error(self):
        code, payload = _run_cli(['list-inbounds', '--server-id', '9999'])
        self.assertEqual(code, 2)
        self.assertIn('not found', payload['error'])


class RunTest(PulseRunnerTestBase):
    def test_manual_queued_run_uses_links_without_panel_lookup(self):
        first = _manual_uri('First')
        second = _manual_uri('Second', 'two.example.com')
        run = PulseRun(
            server_id=None, server_name='Manual links', scope='config',
            profile='quick', vantage='local', status='running',
            triggered_by='web', params_json=json.dumps({
                'config_source': 'manual',
                'manual_configs': [
                    {'uri': first, 'label': 'First'},
                    {'uri': second, 'label': 'Second'},
                ],
                'sites': [],
            }),
        )
        db.session.add(run)
        db.session.commit()
        with mock.patch.object(pulse_runner, '_get_server') as get_server, \
                mock.patch.object(pulse_runner, 'execute_probe_run',
                                  return_value=({}, [])) as execute:
            pulse_runner.execute_queued_run(run)
        get_server.assert_not_called()
        configs = execute.call_args.args[1]
        self.assertEqual([entry['config'].uri for entry in configs],
                         [first, second])
        self.assertEqual([entry['config'].label for entry in configs],
                         ['First', 'Second'])
        self.assertEqual(run.inbound_label, 'Manual links')

    def test_run_persists_run_and_rows(self):
        inbounds = [_inbound(1, 'main', [_client('alice'), _client('probe-bob')])]
        with self._mock_inbounds(inbounds), \
                mock.patch('pulse.run_probe', side_effect=_fake_probe()) as probe:
            code, payload = _run_cli(
                ['run', '--server-id', str(self.server.id), '--inbound-id', '1'])
        self.assertEqual(code, 0)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(payload['summary'], {'healthy': 2, 'degraded': 0, 'down': 0})
        self.assertEqual(payload['probed'], 2)
        self.assertFalse(payload['truncated'])

        run = PulseRun.query.get(payload['run_id'])
        self.assertIsNotNone(run)
        self.assertEqual(run.status, 'done')
        self.assertEqual(run.server_id, self.server.id)
        self.assertEqual(run.server_name, 'panel-1')
        self.assertEqual(run.scope, 'inbound')
        self.assertEqual(run.inbound_label, 'main')
        self.assertEqual(run.profile, 'quick')
        self.assertEqual(run.vantage, 'local')
        self.assertEqual(run.triggered_by, 'cli')
        self.assertEqual(json.loads(run.summary_json), {'healthy': 2, 'degraded': 0, 'down': 0})

        rows = PulseResultRecord.query.filter_by(run_id=run.id).all()
        self.assertEqual(len(rows), 2)
        by_probe_flag = {row.is_probe: row for row in rows}
        self.assertIn('probe-bob', by_probe_flag[True].config_label)
        self.assertEqual(by_probe_flag[True].verdict, 'healthy')
        self.assertAlmostEqual(by_probe_flag[True].latency_avg_ms, 123.4)
        detail = json.loads(by_probe_flag[True].detail_json)
        self.assertEqual(detail['scheme'], 'vless')

    def test_down_verdicts_tallied_and_exit_code_1(self):
        inbounds = [_inbound(1, 'main', [_client('alice'), _client('carol')])]
        verdicts = {'alice @ main': pulse.VERDICT_DOWN}
        with self._mock_inbounds(inbounds), \
                mock.patch('pulse.run_probe', side_effect=_fake_probe(verdicts)):
            code, payload = _run_cli(
                ['run', '--server-id', str(self.server.id), '--all-inbounds'])
        self.assertEqual(code, 1)
        self.assertEqual(payload['summary']['down'], 1)
        self.assertEqual(payload['summary']['healthy'], 1)
        self.assertEqual(payload['scope'], 'server')
        row = PulseResultRecord.query.filter_by(verdict='down').one()
        self.assertEqual(row.error, 'xray died')

    def test_limit_truncation_warns(self):
        inbounds = [_inbound(1, 'main', [_client('u1'), _client('u2'), _client('u3')])]
        with self._mock_inbounds(inbounds), \
                mock.patch('pulse.run_probe', side_effect=_fake_probe()) as probe:
            code, payload = _run_cli(
                ['run', '--server-id', str(self.server.id),
                 '--inbound-id', '1', '--limit', '2'])
        self.assertEqual(code, 0)
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(payload['truncated'])
        self.assertEqual(payload['total_available'], 3)
        self.assertEqual(payload['probed'], 2)
        self.assertTrue(any('--limit' in w for w in payload['warnings']))
        self.assertEqual(PulseResultRecord.query.count(), 2)

    def test_full_profile_traffic_caveat(self):
        inbounds = [_inbound(1, 'main', [_client('alice')])]
        with self._mock_inbounds(inbounds), \
                mock.patch('pulse.run_probe', side_effect=_fake_probe()):
            code, payload = _run_cli(
                ['run', '--server-id', str(self.server.id),
                 '--inbound-id', '1', '--profile', 'full'])
        self.assertEqual(code, 0)
        self.assertTrue(any('traffic' in w for w in payload['warnings']))

    def test_full_profile_uses_custom_download_and_upload_samples(self):
        profile = pulse_runner._build_profile(
            'full', [], download_bytes=25_000_000, upload_bytes=4_000_000)
        self.assertIn('bytes=25000000', profile.download_url)
        self.assertEqual(profile.upload_bytes, 4_000_000)

        default_profile = pulse_runner._build_profile('full', [])
        self.assertIn('bytes=10000000', default_profile.download_url)
        self.assertEqual(default_profile.upload_bytes, 2_000_000)

    def test_site_args_passed_to_profile(self):
        inbounds = [_inbound(1, 'main', [_client('alice')])]
        sites_file = tempfile.NamedTemporaryFile(
            'w', suffix='.txt', delete=False, encoding='utf-8')
        sites_file.write('# comment\n\nfile1=https://example.com::Welcome\n')
        sites_file.close()
        try:
            with self._mock_inbounds(inbounds), \
                    mock.patch('pulse.run_probe', side_effect=_fake_probe()) as probe:
                code, payload = _run_cli(
                    ['run', '--server-id', str(self.server.id), '--inbound-id', '1',
                     '--site', 'cli1=https://a.example',
                     '--sites-file', sites_file.name])
            self.assertEqual(code, 0)
            profile = probe.call_args[0][1]
            names = [s.name for s in profile.site_checks]
            self.assertEqual(names, ['cli1', 'file1'])
            self.assertEqual(profile.site_checks[1].expect_substring, 'Welcome')
        finally:
            os.unlink(sites_file.name)

    def test_missing_xray_is_clear_json_error(self):
        for patcher in self._patches:
            patcher.stop()
        self._patches = [
            mock.patch.object(pulse_runner.app_module, 'get_xui_session',
                              return_value=(object(), None)),
            mock.patch.object(pulse_runner, 'find_xray_binary', return_value=None),
        ]
        for patcher in self._patches:
            patcher.start()
        inbounds = [_inbound(1, 'main', [_client('alice')])]
        with self._mock_inbounds(inbounds), \
                mock.patch('pulse.run_probe') as probe:
            code, payload = _run_cli(
                ['run', '--server-id', str(self.server.id), '--inbound-id', '1'])
        self.assertEqual(code, 2)
        self.assertIn('eve --install-xray', payload['error'])
        probe.assert_not_called()
        self.assertEqual(PulseRun.query.count(), 0)

    def test_panel_login_failure_is_json_error(self):
        for patcher in self._patches:
            patcher.stop()
        self._patches = [
            mock.patch.object(pulse_runner.app_module, 'get_xui_session',
                              return_value=(None, 'Login failed: 403')),
            mock.patch.object(pulse_runner, 'find_xray_binary',
                              return_value='/fake/xray'),
        ]
        for patcher in self._patches:
            patcher.start()
        code, payload = _run_cli(
            ['run', '--server-id', str(self.server.id), '--all-inbounds'])
        self.assertEqual(code, 2)
        self.assertIn('login failed', payload['error'])


class HistoryTest(PulseRunnerTestBase):
    def _make_run(self, server_id, name, summary):
        run = PulseRun(server_id=server_id, server_name=name, scope='server',
                       profile='quick', status='done',
                       summary_json=json.dumps(summary))
        db.session.add(run)
        db.session.commit()
        return run

    def test_history_lists_recent_runs(self):
        self._make_run(self.server.id, 'panel-1', {'healthy': 3, 'degraded': 1, 'down': 0})
        code, payload = _run_cli(['history'])
        self.assertEqual(code, 0)
        self.assertEqual(len(payload['runs']), 1)
        entry = payload['runs'][0]
        self.assertEqual(entry['server_name'], 'panel-1')
        self.assertEqual(entry['summary']['healthy'], 3)
        self.assertEqual(entry['status'], 'done')

    def test_history_server_filter(self):
        self._make_run(self.server.id, 'panel-1', {'healthy': 1})
        other = _server(name='panel-2')
        db.session.add(other)
        db.session.commit()
        self._make_run(other.id, 'panel-2', {'down': 2})
        code, payload = _run_cli(['history', '--server-id', str(other.id)])
        self.assertEqual(code, 0)
        self.assertEqual(len(payload['runs']), 1)
        self.assertEqual(payload['runs'][0]['server_name'], 'panel-2')


class SitesFileTest(unittest.TestCase):
    def test_parse_sites_file(self):
        handle = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                             encoding='utf-8')
        handle.write('# comment\n\nfoo=https://foo.example\nbar=https://bar.example::OK\n')
        handle.close()
        try:
            sites = pulse_runner._load_sites_file(handle.name)
        finally:
            os.unlink(handle.name)
        self.assertEqual(len(sites), 2)
        self.assertEqual(sites[0], {'name': 'foo', 'url': 'https://foo.example',
                                    'expect_substring': None})
        self.assertEqual(sites[1]['expect_substring'], 'OK')

    def test_parse_sites_file_rejects_bad_line(self):
        handle = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                             encoding='utf-8')
        handle.write('no-equals-sign\n')
        handle.close()
        try:
            with self.assertRaises(ValueError):
                pulse_runner._load_sites_file(handle.name)
        finally:
            os.unlink(handle.name)

    def test_parse_site_spec(self):
        spec = pulse_runner._parse_site_spec('n=https://x.example::expect me')
        self.assertEqual(spec['name'], 'n')
        self.assertEqual(spec['url'], 'https://x.example')
        self.assertEqual(spec['expect_substring'], 'expect me')
        with self.assertRaises(ValueError):
            pulse_runner._parse_site_spec('broken')


if __name__ == '__main__':
    unittest.main()
