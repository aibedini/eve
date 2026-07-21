"""Tests for the Eve Pulse web UI, scheduler tick, and telegram alerts."""

import json
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import pulse  # noqa: E402
import pulse_runner  # noqa: E402
import app as app_module  # noqa: E402
from app import (  # noqa: E402
    Admin,
    PulseResultRecord,
    PulseRun,
    PulseSettings,
    PulseTemplate,
    Server,
    app,
    db,
)


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


def _fake_probe(verdict_by_label=None):
    verdict_by_label = verdict_by_label or {}

    def fake(config, profile):
        verdict = verdict_by_label.get(config.label, pulse.VERDICT_HEALTHY)
        result = pulse.ProbeResult(label=config.label, scheme='vless', verdict=verdict)
        result.tests = {
            'latency': {'avg_ms': 123.4, 'successes': 5},
            'loss': {'loss_pct': 0.0 if verdict != pulse.VERDICT_DOWN else 100.0},
        }
        if verdict == pulse.VERDICT_DOWN:
            result.error = 'xray died'
        return result

    return fake


class PulseWebTestBase(unittest.TestCase):
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
        PulseTemplate.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()

        self.admin = Admin(username='owner', password_hash='x',
                           role='superadmin', is_superadmin=True)
        self.server = _server()
        db.session.add_all([self.admin, self.server])
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
            mock.patch.object(pulse_runner, 'find_xray_binary',
                              return_value='/fake/xray'),
        ]

    def _enqueue_run(self, inbound_id=None, limit=5, sites=None):
        run = PulseRun(
            server_id=self.server.id,
            server_name=self.server.name,
            scope='inbound' if inbound_id is not None else 'server',
            profile='quick',
            status='queued',
            triggered_by='web',
            params_json=json.dumps({
                'inbound_id': inbound_id,
                'limit': limit,
                'sites': sites or [],
            }),
        )
        db.session.add(run)
        db.session.commit()
        return run


class PulsePageTest(PulseWebTestBase):
    def test_page_renders_with_sidebar_link(self):
        run = PulseRun(
            server_id=self.server.id, server_name='panel-1', scope='server',
            profile='quick', status='done', triggered_by='cli',
            created_at=app_module.datetime(2026, 7, 20, 10, 0, 0),
            finished_at=app_module.datetime(2026, 7, 20, 10, 0, 42),
            summary_json=json.dumps({'healthy': 2, 'degraded': 1, 'down': 0}),
        )
        db.session.add(run)
        db.session.commit()

        resp = self.client.get('/pulse')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # Sidebar entry (base.html) and page content both present.
        self.assertIn('<span>Pulse</span>', html)
        self.assertIn('panel-1', html)
        self.assertIn('pulse-add-step', html)
        self.assertIn('pulse-queue-body', html)
        self.assertIn('pulse-picker-grid', html)
        self.assertIn('pulse-inbound-select-all', html)
        self.assertIn('pulse-common-count', html)
        self.assertIn('pulse-selection-summary', html)
        self.assertIn('class="search-wrapper pulse-search-wrapper"', html)
        self.assertIn('id="pulse-search-clear"', html)
        self.assertIn('row.hidden=!matches', html)
        self.assertIn('pulse-config[hidden]{display:none!important}', html)
        self.assertIn('Choose the server, inbound, and exact configs', html)

    def test_page_requires_login(self):
        anon = app.test_client()
        resp = anon.get('/pulse')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers.get('Location', ''))

    def test_page_copy_follows_panel_language(self):
        with mock.patch.object(app_module, '_get_panel_ui_lang', return_value='fa'):
            html = self.client.get('/pulse').get_data(as_text=True)
        self.assertIn('ساخت برنامه تست', html)
        self.assertIn('صف زنده', html)
        self.assertNotIn('Create a test plan</h3>', html)


