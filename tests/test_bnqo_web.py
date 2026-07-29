"""Tests for the BNQO control-plane: agent enroll/auth/report, admin API,
status engine and incident lifecycle.

Wire contract under test: docs/bnqo/EVE_API_CONTRACT.md
"""

import base64
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

import app as app_module  # noqa: E402
from app import (  # noqa: E402
    Admin,
    BnqoAgent,
    BnqoEnrollToken,
    BnqoIncident,
    BnqoJob,
    BnqoLink,
    BnqoMeasurement,
    app,
    db,
)
from panel.jobs.bnqo import bnqo_scheduler_tick  # noqa: E402
from panel.services.bnqo_crypto import canonical_json  # noqa: E402


def _agent_key():
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode('ascii')
    return key, pub


def _signed_headers(key, token, body=b''):
    ts = str(int(time.time()))
    signature = base64.b64encode(key.sign(ts.encode('utf-8') + b'\n' + body)).decode('ascii')
    return {
        'Authorization': f'Bearer {token}',
        'X-BNQO-Timestamp': ts,
        'X-BNQO-Signature': signature,
    }


def _verify_cp_signature(payload, cp_pubkey_b64):
    """Verify the CP Ed25519 signature over the canonical JSON of the payload
    minus its ``signature`` field (contract §1)."""
    signed = {k: v for k, v in payload.items() if k != 'signature'}
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(cp_pubkey_b64))
    pub.verify(base64.b64decode(payload['signature']), canonical_json(signed))


class BnqoTestBase(unittest.TestCase):
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
        for model in (BnqoMeasurement, BnqoIncident, BnqoJob, BnqoLink,
                      BnqoAgent, BnqoEnrollToken):
            model.query.delete()
        Admin.query.delete()
        db.session.commit()

        self.admin = Admin(username='owner', password_hash='x',
                           role='superadmin', is_superadmin=True)
        db.session.add(self.admin)
        db.session.commit()

        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin.id

    # -- helpers ------------------------------------------------------------
    def _make_enroll_token(self, token='enroll-tok-1', role='outside',
                           expires_in_minutes=30, used=False):
        row = BnqoEnrollToken(
            token=token, role=role,
            expires_at=datetime.utcnow() + timedelta(minutes=expires_in_minutes),
            used_at=datetime.utcnow() if used else None,
        )
        db.session.add(row)
        db.session.commit()
        return row

    def _enroll(self, name='agent-1', token='enroll-tok-1', key=None, role='outside'):
        key = key or _agent_key()[0]
        pub = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ).decode('ascii')
        body = json.dumps({
            'enroll_token': token, 'name': name, 'role': role,
            'pubkey': pub, 'address': '203.0.113.7', 'port': 44818,
            'version': '0.1.0',
        }).encode('utf-8')
        resp = self.client.post('/api/bnqo/agent/enroll', data=body,
                                content_type='application/json')
        return resp, key

    def _enroll_ok(self, name, token):
        resp, key = self._enroll(name=name, token=token)
        assert resp.status_code == 200, resp.get_json()
        return key, resp.get_json()['agent_token']

    def _agent_get(self, path, key, token):
        return self.client.get(path, headers=_signed_headers(key, token))

    def _agent_post(self, path, payload, key, token):
        body = json.dumps(payload).encode('utf-8')
        return self.client.post(path, data=body, content_type='application/json',
                                headers=_signed_headers(key, token, body))

    def _link(self, agent_a_id, agent_b_id, name='IR-DE'):
        link = BnqoLink(name=name, agent_a_id=agent_a_id, agent_b_id=agent_b_id,
                        enabled=True)
        db.session.add(link)
        db.session.commit()
        return link

    def _two_agents_with_link(self):
        self._make_enroll_token('tok-a')
        self._make_enroll_token('tok-b')
        key_a, token_a = self._enroll_ok('agent-a', 'tok-a')
        key_b, token_b = self._enroll_ok('agent-b', 'tok-b')
        agent_a = BnqoAgent.query.filter_by(name='agent-a').one()
        agent_b = BnqoAgent.query.filter_by(name='agent-b').one()
        link = self._link(agent_a.id, agent_b.id)
        return (key_a, token_a, agent_a), (key_b, token_b, agent_b), link


