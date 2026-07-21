"""Tests for the Pulse Phase-3 remote vantage agent and its eve-side API.

Agent-side tests are fully offline (fake HTTP session, mocked run_probe).
Server-side tests use the Flask test client with the panel API mocked.
"""

import json
import os
import tempfile
import types
import unittest
from unittest import mock

import requests

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import pulse  # noqa: E402
import pulse_agent  # noqa: E402
import pulse_runner  # noqa: E402
import app as app_module  # noqa: E402
from app import (  # noqa: E402
    Admin,
    PulseAgent,
    PulseResultRecord,
    PulseRun,
    PulseSettings,
    Server,
    app,
    db,
)


# ---------------------------------------------------------------------------
# Agent-side (pulse_agent.py) — offline, fake eve server
# ---------------------------------------------------------------------------
class FakeHttpResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"http {self.status_code}")

    def json(self):
        return self._payload


class FakeHttp:
    """Scripted stand-in for requests.Session used by the agent."""

    def __init__(self, task=None):
        self._task = task or {'ok': True, 'run_id': None}
        self.gets = []
        self.posts = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeHttpResponse(self._task)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeHttpResponse({'ok': True})


def _agent_args(**overrides):
    args = types.SimpleNamespace(
        eve_url='https://eve.example', token='tok123', agent_name='de-1',
        timeout=5, poll=10, once=True,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _canned_result(label):
    result = pulse.ProbeResult(label=label, scheme='vless',
                               verdict=pulse.VERDICT_HEALTHY)
    result.tests = {'latency': {'avg_ms': 100.0, 'successes': 5},
                    'loss': {'loss_pct': 0.0}}
    return result


class AgentProfileTest(unittest.TestCase):
    def test_build_profile_from_task_payload(self):
        payload = {
            'run_latency': True, 'run_download': False, 'run_upload': False,
            'latency_attempts': 3, 'unknown_field': 'ignored',
            'site_checks': [
                {'name': 'ex', 'url': 'https://example.com',
                 'expect_substring': 'OK', 'bogus': 1},
            ],
        }
        profile = pulse_agent.build_profile(payload)
        self.assertFalse(profile.run_download)
        self.assertFalse(profile.run_upload)
        self.assertEqual(profile.latency_attempts, 3)
        self.assertEqual(len(profile.site_checks), 1)
        self.assertEqual(profile.site_checks[0].name, 'ex')
        self.assertEqual(profile.site_checks[0].expect_substring, 'OK')

    def test_build_profile_empty_payload_gives_defaults(self):
        profile = pulse_agent.build_profile(None)
        self.assertEqual(profile, pulse.ProbeProfile())


class AgentLoopTest(unittest.TestCase):
    def test_once_claims_runs_and_reports(self):
        task = {
            'ok': True,
            'run_id': 42,
            'configs': [
                {'label': 'alice @ main', 'uri': 'vless://a'},
                {'label': 'bob @ main', 'uri': 'vless://b'},
            ],
            'profile': {'run_download': False, 'run_upload': False},
        }
        http = FakeHttp(task=task)
        probed = []

        def fake_probe(config, profile):
            probed.append((config.label, profile.run_download))
            return _canned_result(config.label)

        code = pulse_agent.loop(_agent_args(), http=http, probe_runner=fake_probe)
        self.assertEqual(code, 0)
        self.assertEqual(probed, [('alice @ main', False), ('bob @ main', False)])
        # claimed with bearer auth and agent name
        get_url, get_kwargs = http.gets[0]
        self.assertEqual(get_url, 'https://eve.example/api/pulse/agent/tasks')
        self.assertEqual(get_kwargs['params'], {'agent': 'de-1'})
        self.assertEqual(get_kwargs['headers']['Authorization'], 'Bearer tok123')
        # results pushed to the report endpoint
        post_url, post_kwargs = http.posts[0]
        self.assertEqual(post_url, 'https://eve.example/api/pulse/agent/report')
        body = post_kwargs['json']
        self.assertEqual(body['run_id'], 42)
        self.assertEqual(len(body['results']), 2)
        self.assertEqual(body['results'][0]['label'], 'alice @ main')

    def test_once_empty_task_posts_nothing(self):
        http = FakeHttp()  # {'ok': True, 'run_id': None}
        code = pulse_agent.loop(_agent_args(), http=http)
        self.assertEqual(code, 0)
        self.assertEqual(len(http.posts), 0)

    def test_once_unreachable_eve_returns_error(self):
        http = mock.Mock()
        http.get.side_effect = requests.ConnectionError('eve down')
        code = pulse_agent.loop(_agent_args(), http=http)
        self.assertEqual(code, 1)

    def test_backoff_does_not_crash_and_grows(self):
        http = mock.Mock()
        http.get.side_effect = requests.ConnectionError('eve down')
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            if len(slept) >= 3:
                raise SystemExit(0)

        with mock.patch.object(pulse_agent.time, 'sleep', side_effect=fake_sleep):
            with self.assertRaises(SystemExit):
                pulse_agent.loop(_agent_args(once=False, poll=10), http=http)
        self.assertEqual(slept, [10, 20, 40])

    def test_report_http_error_propagates(self):
        http = FakeHttp()
        with mock.patch.object(http, 'post',
                               return_value=FakeHttpResponse({}, status_code=500)):
            with self.assertRaises(requests.HTTPError):
                pulse_agent.post_report(http, 'https://eve.example', 'tok',
                                        1, [], 5)


# ---------------------------------------------------------------------------
# Server-side (app.py) — Flask test client
# ---------------------------------------------------------------------------
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


class PulseAgentApiTestBase(unittest.TestCase):
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
        PulseSettings.query.delete()
        PulseAgent.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()

        self.admin = Admin(username='owner', password_hash='x',
                           role='superadmin', is_superadmin=True)
        self.server = _server()
        self.agent = PulseAgent(name='de-1', token='tok123')
        db.session.add_all([self.admin, self.server, self.agent])
        db.session.commit()

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin.id

    def _panel_patches(self, inbounds):
        return [
            mock.patch.object(pulse_runner.app_module, 'get_xui_session',
                              return_value=(object(), None)),
            mock.patch.object(pulse_runner.app_module, 'fetch_inbounds',
                              return_value=(inbounds, None, 'sanaei')),
        ]

    def _enqueue_remote_run(self):
        run = PulseRun(
            server_id=self.server.id,
            server_name=self.server.name,
            scope='server',
            profile='quick',
            vantage='agent:de-1',
            status='queued',
            triggered_by='web',
            params_json=json.dumps({
                'inbound_id': None, 'limit': 5, 'sites': [],
            }),
        )
        db.session.add(run)
        db.session.commit()
        return run


class PulseAgentAdminTest(PulseAgentApiTestBase):
    def test_create_agent_returns_token_once(self):
        resp = self.client.post('/pulse/agents', json={'name': 'nl-ams-1'})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['ok'])
        token = payload['agent']['token']
        self.assertEqual(len(token), 32)
        agent = PulseAgent.query.filter_by(name='nl-ams-1').one()
        self.assertEqual(agent.token, token)
        self.assertTrue(agent.enabled)

    def test_create_agent_rejects_duplicate_and_bad_name(self):
        dup = self.client.post('/pulse/agents', json={'name': 'de-1'})
        self.assertEqual(dup.status_code, 409)
        bad = self.client.post('/pulse/agents', json={'name': 'x'})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(PulseAgent.query.count(), 1)

    def test_delete_agent(self):
        resp = self.client.post(f'/pulse/agents/{self.agent.id}/delete',
                                headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(PulseAgent.query.filter_by(name='de-1').first())

    def test_page_lists_agents_and_vantage_options(self):
        resp = self.client.get('/pulse')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('de-1', html)
        self.assertIn('agent:de-1', html)  # vantage selector option
        self.assertIn('pulse-create-agent', html)

    def test_run_create_with_agent_vantage(self):
        resp = self.client.post('/pulse/run', json={
            'server_id': self.server.id, 'vantage': 'agent:de-1',
        })
        self.assertEqual(resp.status_code, 200)
        run = db.session.get(PulseRun, resp.get_json()['run_id'])
        self.assertEqual(run.vantage, 'agent:de-1')
        self.assertEqual(run.status, 'queued')

    def test_run_create_rejects_unknown_agent_vantage(self):
        resp = self.client.post('/pulse/run', json={
            'server_id': self.server.id, 'vantage': 'agent:nope',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PulseRun.query.count(), 0)


class PulseAgentTasksEndpointTest(PulseAgentApiTestBase):
    def test_auth_failures(self):
        no_token = self.client.get('/api/pulse/agent/tasks?agent=de-1')
        self.assertEqual(no_token.status_code, 401)
        wrong = self.client.get(
            '/api/pulse/agent/tasks?agent=de-1',
            headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(wrong.status_code, 401)

    def test_empty_queue_returns_null_run(self):
        resp = self.client.get(
            '/api/pulse/agent/tasks?agent=de-1',
            headers={'Authorization': 'Bearer tok123'})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()['run_id'])
        # last_seen bookkeeping updated
        db.session.expire_all()
        agent = PulseAgent.query.filter_by(name='de-1').one()
        self.assertIsNotNone(agent.last_seen_at)
        self.assertIsNotNone(agent.last_ip)

    def test_local_runs_are_not_claimable(self):
        run = PulseRun(
            server_id=self.server.id, server_name=self.server.name,
            scope='server', profile='quick', vantage='local',
            status='queued', triggered_by='web',
            params_json='{}',
        )
        db.session.add(run)
        db.session.commit()
        resp = self.client.get(
            '/api/pulse/agent/tasks?agent=de-1',
            headers={'Authorization': 'Bearer tok123'})
        self.assertIsNone(resp.get_json()['run_id'])

    def test_claim_flow_returns_configs_and_marks_running(self):
        run = self._enqueue_remote_run()
        inbounds = [_inbound(1, 'main', [_client('alice'), _client('probe-bob')])]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            resp = self.client.get(
                '/api/pulse/agent/tasks?agent=de-1',
                headers={'Authorization': 'Bearer tok123'})
        finally:
            for patcher in patches:
                patcher.stop()
        self.assertEqual(resp.status_code, 200)
        task = resp.get_json()
        self.assertTrue(task['ok'])
        self.assertEqual(task['run_id'], run.id)
        labels = [c['label'] for c in task['configs']]
        self.assertEqual(labels, ['alice @ main', 'probe-bob @ main'])
        for entry in task['configs']:
            self.assertTrue(entry['uri'].startswith('vless://'))
            self.assertNotIn('is_probe', entry)  # internal flag stays server-side
        self.assertIsInstance(task['profile'], dict)
        self.assertFalse(task['profile']['run_download'])  # quick profile

        db.session.expire_all()
        run = db.session.get(PulseRun, run.id)
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.scope, 'server')
        self.assertIsNone(run.inbound_label)
        stashed = json.loads(run.params_json)['configs']
        self.assertEqual(len(stashed), 2)
        self.assertTrue(stashed[1]['is_probe'])  # 'probe' in email

        # second claim: nothing left
        resp2 = self.client.get(
            '/api/pulse/agent/tasks?agent=de-1',
            headers={'Authorization': 'Bearer tok123'})
        self.assertIsNone(resp2.get_json()['run_id'])

    def test_full_task_carries_custom_speed_sample_sizes(self):
        run = self._enqueue_remote_run()
        run.profile = 'full'
        run.params_json = json.dumps({
            'inbound_id': 1,
            'limit': 1,
            'sites': [],
            'download_bytes': 30_000_000,
            'upload_bytes': 6_000_000,
        })
        db.session.commit()
        patches = self._panel_patches([
            _inbound(1, 'main', [_client('alice')]),
        ])
        for patcher in patches:
            patcher.start()
        try:
            response = self.client.get(
                '/api/pulse/agent/tasks?agent=de-1',
                headers={'Authorization': 'Bearer tok123'})
        finally:
            for patcher in patches:
                patcher.stop()
        self.assertEqual(response.status_code, 200)
        profile = response.get_json()['profile']
        self.assertIn('bytes=30000000', profile['download_url'])
        self.assertEqual(profile['upload_bytes'], 6_000_000)

    def test_scheduler_does_not_drain_remote_runs(self):
        self._enqueue_remote_run()
        app_module.pulse_scheduler_tick()
        db.session.expire_all()
        run = PulseRun.query.one()
        self.assertEqual(run.status, 'queued')  # untouched by the local worker


class PulseAgentReportEndpointTest(PulseAgentApiTestBase):
    def _claimed_run(self):
        run = self._enqueue_remote_run()
        inbounds = [_inbound(1, 'main', [_client('alice'), _client('carol')])]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            self.client.get(
                '/api/pulse/agent/tasks?agent=de-1',
                headers={'Authorization': 'Bearer tok123'})
        finally:
            for patcher in patches:
                patcher.stop()
        db.session.expire_all()
        return db.session.get(PulseRun, run.id)

    def _result_dict(self, label, verdict, error=None):
        result = pulse.ProbeResult(label=label, scheme='vless', verdict=verdict)
        result.error = error
        result.tests = {
            'latency': {'avg_ms': 210.5, 'successes': 5},
            'loss': {'loss_pct': 0.0 if verdict != 'down' else 100.0},
        }
        return result.to_dict()

    def test_report_persists_rows_and_finalizes(self):
        run = self._claimed_run()
        results = [
            self._result_dict('alice @ main', 'healthy'),
            self._result_dict('carol @ main', 'down', error='xray died'),
        ]
        with mock.patch.object(app_module, '_pulse_send_telegram_alert') as alert:
            resp = self.client.post(
                '/api/pulse/agent/report',
                json={'run_id': run.id, 'results': results},
                headers={'Authorization': 'Bearer tok123'})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['summary'], {'healthy': 1, 'degraded': 0, 'down': 1})

        db.session.expire_all()
        run = db.session.get(PulseRun, run.id)
        self.assertEqual(run.status, 'done')
        self.assertIsNotNone(run.finished_at)
        records = PulseResultRecord.query.filter_by(run_id=run.id).all()
        self.assertEqual(len(records), 2)
        by_label = {rec.config_label: rec for rec in records}
        self.assertEqual(by_label['alice @ main'].verdict, 'healthy')
        self.assertEqual(by_label['carol @ main'].verdict, 'down')
        self.assertEqual(by_label['carol @ main'].loss_pct, 100.0)
        self.assertEqual(by_label['carol @ main'].error, 'xray died')
        # alert fired because one config is down
        alert.assert_called_once()
        self.assertEqual(alert.call_args[0][0].id, run.id)
        self.assertIn('down: 1', alert.call_args[0][1])

    def test_report_rejects_foreign_agent_and_unknown_run(self):
        run = self._claimed_run()
        other = PulseAgent(name='fr-1', token='tok999')
        db.session.add(other)
        db.session.commit()
        resp = self.client.post(
            '/api/pulse/agent/report',
            json={'run_id': run.id, 'results': []},
            headers={'Authorization': 'Bearer tok999'})
        self.assertEqual(resp.status_code, 404)
        missing = self.client.post(
            '/api/pulse/agent/report',
            json={'run_id': 99999, 'results': []},
            headers={'Authorization': 'Bearer tok123'})
        self.assertEqual(missing.status_code, 404)

    def test_report_on_finished_run_conflicts(self):
        run = self._claimed_run()
        self.client.post(
            '/api/pulse/agent/report',
            json={'run_id': run.id, 'results': [self._result_dict('alice @ main', 'healthy')]},
            headers={'Authorization': 'Bearer tok123'})
        again = self.client.post(
            '/api/pulse/agent/report',
            json={'run_id': run.id, 'results': []},
            headers={'Authorization': 'Bearer tok123'})
        self.assertEqual(again.status_code, 409)


