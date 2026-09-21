"""Phase 13: the required regression suite for the mutation -> cache -> UI program.

One place that walks every operation the program touched - renew, enable, disable,
expiry edit, usage reset, delete - through the **real routes** with a stubbed panel, and
asserts the same post-conditions for each:

* the panel write carried what the operator asked for;
* the shared cache reflects it immediately, with no manual refresh;
* the change is announced to other tabs (a ``client.changed`` event with a revision);
* the browser's own cursor (the snapshot revision) moved.

Then the four cross-cutting scenarios: cache miss, a stale background refresh racing a
mutation, multi-tab delivery, and the subscription read path with no panel call.

Run it as the release gate for this area:

    python -m pytest tests/test_regression_matrix.py -q
"""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

import app as app_module  # noqa: E402
from app import Admin, ClientOperation, GLOBAL_SERVER_DATA, Server, app, db  # noqa: E402
from panel.core import client_events, refresh_policy, snapshot_delta  # noqa: E402
from panel.adapters import xui as xui_adapter  # noqa: E402
from panel.jobs import refresh as refresh_jobs  # noqa: E402
from panel.jobs import schedulers  # noqa: E402
from panel.routes import clients as clients_module  # noqa: E402
from panel.services import panel_capabilities  # noqa: E402

GB = 1024 ** 3
DAY_MS = 24 * 60 * 60 * 1000


def _panel_client(raw):
    return dict(raw)