class BnqoEnrollTest(BnqoTestBase):
    def test_enroll_unknown_token_404(self):
        resp, _ = self._enroll(token='nope')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['error']['code'], 'enroll_token_invalid')

    def test_enroll_expired_token_410(self):
        self._make_enroll_token(expires_in_minutes=-5)
        resp, _ = self._enroll()
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.get_json()['error']['code'], 'enroll_token_expired')

    def test_enroll_success_and_single_use(self):
        self._make_enroll_token()
        resp, _ = self._enroll()
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn('agent_token', payload)
        self.assertIn('cp_pubkey', payload)
        self.assertEqual(payload['config_version'], 1)
        agent = BnqoAgent.query.filter_by(name='agent-1').one()
        self.assertEqual(agent.role, 'outside')
        self.assertEqual(agent.address, '203.0.113.7')

        # Second use of the same token is rejected.
        resp2, _ = self._enroll(name='agent-2')
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.get_json()['error']['code'], 'enroll_token_used')

    def test_enroll_duplicate_name_409(self):
        self._make_enroll_token('tok-1')
        self._make_enroll_token('tok-2')
        resp, _ = self._enroll(token='tok-1')
        self.assertEqual(resp.status_code, 200)
        resp2, _ = self._enroll(token='tok-2')  # same default name 'agent-1'
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.get_json()['error']['code'], 'agent_name_taken')

    def test_enroll_bad_pubkey_400(self):
        self._make_enroll_token()
        body = json.dumps({
            'enroll_token': 'enroll-tok-1', 'name': 'agent-x', 'role': 'iran',
            'pubkey': 'not-base64!!', 'port': 44818,
        }).encode('utf-8')
        resp = self.client.post('/api/bnqo/agent/enroll', data=body,
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class BnqoAgentAuthTest(BnqoTestBase):
    def test_config_requires_auth_headers(self):
        self._make_enroll_token()
        resp, _ = self._enroll()
        self.assertEqual(resp.status_code, 200)
        # No headers at all → 401
        self.assertEqual(self.client.get('/api/bnqo/agent/config').status_code, 401)
        # Bearer only, no signature headers → 401
        token = resp.get_json()['agent_token']
        resp2 = self.client.get('/api/bnqo/agent/config',
                                headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(resp2.status_code, 401)
        self.assertEqual(resp2.get_json()['error']['code'], 'missing_signature')

    def test_config_bad_signature_401(self):
        self._make_enroll_token()
        resp, _ = self._enroll()
        token = resp.get_json()['agent_token']
        other_key, _ = _agent_key()
        resp2 = self.client.get('/api/bnqo/agent/config',
                                headers=_signed_headers(other_key, token))
        self.assertEqual(resp2.status_code, 401)
        self.assertEqual(resp2.get_json()['error']['code'], 'invalid_signature')

    def test_config_stale_timestamp_401(self):
        self._make_enroll_token()
        resp, key = self._enroll()
        token = resp.get_json()['agent_token']
        ts = str(int(time.time()) - 900)
        sig = base64.b64encode(key.sign(ts.encode() + b'\n')).decode()
        resp2 = self.client.get('/api/bnqo/agent/config', headers={
            'Authorization': f'Bearer {token}',
            'X-BNQO-Timestamp': ts,
            'X-BNQO-Signature': sig,
        })
        self.assertEqual(resp2.status_code, 401)

    def test_config_signed_and_seed_shared(self):
        (key_a, token_a, agent_a), (key_b, token_b, agent_b), link = \
            self._two_agents_with_link()

        resp_a = self._agent_get('/api/bnqo/agent/config', key_a, token_a)
        resp_b = self._agent_get('/api/bnqo/agent/config', key_b, token_b)
        self.assertEqual(resp_a.status_code, 200)
        self.assertEqual(resp_b.status_code, 200)
        cfg_a, cfg_b = resp_a.get_json(), resp_b.get_json()

        # Both configs verify against the CP public key.
        from panel.services.bnqo_crypto import get_cp_pubkey_b64
        cp_pub = get_cp_pubkey_b64()
        _verify_cp_signature(cfg_a, cp_pub)
        _verify_cp_signature(cfg_b, cp_pub)

        # Each agent sees exactly its own link with the peer's address.
        self.assertEqual(len(cfg_a['links']), 1)
        self.assertEqual(cfg_a['links'][0]['peer']['name'], 'agent-b')
        self.assertEqual(cfg_a['links'][0]['direction'], 'a_to_b')
        self.assertEqual(cfg_b['links'][0]['direction'], 'b_to_a')

        # Both sides share the same session seed (HKDF key pairs must match).
        self.assertEqual(cfg_a['links'][0]['session_seed'],
                         cfg_b['links'][0]['session_seed'])
        self.assertEqual(len(cfg_a['links'][0]['session_seed']), 64)

    def test_revoked_agent_rejected(self):
        self._make_enroll_token()
        resp, key = self._enroll()
        token = resp.get_json()['agent_token']
        agent = BnqoAgent.query.filter_by(name='agent-1').one()
        agent.enabled = False
        db.session.commit()
        self.assertEqual(
            self._agent_get('/api/bnqo/agent/config', key, token).status_code, 401)


class BnqoReportTest(BnqoTestBase):
    def _measurement(self, link_id, direction='a_to_b', loss=1.0, seconds_ago=30):
        start = datetime.utcnow() - timedelta(seconds=seconds_ago)
        return {
            'link_id': link_id, 'direction': direction,
            'window_start': start.isoformat() + 'Z',
            'window_end': (start + timedelta(seconds=30)).isoformat() + 'Z',
            'sent': 150, 'received': 148, 'loss_pct': loss,
            'rtt_min_ms': 71.2, 'rtt_avg_ms': 83.5, 'rtt_p95_ms': 120.4,
            'rtt_max_ms': 210.0, 'owd_ms': 41.0, 'clock_quality': 'good',
            'jitter_ms': 6.2, 'reordered': 0, 'duplicated': 1,
            'corrupted': 0, 'burst_max': 2,
        }

    def test_report_accept_and_idempotent_replay(self):
        (key_a, token_a, agent_a), _, link = self._two_agents_with_link()
        batch = {
            'agent_seq': 1,
            'sent_at': datetime.utcnow().isoformat() + 'Z',
            'measurements': [self._measurement(link.id)],
            'icmp': [{'link_id': link.id, 'direction': 'a_to_b', 'sent': 5,
                      'received': 5, 'loss_pct': 0.0, 'rtt_avg_ms': 82.1,
                      'rtt_p95_ms': 95.3}],
            'service_probes': [{'link_id': link.id, 'target_name': 'panel',
                                'ok': True, 'tcp_ms': 80.2, 'tls_ms': 41.5,
                                'http_status': 200, 'error_class': None}],
            'host': {'cpu_pct': 3.1, 'mem_pct': 41.0},
        }
        resp = self._agent_post('/api/bnqo/agent/report', batch, key_a, token_a)
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['accepted'])
        self.assertFalse(payload['duplicate'])
        self.assertEqual(BnqoMeasurement.query.count(), 2)  # udp + icmp rows

        # Replay with the same agent_seq: accepted as duplicate, stores nothing.
        resp2 = self._agent_post('/api/bnqo/agent/report', batch, key_a, token_a)
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.get_json()['duplicate'])
        self.assertEqual(BnqoMeasurement.query.count(), 2)

    def test_report_rejects_foreign_link(self):
        (key_a, token_a, agent_a), (key_b, token_b, agent_b), link = \
            self._two_agents_with_link()
        # agent-a may not report for a link that does not exist for it.
        batch = {'agent_seq': 1,
                 'measurements': [self._measurement(link_id=99999)]}
        resp = self._agent_post('/api/bnqo/agent/report', batch, key_a, token_a)
        self.assertEqual(resp.status_code, 403)

    def test_report_rejects_bad_agent_seq(self):
        (key_a, token_a, _), _, link = self._two_agents_with_link()
        batch = {'agent_seq': 'one',
                 'measurements': [self._measurement(link.id)]}
        resp = self._agent_post('/api/bnqo/agent/report', batch, key_a, token_a)
        self.assertEqual(resp.status_code, 400)

    def test_report_rejects_unsigned(self):
        (_, token_a, _), _, link = self._two_agents_with_link()
        batch = {'agent_seq': 1, 'measurements': [self._measurement(link.id)]}
        resp = self.client.post('/api/bnqo/agent/report', json=batch,
                                headers={'Authorization': f'Bearer {token_a}'})
        self.assertEqual(resp.status_code, 401)