class PulseRunCreateTest(PulseWebTestBase):
    def test_post_run_enqueues_queued_run(self):
        resp = self.client.post('/pulse/run', data={
            'server_id': str(self.server.id),
            'inbound_id': 'all',
            'profile': 'quick',
            'limit': '5',
            'sites': 'foo=https://x.example::OK',
        }, headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['ok'])

        run = db.session.get(PulseRun, payload['run_id'])
        self.assertIsNotNone(run)
        self.assertEqual(run.status, 'queued')
        self.assertEqual(run.triggered_by, 'web')
        self.assertEqual(run.scope, 'server')
        params = json.loads(run.params_json)
        self.assertIsNone(params['inbound_id'])
        self.assertEqual(params['limit'], 5)
        self.assertEqual(params['sites'][0]['name'], 'foo')
        self.assertEqual(params['sites'][0]['expect_substring'], 'OK')

    def test_post_run_specific_inbound(self):
        resp = self.client.post('/pulse/run', json={
            'server_id': self.server.id, 'inbound_id': '3', 'profile': 'full',
        })
        self.assertEqual(resp.status_code, 200)
        run = db.session.get(PulseRun, resp.get_json()['run_id'])
        self.assertEqual(run.scope, 'inbound')
        self.assertEqual(run.profile, 'full')
        self.assertEqual(json.loads(run.params_json)['inbound_id'], 3)

    def test_post_run_rejects_bad_server(self):
        resp = self.client.post('/pulse/run', json={'server_id': 9999})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['ok'])
        self.assertEqual(PulseRun.query.count(), 0)

    def test_post_run_rejects_bad_site_spec(self):
        resp = self.client.post('/pulse/run', json={
            'server_id': self.server.id, 'sites': 'no-equals-sign',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PulseRun.query.count(), 0)

    def test_post_run_requires_login(self):
        anon = app.test_client()
        resp = anon.post('/pulse/run', json={'server_id': self.server.id})
        self.assertEqual(resp.status_code, 401)


class PulseRunDetailTest(PulseWebTestBase):
    def test_run_detail_json(self):
        run = PulseRun(
            server_id=self.server.id, server_name='panel-1', scope='inbound',
            inbound_label='main', profile='quick', status='done',
            triggered_by='web',
            summary_json=json.dumps({'healthy': 1, 'degraded': 0, 'down': 0}),
        )
        db.session.add(run)
        db.session.commit()
        db.session.add(PulseResultRecord(
            run_id=run.id, config_label='alice @ main', uri_scheme='vless',
            verdict='healthy', latency_avg_ms=123.4, loss_pct=0.0,
        ))
        db.session.commit()

        resp = self.client.get(f'/pulse/run/{run.id}',
                               headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['summary']['healthy'], 1)
        self.assertEqual(payload['inbound_label'], 'main')
        self.assertEqual(len(payload['results']), 1)
        self.assertEqual(payload['results'][0]['config_label'], 'alice @ main')

    def test_run_detail_404(self):
        resp = self.client.get('/pulse/run/9999')
        self.assertEqual(resp.status_code, 404)


class PulseInboundsEndpointTest(PulseWebTestBase):
    def test_inbounds_json(self):
        inbounds = [
            _inbound(1, 'main', [_client('a'), _client('b')]),
            _inbound(2, 'empty', []),
        ]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            resp = self.client.get(f'/pulse/servers/{self.server.id}/inbounds')
        finally:
            for patcher in patches:
                patcher.stop()
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['ok'])
        by_id = {i['id']: i for i in payload['inbounds']}
        self.assertEqual(by_id[1]['clients'], 2)
        self.assertEqual(by_id[2]['remark'], 'empty')

    def test_inbounds_unknown_server_404(self):
        resp = self.client.get('/pulse/servers/9999/inbounds')
        self.assertEqual(resp.status_code, 404)

    def test_configs_endpoint_lists_exact_safe_choices(self):
        inbounds = [_inbound(1, 'main', [
            _client('alice'), _client('disabled', enable=False),
        ])]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            resp = self.client.get(
                f'/pulse/servers/{self.server.id}/inbounds/1/configs')
        finally:
            for patcher in patches:
                patcher.stop()
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual([row['label'] for row in payload['configs']], ['alice'])
        self.assertNotIn('uri', payload['configs'][0])

    def test_v3_common_configs_filters_by_every_selected_inbound(self):
        clients = [
            dict(_client('alice'), inboundIds=[1, 2, 3]),
            dict(_client('bob'), inboundIds=[1]),
            dict(_client('carol'), inboundIds=[1, 2]),
        ]
        with mock.patch.object(app_module, 'get_xui_session',
                               return_value=(object(), None)), \
                mock.patch.object(app_module, 'server_is_v3', return_value=True), \
                mock.patch.object(app_module, '_v3_get',
                                  return_value=(True, {'obj': clients}, None)):
            resp = self.client.get(
                f'/pulse/servers/{self.server.id}/configs'
                '?inbound_id=1&inbound_id=2')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(
            [row['label'] for row in payload['configs']], ['alice', 'carol'])
        self.assertNotIn('uri', payload['configs'][0])