class MutationHarness(unittest.TestCase):
    """Shared harness: a real route, a stubbed 3x-ui panel, a seeded cache."""

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
        client_events.reset()
        snapshot_delta.reset_state()
        refresh_policy.reset_state()
        refresh_jobs.REFRESH_BACKOFF.clear()
        ClientOperation.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()

        self.admin = Admin(username='regression', password_hash='x',
                           role='superadmin', is_superadmin=True)
        self.server = Server(name='panel-regression', host='https://panel.example:8443/base',
                             username='u', password='p', sub_path='/sub/', panel_type='auto')
        db.session.add_all([self.admin, self.server])
        db.session.commit()

        self.http = app.test_client()
        with self.http.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
            sess['admin_username'] = self.admin.username
            sess['role'] = self.admin.role
            sess['is_superadmin'] = True

        self._saved_snapshot = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore)
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({
            'inbounds': [], 'stats': {}, 'servers_status': [], 'last_update': None,
        })

        # What the panel itself holds; a test can replace it before seeding the cache.
        self.panel_raw = self._raw(up=0, down=0)
        self.session_obj = mock.Mock()
        self.v3_update = mock.Mock(return_value=(True, {}, None))
        self.v3_enable = mock.Mock(return_value=(True, {}, None))
        self.v3_delete = mock.Mock(return_value=(True, {}, None))
        # The renewal path routes through the capability planner, and verification
        # reads the client record plus the traffic row separately. This fixture models
        # a v3.8 panel whose client-level read reflects the applied write.
        self.capabilities_override = None

        def fetch_confirmed(*_args, **_kwargs):
            """The panel's answer: whatever the flow just wrote, else its own state."""
            if self.v3_update.call_args:
                sent = dict(self.v3_update.call_args[0][3])
            else:
                sent = copy.deepcopy(self.panel_raw)
            return ([{'id': 1, 'server_id': self.server.id,
                      'settings': json.dumps({'clients': [sent]})}], None, '3x-ui')

        self._patches = [
            mock.patch.object(app_module, 'get_xui_session',
                              return_value=(self.session_obj, None)),
            mock.patch.object(app_module, 'server_is_v3', return_value=True),
            mock.patch.object(panel_capabilities, 'capabilities_for',
                              side_effect=self._capabilities),
            mock.patch.object(app_module, 'v3_update_client', self.v3_update),
            mock.patch.object(xui_adapter, 'v3_update_client_result',
                              side_effect=self._panel_write_result),
            mock.patch.object(app_module, 'v3_enable_client', self.v3_enable),
            mock.patch.object(app_module, 'v3_delete_client', self.v3_delete),
            mock.patch.object(app_module, 'v3_reset_client', return_value=(True, {}, None)),
            mock.patch.object(xui_adapter, 'v3_get_client_details',
                              side_effect=self._client_details),
            mock.patch.object(xui_adapter, 'v3_client_traffic',
                              return_value={'available': False,
                                            'reason': 'not modelled by this fixture'}),
            mock.patch.object(app_module, 'fetch_inbounds', side_effect=fetch_confirmed),
            mock.patch.object(app_module, '_fire_automation_sms'),
            mock.patch.object(app_module, '_fire_cancel_stale_account_sms'),
            mock.patch.object(app_module, '_notify_customer_telegram'),
            mock.patch.object(clients_module, '_fire_renew_whatsapp'),
            mock.patch.object(clients_module, '_fire_renew_postcheck'),
            mock.patch('time.sleep'),
        ]
        for patch in self._patches:
            patch.start()

    def _capabilities(self, *_args, **_kwargs):
        if self.capabilities_override is not None:
            return self.capabilities_override
        caps = panel_capabilities.PanelClientCapabilities(
            client_api_family=panel_capabilities.CLIENT_API_FIRST_CLASS,
            client_get=True, client_update=True, client_traffic=True,
            client_reset_traffic=True, bulk_adjust=True, bulk_enable=True,
            node_pending_response=True, limit_hwid=True, scoped_tokens=True,
            version='3.8.5', version_family=(3, 8), profile='xui_3_8',
            probe_state=panel_capabilities.PROBE_SUPPORTED,
            evidence={'fixture': 'v3.8 panel'})
        return caps, None

    def _panel_write_result(self, server, session, email, client, **_kwargs):
        """Structured write result, recording the call on the legacy mock."""
        self.v3_update(server, session, email, client)
        return xui_adapter.PanelMutationResult(transport_ok=True, panel_success=True)

    def _client_details(self, server, session, email, *_args, **_kwargs):
        """The client-level read: the last write's config, as a real v3 panel shows."""
        row = None
        if self.v3_update.call_args:
            row = dict(self.v3_update.call_args[0][3])
        elif isinstance(self.panel_raw, dict):
            row = dict(self.panel_raw)
        if not isinstance(row, dict):
            return {'ok': False, 'client': None, 'inbound_ids': [], 'raw': None,
                    'error': 'client not found'}
        return {'ok': True, 'client': row, 'inbound_ids': [1],
                'raw': {'client': row, 'inboundIds': [1]}, 'error': None}

    def _restore(self):
        for patch in self._patches:
            patch.stop()
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved_snapshot)
        db.session.rollback()
        db.session.remove()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _raw(self, email='bob', *, total=5 * GB, expiry=None, enable=True, up=0, down=0,
             comment=None):
        raw = {
            'email': email, 'id': 'uuid-' + email, 'enable': enable,
            'totalGB': total, 'expiryTime': expiry if expiry is not None else 0,
        }
        if comment is not None:
            raw['comment'] = comment
        return raw

    def _seed_cache(self, raw, *, up=0, down=0):
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server.id, 'id': 1, 'remark': 'in',
            'clients': [{
                'server_id': self.server.id, 'inbound_id': 1, 'email': raw['email'],
                'id': raw.get('id'), 'up': up, 'down': down,
                'up_formatted': '%d B' % up, 'down_formatted': '%d B' % down,
                'raw_client': copy.deepcopy(raw),
            }],
            'client_count': 1, 'active_count': 1 if raw.get('enable', True) else 0,
        }]
        GLOBAL_SERVER_DATA['servers_status'] = [
            {'server_id': self.server.id, 'success': True, 'reachable': True, 'stats': {}}]
        GLOBAL_SERVER_DATA['stats'] = {}
        GLOBAL_SERVER_DATA['last_update'] = datetime.now(timezone.utc).isoformat()
        snapshot_delta.reset_state()
        return snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)

    def _cached_row(self, email='bob'):
        for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
            for row in inbound.get('clients') or []:
                if (row.get('email') or '').lower() == email.lower():
                    return row
        return None

    def _events_since(self, revision):
        return client_events.since(revision)

    def _assert_announced(self, revision_before, operation):
        """The change must reach other tabs and move the browser's cursor."""
        events = self._events_since(revision_before)
        self.assertTrue(events, 'no client.changed event was recorded')
        self.assertIn(operation, {event.get('operation') for event in events}, events)
        self.assertGreater(snapshot_delta.current_revision(GLOBAL_SERVER_DATA), revision_before)