class BnqoAdminApiTest(BnqoTestBase):
    def test_admin_endpoints_require_login(self):
        anon = app.test_client()
        self.assertIn(anon.get('/api/bnqo/agents').status_code, (302, 401))
        self.assertIn(anon.post('/api/bnqo/links', json={}).status_code, (302, 401))
        self.assertIn(anon.get('/pulse/links').status_code, (302, 401))

    def test_enroll_token_creation_returns_install_command(self):
        resp = self.client.post('/api/bnqo/enroll-tokens',
                                json={'role': 'relay', 'ttl_minutes': 30})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn('token', payload)
        self.assertIn('install_command', payload)
        self.assertIn('BNQO_ENROLL_TOKEN', payload['install_command'])
        row = BnqoEnrollToken.query.filter_by(token=payload['token']).one()
        self.assertEqual(row.role, 'relay')

    def test_link_crud_and_diagnose(self):
        (key_a, token_a, agent_a), (key_b, token_b, agent_b), link = \
            self._two_agents_with_link()

        resp = self.client.get('/api/bnqo/links')
        self.assertEqual(resp.status_code, 200)
        links = resp.get_json()['links']
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['name'], 'IR-DE')
        self.assertEqual(links[0]['status'], 'unknown')  # never 'healthy' w/o data

        # Diagnose enqueues a signed RUN_MTR job for each agent.
        resp = self.client.post(f'/api/bnqo/links/{link.id}/diagnose')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()['job_ids']), 2)

        jobs_a = self._agent_get('/api/bnqo/agent/jobs', key_a, token_a).get_json()['jobs']
        jobs_b = self._agent_get('/api/bnqo/agent/jobs', key_b, token_b).get_json()['jobs']
        self.assertEqual(len(jobs_a), 1)
        self.assertEqual(len(jobs_b), 1)
        self.assertEqual(jobs_a[0]['type'], 'RUN_MTR')
        from panel.services.bnqo_crypto import get_cp_pubkey_b64
        _verify_cp_signature(jobs_a[0], get_cp_pubkey_b64())

        # Update + delete.
        resp = self.client.patch(f'/api/bnqo/links/{link.id}',
                                 json={'enabled': False})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.delete(f'/api/bnqo/links/{link.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BnqoLink.query.count(), 0)

    def test_series_endpoint(self):
        (key_a, token_a, agent_a), _, link = self._two_agents_with_link()
        for i in range(5):
            start = datetime.utcnow() - timedelta(seconds=30 * (i + 1))
            db.session.add(BnqoMeasurement(
                link_id=link.id, direction='a_to_b', source='udp',
                window_start=start, window_end=start + timedelta(seconds=30),
                sent=100, received=99, loss_pct=1.0 + i,
                rtt_p95_ms=100.0 + i, jitter_ms=5.0))
        db.session.commit()

        resp = self.client.get(
            f'/api/bnqo/links/{link.id}/series?metric=loss&direction=a_to_b&hours=1')
        self.assertEqual(resp.status_code, 200)
        points = resp.get_json()['points']
        self.assertEqual(len(points), 5)
        self.assertIn('t', points[0])
        self.assertIn('value', points[0])

        resp = self.client.get(
            f'/api/bnqo/links/{link.id}/series?metric=bogus')
        self.assertEqual(resp.status_code, 400)


class BnqoStatusEngineTest(BnqoTestBase):
    def _window_rows(self, link, direction, count, loss, rtt_p95=100.0):
        rows = []
        for i in range(count):
            start = datetime.utcnow() - timedelta(seconds=30 * (i + 1))
            rows.append(BnqoMeasurement(
                link_id=link.id, direction=direction, source='udp',
                window_start=start, window_end=start + timedelta(seconds=30),
                sent=100, received=int(100 - loss), loss_pct=float(loss),
                rtt_min_ms=90.0, rtt_avg_ms=95.0, rtt_p95_ms=rtt_p95,
                rtt_max_ms=110.0, jitter_ms=3.0))
        db.session.add_all(rows)
        # The engine gates on link.last_data_at freshness (set by the report
        # endpoint in production); simulate a just-received batch.
        link.last_data_at = datetime.utcnow()
        db.session.commit()

    def test_no_data_is_unknown_not_healthy(self):
        _, _, link = self._two_agents_with_link()
        bnqo_scheduler_tick()
        db.session.refresh(link)
        self.assertEqual(link.status, 'unknown')

    def test_complete_loss_opens_incident(self):
        (_, _, agent_a), (_, _, agent_b), link = self._two_agents_with_link()
        self._window_rows(link, 'a_to_b', 3, 100.0)
        self._window_rows(link, 'b_to_a', 3, 100.0)
        bnqo_scheduler_tick()
        db.session.refresh(link)
        self.assertIn(link.status, ('unreachable', 'critical'))
        incident = BnqoIncident.query.filter_by(link_id=link.id).first()
        self.assertIsNotNone(incident)
        self.assertEqual(incident.status, 'open')
        self.assertTrue(incident.evidence_json)

    def test_moderate_loss_not_healthy(self):
        _, _, link = self._two_agents_with_link()
        self._window_rows(link, 'a_to_b', 3, 10.0)
        self._window_rows(link, 'b_to_a', 3, 10.0)
        bnqo_scheduler_tick()
        db.session.refresh(link)
        self.assertIn(link.status, ('degraded', 'critical'))
        self.assertNotEqual(link.status, 'healthy')

    def test_incident_ack_and_resolve(self):
        _, _, link = self._two_agents_with_link()
        self._window_rows(link, 'a_to_b', 3, 100.0)
        self._window_rows(link, 'b_to_a', 3, 100.0)
        bnqo_scheduler_tick()
        incident = BnqoIncident.query.filter_by(link_id=link.id,
                                                status='open').first()
        self.assertIsNotNone(incident)

        resp = self.client.get('/api/bnqo/incidents?status=open')
        self.assertEqual(resp.status_code, 200)
        open_ids = [i['id'] for i in resp.get_json()['incidents']]
        self.assertIn(incident.id, open_ids)  # one incident per failing direction

        resp = self.client.post(f'/api/bnqo/incidents/{incident.id}/ack')
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(incident)
        self.assertEqual(incident.status, 'ack')

        resp = self.client.post(f'/api/bnqo/incidents/{incident.id}/resolve')
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(incident)
        self.assertEqual(incident.status, 'resolved')


if __name__ == '__main__':
    unittest.main()