class PulseComparisonTest(PulseAgentApiTestBase):
    def _done_run(self, vantage, label, verdict, latency, loss):
        run = PulseRun(
            server_id=self.server.id, server_name=self.server.name,
            scope='server', profile='quick', vantage=vantage,
            status='done', triggered_by='web',
            finished_at=app_module.datetime.utcnow(),
            summary_json=json.dumps({'healthy': 1, 'degraded': 0, 'down': 0}),
        )
        db.session.add(run)
        db.session.commit()
        db.session.add(PulseResultRecord(
            run_id=run.id, config_label=label, uri_scheme='vless',
            verdict=verdict, latency_avg_ms=latency, loss_pct=loss,
        ))
        db.session.commit()
        return run

    def test_comparison_block_in_run_detail(self):
        local = self._done_run('local', 'alice @ main', 'healthy', 123.0, 0.0)
        remote = self._done_run('agent:de-1', 'alice @ main', 'degraded', 450.0, 5.0)

        resp = self.client.get(f'/pulse/run/{local.id}',
                               headers={'X-Requested-With': 'XMLHttpRequest'})
        payload = resp.get_json()
        cmp = payload['comparison']
        self.assertIsNotNone(cmp)
        self.assertEqual(cmp['run_id'], remote.id)
        self.assertEqual(cmp['vantage'], 'agent:de-1')
        other = cmp['configs']['alice @ main']
        self.assertEqual(other['verdict'], 'degraded')
        self.assertEqual(other['latency_avg_ms'], 450.0)
        self.assertEqual(other['loss_pct'], 5.0)

        # and symmetrically from the remote run's point of view
        resp = self.client.get(f'/pulse/run/{remote.id}',
                               headers={'X-Requested-With': 'XMLHttpRequest'})
        cmp = resp.get_json()['comparison']
        self.assertEqual(cmp['vantage'], 'local')

    def test_comparison_none_without_other_vantage(self):
        local = self._done_run('local', 'alice @ main', 'healthy', 123.0, 0.0)
        resp = self.client.get(f'/pulse/run/{local.id}',
                               headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertIsNone(resp.get_json()['comparison'])


if __name__ == '__main__':
    unittest.main()