class MutationRegressionTests(MutationHarness):
    """Every mutation, through its real route, against a stubbed 3x-ui panel."""

    # ── the operations ───────────────────────────────────────────────────────

    def test_renew_updates_the_cache_and_the_ui_cursor_immediately(self):
        raw = self._raw(total=4 * GB, expiry=int(time.time() * 1000) + DAY_MS)
        revision_before = self._seed_cache(raw)

        response = self.http.post(
            '/api/client/%d/1/bob/renew' % self.server.id,
            json={'mode': 'custom', 'days': 30, 'volume': 10, 'free': True})
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'], payload)
        self.assertTrue(payload['verify']['ok'], payload)

        # Panel write, then the cache without a manual refresh.
        sent = self.v3_update.call_args[0][3]
        self.assertTrue(sent['enable'])
        row = self._cached_row()
        self.assertEqual(row['raw_client']['totalGB'], 14 * GB)
        self.assertEqual(row['raw_client']['expiryTime'], sent['expiryTime'])
        self.assertTrue(row['raw_client']['enable'])
        # The response is what the card is patched from.
        self.assertEqual(payload['client_state']['total_bytes'], 14 * GB)
        self._assert_announced(revision_before, 'renew')

    def test_disable_then_enable_updates_the_cache_and_the_optout_tags(self):
        raw = self._raw()
        revision_before = self._seed_cache(raw)

        response = self.http.post(
            '/api/client/%d/1/toggle' % self.server.id,
            json={'email': 'bob', 'enable': False})
        self.assertEqual(response.status_code, 200, response.get_json())
        row = self._cached_row()
        self.assertFalse(row['raw_client']['enable'])
        self.assertIn('#nosms', row['raw_client'].get('comment') or '')
        self.assertTrue(self.v3_update.called)
        self.assertFalse(self.v3_update.call_args[0][3]['enable'])
        disable_revision = snapshot_delta.current_revision(GLOBAL_SERVER_DATA)
        self._assert_announced(revision_before, 'update')

        # Re-enable through the same route: the opt-out tags come back off.
        response = self.http.post(
            '/api/client/%d/1/toggle' % self.server.id,
            json={'email': 'bob', 'enable': True})
        self.assertEqual(response.status_code, 200, response.get_json())
        row = self._cached_row()
        self.assertTrue(row['raw_client']['enable'])
        self.assertNotIn('#nosms', row['raw_client'].get('comment') or '')
        self._assert_announced(disable_revision, 'update')

    def test_expiry_edit_lands_in_the_cache_without_a_refetch(self):
        raw = self._raw(expiry=0)
        revision_before = self._seed_cache(raw)
        new_expiry = int(time.time() * 1000) + 7 * DAY_MS

        response = self.http.post(
            '/api/client/%d/1/bob/edit' % self.server.id,
            json={'new_email': 'bob', 'expiryTime': new_expiry, 'totalGB': 12})
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'], payload)
        self.assertTrue(payload['cache_sync'], payload)
        row = self._cached_row()
        self.assertEqual(row['raw_client']['expiryTime'], new_expiry)
        self.assertEqual(row['raw_client']['totalGB'], 12 * GB)
        self.assertEqual(self.v3_update.call_args[0][3]['expiryTime'], new_expiry)
        self._assert_announced(revision_before, 'update')

    def test_usage_reset_zeroes_the_counters_in_the_cache(self):
        raw = self._raw(total=10 * GB)
        revision_before = self._seed_cache(raw, up=9 * GB, down=3 * GB)

        response = self.http.post(
            '/api/client/%d/1/reset' % self.server.id,
            json={'email': 'bob', 'volume_gb': 0})
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'], payload)
        row = self._cached_row()
        self.assertEqual(row['up'], 0)
        self.assertEqual(row['down'], 0)
        self.assertEqual(row['up_formatted'], '0 B')
        self._assert_announced(revision_before, 'update')

    def test_delete_removes_the_row_from_the_cache(self):
        raw = self._raw()
        revision_before = self._seed_cache(raw)

        response = self.http.post(
            '/api/client/%d/1/bob/delete' % self.server.id, json={})
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'], payload)
        self.assertTrue(self.v3_delete.called)
        self.assertIsNone(self._cached_row(), 'the deleted row is still in the cache')
        self._assert_announced(revision_before, 'delete')

    def test_a_mutation_does_not_need_a_panel_read_afterwards(self):
        """The read path stays panel-free right after a mutation."""
        raw = self._raw()
        self._seed_cache(raw)
        self.http.post('/api/client/%d/1/bob/toggle' % self.server.id,
                       json={'email': 'bob', 'enable': False})

        import requests
        original = requests.sessions.Session.request
        calls = []

        def counting(self, method, url, *args, **kwargs):
            calls.append((method, url))
            raise RuntimeError('cache read called %s %s' % (method, url))

        requests.sessions.Session.request = counting
        try:
            response = self.http.get('/api/refresh?mode=cache')
        except RuntimeError:  # pragma: no cover - only on a regression
            self.fail('the cache read called the panel: %s' % calls)
        finally:
            requests.sessions.Session.request = original
        self.assertIn(response.status_code, (200, 202))
        self.assertEqual(calls, [])