class PulsePlanAndTemplateTest(PulseWebTestBase):
    def _target(self, inbound_id=1, config_ids=None):
        return {
            'server_id': self.server.id,
            'server_name': self.server.name,
            'inbound_id': inbound_id,
            'inbound_label': f'inbound-{inbound_id}',
            'config_ids': config_ids or ['uuid-alice'],
            'config_labels': ['alice'],
        }

    def test_plan_enqueues_targets_and_configs_in_order(self):
        resp = self.client.post('/pulse/plan/run', json={
            'targets': [self._target(2, ['uuid-b']), self._target(1, ['uuid-a'])],
            'profile': 'full', 'vantage': 'local',
            'download_mb': 25, 'upload_mb': 5,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['queued'], 2)
        runs = PulseRun.query.order_by(PulseRun.id.asc()).all()
        self.assertEqual([run.params()['inbound_id'] for run in runs], [2, 1])
        self.assertEqual(runs[0].params()['config_ids'], ['uuid-b'])
        self.assertEqual(runs[0].params()['batch_index'], 1)
        self.assertEqual(runs[1].params()['batch_index'], 2)
        self.assertEqual(runs[0].params()['download_bytes'], 25_000_000)
        self.assertEqual(runs[0].params()['upload_bytes'], 5_000_000)

    def test_template_saves_exact_plan_and_can_queue_it(self):
        created = self.client.post('/pulse/templates', json={
            'name': 'EU baseline', 'targets': [self._target()],
            'profile': 'full', 'vantage': 'local',
            'schedule_enabled': True, 'interval_minutes': 30,
            'download_mb': 20, 'upload_mb': 4,
        })
        self.assertEqual(created.status_code, 200)
        template = PulseTemplate.query.one()
        self.assertEqual(template.targets()[0]['config_ids'], ['uuid-alice'])
        self.assertTrue(template.schedule_enabled)
        self.assertEqual(template.download_bytes, 20_000_000)
        self.assertEqual(template.upload_bytes, 4_000_000)
        page = self.client.get('/pulse')
        self.assertEqual(page.status_code, 200)
        self.assertIn('EU baseline', page.get_data(as_text=True))

        queued = self.client.post(f'/pulse/templates/{template.id}/run')
        self.assertEqual(queued.status_code, 200)
        run = PulseRun.query.one()
        self.assertEqual(run.triggered_by, 'template')
        self.assertEqual(run.params()['template_name'], 'EU baseline')

    def test_queue_endpoint_exposes_positions_and_selection_count(self):
        self._enqueue_run(inbound_id=1)
        second = self._enqueue_run(inbound_id=2)
        second.params_json = json.dumps({
            'inbound_id': 2, 'config_ids': ['a', 'b'], 'sites': [],
        })
        db.session.commit()
        payload = self.client.get('/pulse/queue').get_json()
        self.assertEqual([row['position'] for row in payload['runs']], [1, 2])
        self.assertEqual(payload['runs'][1]['params']['config_ids'], ['a', 'b'])

    def test_worker_probes_only_selected_configs_in_selected_order(self):
        run = self._enqueue_run(inbound_id=1)
        run.params_json = json.dumps({
            'inbound_id': 1,
            'config_ids': ['uuid-carol', 'uuid-alice'],
            'limit': 2, 'sites': [],
        })
        db.session.commit()
        inbounds = [_inbound(1, 'main', [
            _client('alice'), _client('bob'), _client('carol'),
        ])]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            with mock.patch('pulse.run_probe', side_effect=_fake_probe()) as probe:
                app_module.pulse_scheduler_tick()
        finally:
            for patcher in patches:
                patcher.stop()
        self.assertEqual(
            [call.args[0].label for call in probe.call_args_list],
            ['carol @ main', 'alice @ main'])

    def test_v3_worker_uses_one_common_client_across_selected_inbounds(self):
        run = self._enqueue_run(inbound_id=None)
        run.params_json = json.dumps({
            'inbound_id': None,
            'inbound_ids': [1, 2],
            'config_ids': ['uuid-alice'],
            'v3_mode': True,
            'limit': 1, 'sites': [],
            'download_bytes': 15_000_000,
            'upload_bytes': 3_000_000,
        })
        db.session.commit()
        inbounds = [
            _inbound(1, 'tcp', []),
            _inbound(2, 'grpc', []),
        ]
        v3_clients = [dict(_client('alice'), inboundIds=[1, 2])]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            with mock.patch.object(pulse_runner, '_fetch_v3_clients',
                                   return_value=v3_clients), \
                    mock.patch('pulse.run_probe', side_effect=_fake_probe()) as probe:
                app_module.pulse_scheduler_tick()
        finally:
            for patcher in patches:
                patcher.stop()
        self.assertEqual(
            [call.args[0].label for call in probe.call_args_list],
            ['alice @ tcp', 'alice @ grpc'])
        db.session.expire_all()
        run = db.session.get(PulseRun, run.id)
        self.assertEqual(run.inbound_label, 'tcp, grpc')


class PulseWorkerTickTest(PulseWebTestBase):
    def test_tick_executes_queued_run(self):
        run = self._enqueue_run(inbound_id=1)
        inbounds = [_inbound(1, 'main', [_client('alice'), _client('probe-bob')])]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            with mock.patch('pulse.run_probe', side_effect=_fake_probe()) as probe:
                app_module.pulse_scheduler_tick()
        finally:
            for patcher in patches:
                patcher.stop()

        self.assertEqual(probe.call_count, 2)
        db.session.expire_all()
        run = db.session.get(PulseRun, run.id)
        self.assertEqual(run.status, 'done')
        self.assertEqual(run.scope, 'inbound')
        self.assertEqual(run.inbound_label, 'main')
        self.assertEqual(json.loads(run.summary_json),
                         {'healthy': 2, 'degraded': 0, 'down': 0})
        self.assertEqual(PulseResultRecord.query.filter_by(run_id=run.id).count(), 2)

    def test_tick_marks_failed_run(self):
        run = self._enqueue_run(inbound_id=1)
        patches = self._panel_patches([_inbound(1, 'main', [_client('alice')])])
        for patcher in patches:
            patcher.start()
        try:
            with mock.patch('pulse.run_probe', side_effect=RuntimeError('boom')):
                app_module.pulse_scheduler_tick()
        finally:
            for patcher in patches:
                patcher.stop()
        db.session.expire_all()
        run = db.session.get(PulseRun, run.id)
        self.assertEqual(run.status, 'failed')
        self.assertIn('boom', run.error)

    def test_alert_sent_on_down_verdict(self):
        run = self._enqueue_run(inbound_id=1)
        inbounds = [_inbound(1, 'main', [_client('alice'), _client('carol')])]
        verdicts = {'carol @ main': pulse.VERDICT_DOWN}
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            with mock.patch('pulse.run_probe', side_effect=_fake_probe(verdicts)), \
                    mock.patch.object(app_module, '_pulse_send_telegram_alert') as alert:
                app_module.pulse_scheduler_tick()
        finally:
            for patcher in patches:
                patcher.stop()

        alert.assert_called_once()
        alert_run, text = alert.call_args[0]
        self.assertEqual(alert_run.id, run.id)
        self.assertIn('down: 1', text)
        self.assertIn('carol @ main', text)
        self.assertIn('panel-1', text)

    def test_no_alert_when_all_healthy(self):
        self._enqueue_run(inbound_id=1)
        inbounds = [_inbound(1, 'main', [_client('alice')])]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            with mock.patch('pulse.run_probe', side_effect=_fake_probe()), \
                    mock.patch.object(app_module, '_pulse_send_telegram_alert') as alert:
                app_module.pulse_scheduler_tick()
        finally:
            for patcher in patches:
                patcher.stop()
        alert.assert_not_called()

    def test_alert_respects_disabled_threshold(self):
        settings = app_module.get_pulse_settings()
        settings.alert_on_down = False
        db.session.commit()

        self._enqueue_run(inbound_id=1)
        inbounds = [_inbound(1, 'main', [_client('carol')])]
        verdicts = {'carol @ main': pulse.VERDICT_DOWN}
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            with mock.patch('pulse.run_probe', side_effect=_fake_probe(verdicts)), \
                    mock.patch.object(app_module, '_pulse_send_telegram_alert') as alert:
                app_module.pulse_scheduler_tick()
        finally:
            for patcher in patches:
                patcher.stop()
        alert.assert_not_called()

    def test_scheduler_enqueues_due_run(self):
        settings = app_module.get_pulse_settings()
        settings.enabled = True
        settings.interval_minutes = 30
        settings.server_id = self.server.id
        db.session.commit()

        inbounds = [_inbound(1, 'main', [_client('alice')])]
        patches = self._panel_patches(inbounds)
        for patcher in patches:
            patcher.start()
        try:
            with mock.patch('pulse.run_probe', side_effect=_fake_probe()):
                app_module.pulse_scheduler_tick()
        finally:
            for patcher in patches:
                patcher.stop()

        db.session.expire_all()
        run = PulseRun.query.filter_by(triggered_by='schedule').one()
        self.assertEqual(run.status, 'done')
        self.assertEqual(run.server_id, self.server.id)
        settings = db.session.get(PulseSettings, settings.id)
        self.assertIsNotNone(settings.last_run_at)

    def test_scheduler_skips_when_not_due(self):
        settings = app_module.get_pulse_settings()
        settings.enabled = True
        settings.interval_minutes = 60
        settings.last_run_at = app_module.datetime.utcnow()
        db.session.commit()

        app_module.pulse_scheduler_tick()
        self.assertEqual(PulseRun.query.count(), 0)

    def test_scheduler_disabled_enqueues_nothing(self):
        app_module.get_pulse_settings()  # singleton exists, enabled=False
        app_module.pulse_scheduler_tick()
        self.assertEqual(PulseRun.query.count(), 0)


class PulseSettingsTest(PulseWebTestBase):
    def test_settings_save(self):
        resp = self.client.post('/pulse/settings', data={
            'enabled': 'on',
            'interval_minutes': '30',
            'server_id': 'all',
            'profile': 'full',
            'limit': '25',
            'sites': 'foo=https://x.example',
            'alert_on_degraded': 'on',
        })
        self.assertEqual(resp.status_code, 302)

        db.session.expire_all()
        settings = PulseSettings.query.one()
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.interval_minutes, 30)
        self.assertIsNone(settings.server_id)
        self.assertEqual(settings.profile, 'full')
        self.assertEqual(settings.probe_limit, 25)
        self.assertEqual(settings.sites()[0]['name'], 'foo')
        self.assertFalse(settings.alert_on_down)  # unchecked checkbox
        self.assertTrue(settings.alert_on_degraded)

    def test_settings_save_specific_server_and_inbound(self):
        resp = self.client.post('/pulse/settings', data={
            'server_id': str(self.server.id),
            'inbound_id': '7',
            'alert_on_down': 'on',
        })
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        settings = PulseSettings.query.one()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.server_id, self.server.id)
        self.assertEqual(settings.inbound_id, 7)
        self.assertTrue(settings.alert_on_down)

    def test_settings_rejects_bad_site_spec(self):
        resp = self.client.post('/pulse/settings', data={
            'sites': 'broken-line',
        }, headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PulseSettings.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