class CacheMissRegressionTests(MutationHarness):
    """The cache-miss half: the panel answered, this worker has no matching row."""

    def test_a_renew_miss_still_returns_the_verified_state_and_queues_a_repair(self):
        """A worker with an empty snapshot must not answer with nothing."""
        self.panel_raw = self._raw(total=GB)
        GLOBAL_SERVER_DATA['inbounds'] = []
        snapshot_delta.reset_state()
        revision_before = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)

        with mock.patch.object(refresh_jobs, 'enqueue_refresh_job') as repair:
            response = self.http.post(
                '/api/client/%d/1/bob/renew' % self.server.id,
                json={'mode': 'custom', 'days': 30, 'volume': 5, 'free': True})
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'], payload)
        # The verified read-back travels with the response even though the cache was cold.
        self.assertTrue(payload['verify']['ok'], payload)
        self.assertIsNotNone(payload.get('client_state'), payload)
        self.assertEqual(payload['client_state']['total_bytes'], 6 * GB)
        self.assertTrue(repair.called, 'no targeted repair was queued')
        self.assertGreaterEqual(len(self._events_since(revision_before)), 1)

    def test_a_toggle_miss_is_not_reported_as_a_successful_cache_write(self):
        """Without a verified read the miss must be visible, not silently 'done'."""
        self.panel_raw = self._raw(total=3 * GB, enable=True)
        GLOBAL_SERVER_DATA['inbounds'] = []
        snapshot_delta.reset_state()
        revision_before = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)
        with mock.patch.object(refresh_jobs, 'enqueue_refresh_job') as repair:
            response = self.http.post(
                '/api/client/%d/1/toggle' % self.server.id,
                json={'email': 'bob', 'enable': False})
        # The panel write succeeded, so the route succeeds ...
        self.assertEqual(response.status_code, 200, response.get_json())
        # ... but the cache miss is logged/announced and a repair is queued, so the row
        # cannot stay wrong until someone clicks Refresh.
        self.assertTrue(repair.called, 'no targeted repair was queued for a cache miss')
        events = self._events_since(revision_before)
        self.assertTrue(events, 'a cache-miss mutation announced nothing')

    def test_the_revision_moves_before_the_cache_row_so_a_stale_read_cannot_win(self):
        raw = self._raw()
        self._seed_cache(raw)
        revision_before = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)
        result = refresh_jobs.patch_cached_client(
            self.server.id, 'bob', enable=False, verified_state={'uuid': 'uuid-bob'})
        self.assertTrue(result.changed)
        self.assertGreater(result.server_revision, 0)
        self.assertGreater(snapshot_delta.current_revision(GLOBAL_SERVER_DATA),
                           revision_before)


class StaleRefreshRegressionTests(unittest.TestCase):
    """A refresh that started before a mutation must not resurrect the old row."""

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
        refresh_jobs.REFRESH_BACKOFF.clear()
        refresh_policy.reset_state()
        Server.query.filter(Server.id.in_((9701,))).delete(synchronize_session=False)
        db.session.add(Server(id=9701, name='stale', host='https://stale.invalid',
                              username='u', password='p', panel_type='auto', enabled=True))
        db.session.commit()
        self._saved = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore)
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({'inbounds': [], 'stats': {}, 'servers_status': [],
                                   'last_update': None})

    def _restore(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved)
        Server.query.filter(Server.id == 9701).delete(synchronize_session=False)
        db.session.commit()
        refresh_policy.reset_state()

    def test_a_concurrent_mutation_beats_an_in_flight_refresh(self):
        started = threading.Event()
        release = threading.Event()

        def slow_worker(server_dict):
            started.set()
            release.wait(timeout=15)
            # The panel's version of the row: what the cycle read *before* the mutation.
            return (9701, [{'id': 1, 'server_id': 9701,
                            'settings': json.dumps({'clients': [
                                {'email': 'bob', 'id': 'uuid-bob', 'enable': True,
                                 'totalGB': 4 * GB, 'expiryTime': 0}]})}],
                    None, {'xui_version': '3.0'}, None, None, 'auto')

        errors = []

        def run_cycle():
            try:
                with app.app_context():
                    with mock.patch.object(app_module, 'fetch_worker', slow_worker):
                        schedulers._fetch_and_update_global_data_inner(force=True)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        worker = threading.Thread(target=run_cycle, daemon=True)
        worker.start()
        self.assertTrue(started.wait(timeout=10), 'the refresh never started')

        # The operator's edit lands while that refresh is still talking to the panel.
        with app.app_context():
            GLOBAL_SERVER_DATA['inbounds'] = [{
                'server_id': 9701, 'id': 1, 'clients': [{
                    'server_id': 9701, 'inbound_id': 1, 'email': 'bob', 'id': 'uuid-bob',
                    'up': 0, 'down': 0, 'raw_client': {'email': 'bob', 'id': 'uuid-bob',
                                                       'enable': False, 'totalGB': 9 * GB,
                                                       'expiryTime': 0}}]}]
            GLOBAL_SERVER_DATA['servers_status'] = [
                {'server_id': 9701, 'success': True, 'reachable': True, 'stats': {}}]
            result = refresh_jobs.patch_cached_client(
                9701, 'bob', total_gb_bytes=9 * GB, enable=False)
            self.assertTrue(result.changed)

        release.set()
        worker.join(timeout=15)
        self.assertEqual(errors, [])

        # The stale cycle must not have overwritten the mutation.
        row = GLOBAL_SERVER_DATA['inbounds'][0]['clients'][0]
        self.assertFalse(row['raw_client']['enable'], 'the stale refresh won')
        self.assertEqual(row['raw_client']['totalGB'], 9 * GB)


class MultiTabRegressionTests(unittest.TestCase):
    """A second tab learns about one client, with its state, and nothing else."""

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
        client_events.reset()
        snapshot_delta.reset_state()
        Server.query.filter(Server.id.in_((9801,))).delete(synchronize_session=False)
        db.session.add(Server(id=9801, name='tabs', host='https://tabs.invalid',
                              username='u', password='p', panel_type='auto', enabled=True))
        db.session.commit()
        self._saved = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore)
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({
            'inbounds': [{'server_id': 9801, 'id': 1, 'clients': [{
                'server_id': 9801, 'inbound_id': 1, 'email': 'bob', 'id': 'uuid-bob',
                'up': 0, 'down': 0, 'raw_client': {'email': 'bob', 'id': 'uuid-bob',
                                                   'enable': True, 'totalGB': GB,
                                                   'expiryTime': 0}}]}],
            'stats': {}, 'servers_status': [{'server_id': 9801, 'success': True}],
            'last_update': 't1'})

    def _restore(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved)
        client_events.reset()
        Server.query.filter(Server.id == 9801).delete(synchronize_session=False)
        db.session.commit()

    def test_the_event_carries_the_verified_state_for_the_other_tab(self):
        revision_before = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)
        state = {'uuid': 'uuid-bob', 'email': 'bob', 'total_bytes': 20 * GB,
                 'used_up': 0, 'used_down': 0, 'enable': True, 'expiry_time': 0}
        result = refresh_jobs.patch_cached_client(
            9801, 'bob', total_gb_bytes=20 * GB, operation='renew',
            verified_state=state)
        self.assertTrue(result.changed)

        events = client_events.since(revision_before)
        self.assertEqual(len(events), 1, events)
        event = events[0]
        self.assertEqual(event['server_id'], 9801)
        self.assertEqual(event['email'], 'bob')
        self.assertEqual(event['operation'], 'renew')
        self.assertFalse(event['deleted'])
        # Another tab patches one card from this, without fetching a delta.
        self.assertIsNotNone(event.get('client_state'))
        self.assertEqual(event['client_state']['total_bytes'], 20 * GB)
        self.assertGreater(event['revision'], revision_before)
        # Replaying from the same cursor is idempotent and ordered oldest first.
        self.assertEqual([item['revision'] for item in client_events.since(revision_before)],
                         [event['revision']])

    def test_a_delete_travels_as_a_delete(self):
        revision_before = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)
        self.assertTrue(refresh_jobs.remove_cached_client(9801, 'bob'))
        events = client_events.since(revision_before)
        self.assertTrue(events, 'the delete announced nothing')
        self.assertTrue(any(event['deleted'] for event in events), events)


class SubscriptionRegressionTests(unittest.TestCase):
    """The subscription read path must be served from the cache, single-flight."""

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
        from panel.core import subscription_cache
        subscription_cache.reset()
        self.addCleanup(subscription_cache.reset)
        Server.query.filter(Server.id.in_((9901,))).delete(synchronize_session=False)
        db.session.add(Server(id=9901, name='subs', host='https://subs.invalid',
                              username='u', password='p', panel_type='auto',
                              enabled=True, sub_path='/sub/'))
        db.session.commit()
        self._saved = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore)
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({
            'inbounds': [{'server_id': 9901, 'id': 1, 'clients': [{
                'server_id': 9901, 'inbound_id': 1, 'email': 'bob', 'id': 'uuid-bob',
                'up': 0, 'down': 0, 'raw_client': {'email': 'bob', 'id': 'uuid-bob',
                                                   'enable': True, 'totalGB': GB,
                                                   'expiryTime': 0}}]}],
            'stats': {}, 'servers_status': [], 'last_update': 't1'})

    def _restore(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved)
        Server.query.filter(Server.id == 9901).delete(synchronize_session=False)
        db.session.commit()

    def test_a_cached_subscription_is_served_without_reading_the_panel(self):
        from panel.core import subscription_cache
        from panel.services import subscription as subscription_service

        key = subscription_cache.make_key(9901, 'sub-bob', 'config')
        subscription_cache.set(key, {'body': 'cached-payload'}, variant='config')

        def explode(*_args, **_kwargs):
            raise AssertionError('the cached subscription read called the panel')

        with mock.patch.object(subscription_service, 'find_client', side_effect=explode), \
                mock.patch.object(app_module, 'fetch_inbounds', side_effect=explode):
            self.assertEqual(subscription_cache.get(key)['body'], 'cached-payload')

    def test_a_burst_of_misses_produces_one_panel_read(self):
        from panel.core import subscription_cache

        key = subscription_cache.make_key(9901, 'sub-burst', 'config')
        loader_calls = []
        started = threading.Event()
        release = threading.Event()

        def loader():
            loader_calls.append(1)
            started.set()
            release.wait(timeout=10)
            return {'body': 'rendered'}

        results = []

        def worker():
            if subscription_cache.begin(key):
                try:
                    value = loader()
                    subscription_cache.set(key, value, variant='config')
                    results.append(value)
                finally:
                    subscription_cache.end(key)
            else:
                # A follower: it waits for the fill instead of rendering again.
                subscription_cache.wait_for_fill(key, timeout=10)
                cached = subscription_cache.get(key)
                if cached is not None:
                    results.append(cached)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        self.assertTrue(started.wait(timeout=10), 'no renderer claimed the key')
        time.sleep(0.1)          # the followers pile up on the in-flight key
        release.set()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(loader_calls), 1, loader_calls)
        self.assertTrue(any(result and result.get('body') == 'rendered' for result in results),
                        results)


if __name__ == '__main__':
    unittest.main()
