"""Renewal consistency end to end: baseline, panel write, read-back, cache, fence.

A renewal is a read-modify-write against a remote panel whose state Eve keeps in a
shared in-memory snapshot:

    baseline -> new cap/expiry -> panel write -> read-back -> write-through -> fence

Every step can answer with a DIFFERENT value, and each mismatch has been a real bug
in this area, so each scenario below pins the number, not just "it did not raise":

* a cached row older than ``EVE_RENEW_BASELINE_MAX_AGE_SECONDS`` used as the
  baseline is a WRONG baseline, not a slow one: the panel is told a smaller
  ``up``/``down`` than it holds (free traffic for the customer) and the operator's
  message quotes a remaining volume the account never had;
* a read-back that loses to the cached row makes the response and the dashboard
  report pre-mutation telemetry right after a renewal;
* a background read whose aggregate inbound list predates (or lags) the write
  republishes the old counters, so the renewal looks like it undid itself, and the
  aggregate view and the client-level view of the same panel disagree.

The renew route and the single-server read path are the real ones; only the 3x-ui
adapter calls are stubbed. No network and no Redis: the in-process fence fallback is
part of the contract on a single-process install.

Run it with:

    $env:EVE_SKIP_IMPORT_MIGRATIONS='1'; .\.venv\Scripts\python.exe -m unittest tests.test_renew_consistency -v
"""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from app import (  # noqa: E402
    Admin,
    ClientOperation,
    GLOBAL_SERVER_DATA,
    Server,
    Transaction,
    app,
    db,
)
from panel.core import client_events, refresh_policy, snapshot_delta  # noqa: E402
from panel.adapters import xui as xui_adapter  # noqa: E402
from panel.jobs import refresh as refresh_jobs  # noqa: E402
from panel.routes import clients as clients_module  # noqa: E402
from panel.services import panel_capabilities  # noqa: E402
from panel.services import subscription as subscription_service  # noqa: E402

GB = 1024 ** 3
INBOUND_ID = 1
EMAIL = 'bob'


def _gb(value):
    """Bytes as GB. Only used where a float comparison is the honest one."""
    return int(value) / float(GB)


def _gb_bytes(value):
    """Whole bytes for a GB figure a test names declaratively (2.71 GB)."""
    return int(float(value) * GB)


def _raw_client(email=EMAIL, *, cap, up=0, down=0, expiry=0, enable=True, uuid=None):
    """One X-UI client object, in the shape the panel and the cache both use."""
    return {
        'id': uuid or ('uuid-' + email), 'email': email, 'subId': 'sub-' + email,
        'comment': '', 'enable': enable, 'expiryTime': expiry, 'totalGB': cap,
        'limitIp': 0, 'flow': '', 'tgId': '', 'reset': 0, 'up': up, 'down': down,
    }


class PanelState:
    """The panel's own truth for one client, plus the two views a read can get.

    ``aggregate_inbounds()`` is the inbound list; the panel's traffic counters reach
    Eve through its ``clientStats``, which is why the aggregate list is the view that
    can lag a write. ``direct_client()`` is the v3 client-level read, which reflects
    a write immediately. Keeping the two views apart is the point: the route reads
    both and they may disagree for a poll.
    """

    def __init__(self, server_id, email=EMAIL, *, cap=0, up=0, down=0, expiry=0,
                 enable=True):
        self.server_id = server_id
        self.email = email
        self.cap = cap
        self.up = up
        self.down = down
        self.expiry = expiry
        self.enable = enable
        #: The client dict of the last write. A panel applies a write to its client
        #: record immediately; a view that has not caught up keeps the old config.
        self.applied = None
        self.lag_config = False
        #: The client-level read, or None when that endpoint is unavailable.
        self.direct = None

    def _config(self, key, fallback):
        """One configuration field, from the applied write unless this view lags."""
        if self.applied and not self.lag_config and key in self.applied:
            return self.applied[key]
        return fallback

    def aggregate_inbounds(self, *, cap=None, up=None, down=None):
        """The inbound list a read returns. Overrides model a partly caught-up view."""
        client = _raw_client(
            self.email,
            cap=self._config('totalGB', self.cap) if cap is None else cap,
            expiry=self._config('expiryTime', self.expiry),
            enable=bool(self._config('enable', self.enable)),
            up=self.up if up is None else up,
            down=self.down if down is None else down,
        )
        return [{
            'id': INBOUND_ID, 'server_id': self.server_id, 'remark': 'in',
            'protocol': 'vless', 'enable': True,
            'settings': json.dumps({'clients': [client]}),
            'clientStats': [{
                'email': self.email,
                'up': self.up if up is None else up,
                'down': self.down if down is None else down,
            }],
        }]

    def direct_client(self):
        """The client-level read, or None. Fresh by construction when a test sets it."""
        return copy.deepcopy(self.direct) if isinstance(self.direct, dict) else None


class RenewConsistencyTests(unittest.TestCase):
    """One panel, one account, the real renew route and the real read path."""

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
        Transaction.query.delete()
        ClientOperation.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()

        self.admin = Admin(username='consistency', password_hash='x',
                           role='superadmin', is_superadmin=True)
        self.server = Server(name='panel-consistency',
                             host='https://panel.example:8443/base',
                             username='u', password='p', sub_path='/sub/',
                             panel_type='auto', enabled=True)
        db.session.add_all([self.admin, self.server])
        db.session.commit()

        self.http = app.test_client()
        with self.http.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
            sess['admin_username'] = self.admin.username
            sess['role'] = self.admin.role
            sess['is_superadmin'] = True

        self._saved_snapshot = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({'inbounds': [], 'stats': {}, 'servers_status': [],
                                   'last_update': None})

        self.panel = PanelState(self.server.id)
        #: Optional overrides for the capability answer and the panel's nodePending
        #: flag, so a test can model an older v3 panel or a node that is still syncing.
        self.capabilities_override = None
        self.node_pending = False
        #: Membership modelling for the multi-inbound divergence tests: the panel's own
        #: inboundIds, plus a per-inbound enable flag for the extra inbounds.
        self.membership_ids = [INBOUND_ID]
        self.membership_enable = {}
        #: Answers the background read path gets, in order. A test that models a read
        #: which STARTED before the mutation parks the pre-mutation list here.
        self.background_answers = []
        self.route_fetch_modes = []
        self.session_obj = mock.Mock()
        self.v3_update = mock.Mock(side_effect=self._panel_write)

        self.route_fetch = mock.Mock(side_effect=self._route_fetch)
        self.background_fetch = mock.Mock(side_effect=self._background_fetch)
        self._patches = [
            # -- the renew route's adapter calls ------------------------------
            mock.patch.object(app_module, 'get_xui_session',
                              return_value=(self.session_obj, None)),
            mock.patch.object(app_module, 'server_is_v3', return_value=True),
            # The renewal path asks the capability planner which API family this
            # panel is, instead of a boolean. This fixture models a v3.8 panel: the
            # first-class family with bulkEnable and nodePending available.
            mock.patch.object(panel_capabilities, 'capabilities_for',
                              side_effect=self._capabilities),
            mock.patch.object(app_module, 'v3_update_client', self.v3_update),
            mock.patch.object(xui_adapter, 'v3_update_client_result',
                              side_effect=self._panel_write_result),
            mock.patch.object(app_module, 'v3_enable_client',
                              return_value=(True, {}, None)),
            mock.patch.object(app_module, 'v3_reset_client',
                              return_value=(True, {}, None)),
            mock.patch.object(app_module, '_v3_get_client',
                              side_effect=lambda *_a, **_k: self.panel.direct_client()),
            # The layered verification reads the client record (with its membership
            # list) and the traffic row separately. This panel exposes the client
            # record and has no traffic endpoint, which is a real v3 shape.
            mock.patch.object(xui_adapter, 'v3_get_client_details',
                              side_effect=self._client_details),
            mock.patch.object(xui_adapter, 'v3_client_traffic',
                              return_value={'available': False,
                                            'reason': 'not modelled by this fixture'}),
            mock.patch.object(app_module, 'fetch_inbounds', self.route_fetch),
            mock.patch.object(app_module, '_fire_automation_sms'),
            mock.patch.object(app_module, '_fire_cancel_stale_account_sms'),
            mock.patch.object(app_module, '_notify_customer_telegram'),
            mock.patch.object(clients_module, '_fire_renew_whatsapp'),
            mock.patch.object(clients_module, '_fire_renew_postcheck'),
            # -- the single-server read path's adapter calls ------------------
            mock.patch.object(refresh_jobs, 'get_xui_session',
                              return_value=(self.session_obj, None)),
            mock.patch.object(refresh_jobs, 'fetch_inbounds', self.background_fetch),
            mock.patch.object(refresh_jobs, 'fetch_onlines', return_value=({}, None)),
            mock.patch.object(refresh_jobs, 'fetch_server_status',
                              return_value=({}, None, None)),
            mock.patch.object(refresh_jobs, 'resolve_server_compatibility'),
            mock.patch.object(refresh_jobs, 'fetch_subscription_profile_metadata'),
            mock.patch.object(refresh_jobs, 'persist_detected_panel_type',
                              return_value=False),
            # Pre-warming public subscription responses is a side effect of a read,
            # not the guard under test; it would otherwise try to reach a panel.
            mock.patch.object(subscription_service, 'warm_subscription_cache',
                              return_value=0),
            mock.patch('time.sleep'),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._restore)

    def _restore(self):
        for patch in reversed(self._patches):
            patch.stop()
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved_snapshot)
        refresh_policy.reset_state()
        db.session.rollback()
        db.session.remove()

    # -- the stubbed panel ----------------------------------------------------

    def _panel_write(self, _server, _session, _email, client):
        """The v3 update by email: the panel applied the config it was sent."""
        self.panel.applied = copy.deepcopy(client)
        return True, {}, None

    def _capabilities(self, *_args, **_kwargs):
        """This fixture's panel: v3.8, first-class, bulkEnable + nodePending available."""
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

    def _panel_write_result(self, _server, _session, email, client, **_kwargs):
        """The structured result the route now consumes (nodePending included).

        The legacy mock is invoked first so the existing call assertions
        (``self.v3_update.call_args`` / ``call_count``) keep recording the write.
        """
        self.v3_update(_server, _session, email, client)
        return xui_adapter.PanelMutationResult(
            transport_ok=True, panel_success=True,
            node_pending=bool(self.node_pending))

    def _client_details(self, _server, _session, email, *_args, **_kwargs):
        """GET /clients/get/{email}: the client record plus its membership list.

        When a test does not set ``panel.direct`` explicitly, the client-level read is
        modelled as reflecting the applied write - which is what a real v3 panel does,
        and what makes this view the fresh one in the divergence tests.
        """
        client = self.panel.direct_client()
        if client is None:
            aggregate = self.panel.aggregate_inbounds()
            parsed = json.loads(aggregate[0]['settings'])
            client = parsed['clients'][0]
        return {'ok': True, 'client': client, 'inbound_ids': list(self.membership_ids),
                'raw': {'client': client, 'inboundIds': list(self.membership_ids)},
                'error': None}

    def _route_fetch(self, *_args, **_kwargs):
        """What the route's fetch_inbounds returns: the aggregate inbound list."""
        self.route_fetch_modes.append(bool(_kwargs.get('force_fresh')))
        inbounds = copy.deepcopy(self.panel.aggregate_inbounds())
        for inbound_id in self.membership_ids:
            if inbound_id == INBOUND_ID:
                continue
            enabled = bool(self.membership_enable.get(inbound_id, True))
            extra = copy.deepcopy(self.panel.aggregate_inbounds())[0]
            extra['id'] = inbound_id
            settings = json.loads(extra['settings'])
            for client in settings['clients']:
                client['enable'] = enabled
            extra['settings'] = json.dumps(settings)
            inbounds.append(extra)
        return inbounds, None, '3x-ui'

    def _background_fetch(self, *_args, **_kwargs):
        """What the background read returns: a parked older list, else the live one."""
        if self.background_answers:
            answer = self.background_answers.pop(0)
        else:
            answer = self.panel.aggregate_inbounds()
        return copy.deepcopy(answer), None, '3x-ui'

    # -- helpers --------------------------------------------------------------

    def _seed_cache(self, *, cap, up=0, down=0, expiry=0, enable=True,
                    age_seconds=None):
        """Seed the shared snapshot's single row for EMAIL; returns (row, revision).

        ``age_seconds`` writes the row's own per-layer telemetry stamp, which is what
        the renewal's baseline gate reads; None leaves it unstamped so the snapshot's
        ``last_update`` (fresh here) is the bound.
        """
        raw = _raw_client(EMAIL, cap=cap, expiry=expiry, enable=enable)
        row = {
            'server_id': self.server.id, 'inbound_id': INBOUND_ID, 'email': EMAIL,
            'id': raw['id'], 'up': up, 'down': down,
            'up_formatted': app_module.format_bytes(up),
            'down_formatted': app_module.format_bytes(down),
            'raw_client': copy.deepcopy(raw),
        }
        if age_seconds is not None:
            row['telemetry_updated_at'] = (
                datetime.utcnow() - timedelta(seconds=age_seconds)).isoformat()
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server.id, 'id': INBOUND_ID, 'remark': 'in',
            'protocol': 'vless', 'enable': True, 'clients': [row],
            'client_count': 1, 'active_count': 1 if enable else 0,
        }]
        GLOBAL_SERVER_DATA['servers_status'] = [
            {'server_id': self.server.id, 'success': True, 'reachable': True,
             'stats': {}}]
        GLOBAL_SERVER_DATA['stats'] = {}
        GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()
        refresh_jobs._recompute_cached_client(row)
        snapshot_delta.reset_state()
        return row, snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)

    def _cached_row(self):
        for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
            for row in inbound.get('clients') or []:
                if (row.get('email') or '').lower() == EMAIL:
                    return row
        return None

    def _renew(self, **payload):
        """Drive the real renew route and return the body of a successful renewal."""
        body = {'mode': 'custom', 'days': 0, 'free': True}
        body.update(payload)
        response = self.http.post(
            '/api/client/%d/%d/%s/renew' % (self.server.id, INBOUND_ID, EMAIL),
            json=body)
        result = response.get_json()
        self.assertEqual(response.status_code, 200, result)
        self.assertTrue(result.get('success'), result)
        return result

    def _background_read(self):
        """Run the real single-server read path (what the scheduler polls with)."""
        return refresh_jobs._fetch_and_update_server_data_inner(self.server.id)

    def _fence(self, *, now=None):
        return refresh_policy.client_fences(self.server.id, now=now).get(EMAIL)

    def _client_state(self, payload):
        state = payload.get('client_state')
        self.assertIsNotNone(state, payload)
        return state

    # -- A. the stale cache must not be the baseline --------------------------

    def test_a_a_stale_cache_row_is_rejected_as_the_renewal_baseline(self):
        """Cache says 2.71 GB left, the panel says 0, the renewal grants 20 GB.

        Why: the new cap and the ledger's "previous" figures are derived from the
        pre-mutation traffic state, so taking it from a row that is older than
        ``EVE_RENEW_BASELINE_MAX_AGE_SECONDS`` tells the panel LESS usage than it
        holds (the customer quietly gains the difference on every renewal) and
        quotes a remaining volume the account never had. The age gate must send the
        route to the panel, and the panel's numbers must survive to the cache, the
        response and the operator's copy.
        """
        stale_seconds = max(600.0, 10 * refresh_policy.baseline_max_age_seconds())
        # The stale row is optimistic in BOTH directions: an older cap AND older
        # counters, so either one alone is enough to expose a fast path that reused it.
        cached_cap = 25 * GB
        cached_used = cached_cap - _gb_bytes(2.71)
        row, _revision = self._seed_cache(cap=cached_cap, up=cached_used, down=0,
                                          age_seconds=stale_seconds)
        self.assertEqual(row['remaining_bytes'], _gb_bytes(2.71))

        # The panel's own state: a 20 GB cap, fully used, so 0 GB is left.
        self.panel.cap = 20 * GB
        self.panel.up = 20 * GB
        self.panel.down = 0

        payload = self._renew(volume=20, reset_traffic=False)

        # 1. The decision itself travels with the response.
        timing = payload['timing']
        self.assertEqual(timing['baseline_source'], 'panel')
        self.assertTrue(timing['cache_baseline_rejected'])
        self.assertFalse(timing['used_cache_client'])
        self.assertGreater(timing['cache_baseline_age_seconds'],
                           refresh_policy.baseline_max_age_seconds())
        # A baseline read BEFORE the write and the read-back AFTER it: two panel
        # reads, where the fast path would have made one.
        self.assertEqual(len(self.route_fetch.call_args_list), 2)
        self.assertEqual(self.route_fetch_modes, [False, True])

        # 2. The panel is told the cap and the traffic it really holds (a 20 GB cap
        #    at 20 GB used, not the cached 25 GB at 22.29 GB), and the new cap is the
        #    PANEL's baseline cap plus the grant.
        sent = self.v3_update.call_args.args[3]
        self.assertEqual(sent['up'], 20 * GB)
        self.assertEqual(sent['down'], 0)
        self.assertEqual(sent['totalGB'], 40 * GB)

        # 3. The read-back is what the cache and the response adopt.
        self.assertTrue(payload['verify']['ok'], payload['verify'])
        self.assertEqual(payload['verify']['observed']['totalGB'], 40 * GB)
        row = self._cached_row()
        self.assertEqual(row['raw_client']['totalGB'], 40 * GB)
        self.assertEqual(row['up'], 20 * GB)
        self.assertEqual(row['remaining_bytes'], 20 * GB)
        state = self._client_state(payload)
        self.assertEqual(state['total_bytes'], 40 * GB)
        self.assertEqual(state['used_up'], 20 * GB)
        self.assertEqual(state['remaining_bytes'], 20 * GB)
        # The write-through hit, so nothing is left waiting for a repair fetch.
        self.assertTrue(payload['cache_sync'], payload)
        # The operator's copy quotes the truth: 20 GB free, not 2.71 + 20 = 22.71.
        self.assertEqual(payload['tpl_vars']['volume_label'], '20.00GB')
        self.assertEqual(payload['tpl_vars']['volume'], 20)
        # The figures only the panel could have supplied, asserted as counter examples:
        # a cache-based baseline sends a 45 GB cap at 22.29 GB used and quotes
        # "22.71GB" on this same request (probed by disabling the age gate).
        self.assertNotEqual(row['raw_client']['totalGB'], cached_cap + 20 * GB)
        self.assertNotEqual(row['remaining_bytes'], _gb_bytes(22.71))

    # -- B. the fresh fast path must stay exact -------------------------------

    def test_b_a_fresh_cache_baseline_keeps_panel_cache_and_response_agreeing(self):
        """Panel and cache agree on 2.71 GB free; the renewal grants 20 GB.

        Why: refusing every cached row would be correct but slow, and reusing a row
        is safe only while it still describes the panel's present. When it does, the
        same number has to come out of the panel write, the read-back, the cached row
        and the response -- a fast path that disagrees with the panel is the bug the
        gate exists for, not a performance trade.
        """
        cap = _gb_bytes(2.71)
        row, _revision = self._seed_cache(cap=cap, up=0, down=0, age_seconds=0)
        self.assertEqual(row['remaining_bytes'], cap)
        self.panel.cap = cap
        self.panel.up = 0
        self.panel.down = 0

        payload = self._renew(volume=20, reset_traffic=False)

        expected_cap = cap + 20 * GB
        timing = payload['timing']
        self.assertEqual(timing['baseline_source'], 'cache')
        self.assertTrue(timing['used_cache_client'])
        self.assertNotIn('cache_baseline_rejected', timing)
        self.assertLessEqual(timing['cache_baseline_age_seconds'],
                             refresh_policy.baseline_max_age_seconds())
        # No baseline panel read: only the read-back after the write.
        self.assertEqual(len(self.route_fetch.call_args_list), 1)
        self.assertEqual(self.route_fetch_modes, [True])
        self.assertEqual(self.v3_update.call_args.args[3]['totalGB'], expected_cap)

        self.assertTrue(payload['verify']['ok'], payload['verify'])
        self.assertEqual(payload['verify']['observed']['totalGB'], expected_cap)
        row = self._cached_row()
        self.assertEqual(row['raw_client']['totalGB'], expected_cap)
        self.assertEqual(row['remaining_bytes'], expected_cap)
        state = self._client_state(payload)
        self.assertEqual(state['total_bytes'], expected_cap)
        self.assertEqual(state['remaining_bytes'], expected_cap)
        # 2.71 + 20 = 22.71 GB, asserted in GB to two places.
        self.assertAlmostEqual(_gb(row['raw_client']['totalGB']), 22.71, places=2)
        self.assertAlmostEqual(_gb(state['total_bytes']), 22.71, places=2)
        # The cache and the response are the same number, not two roundings of it.
        self.assertEqual(state['total_bytes'], row['raw_client']['totalGB'])
        self.assertEqual(state['remaining_bytes'], row['remaining_bytes'])

    # -- C. the verified counters win over the cached row ---------------------

    def test_c_verified_counters_beat_the_stale_cached_ones(self):
        """The cached row says 1 GB used; the panel's read-back says 4 GB and 1 GB.

        Why: adopting the row instead of the read-back is how a renewal reported the
        pre-mutation traffic -- the card, the response and the event for the other tab
        all showed numbers the panel had already replaced.
        """
        cached_up, cached_down = 1 * GB, 0
        panel_up, panel_down = 4 * GB, 1 * GB
        row, revision_before = self._seed_cache(cap=10 * GB, up=cached_up,
                                                down=cached_down, age_seconds=0)
        self.assertEqual(row['up'], cached_up)
        self.panel.cap = 10 * GB
        self.panel.up = panel_up
        self.panel.down = panel_down

        payload = self._renew(volume=5, reset_traffic=False)

        expected_cap = 15 * GB
        self.assertTrue(payload['verify']['ok'], payload['verify'])
        self.assertEqual(payload['verify']['observed']['up'], panel_up)
        self.assertEqual(payload['verify']['observed']['down'], panel_down)
        self.assertEqual(payload['verify']['observed']['totalGB'], expected_cap)

        row = self._cached_row()
        self.assertEqual(row['up'], panel_up)
        self.assertEqual(row['down'], panel_down)
        self.assertEqual(row['raw_client']['totalGB'], expected_cap)
        self.assertEqual(row['remaining_bytes'], 10 * GB)

        state = self._client_state(payload)
        self.assertEqual(state['used_up'], panel_up)
        self.assertEqual(state['used_down'], panel_down)
        self.assertEqual(state['total_bytes'], expected_cap)
        self.assertEqual(state['remaining_bytes'], 10 * GB)

        # The response's mutation block is the browser's cursor update, so it has to
        # describe the same write-through the cache just took.
        self.assertTrue(payload['cache_sync'], payload)
        mutation = payload['mutation']
        self.assertEqual(mutation['operation'], 'renew')
        self.assertTrue(mutation['verified'])
        self.assertTrue(mutation['changed'])
        self.assertGreater(mutation['snapshot_revision'], revision_before)

        # The other tab patches one card from the recorded event, so it needs the
        # verified counters too.
        events = [event for event in client_events.since(revision_before)
                  if event.get('email') == EMAIL]
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]['operation'], 'renew')
        event_state = events[0]['client_state']
        self.assertIsNotNone(event_state, events[0])
        self.assertEqual(event_state['used_up'], panel_up)
        self.assertEqual(event_state['used_down'], panel_down)
        self.assertEqual(event_state['total_bytes'], expected_cap)

        # Nowhere is the pre-mutation figure left behind.
        self.assertNotEqual(row['up'], cached_up)
        self.assertNotEqual(state['used_up'], cached_up)
        self.assertNotEqual(event_state['used_up'], cached_up)

    # -- D. a read that predates the renewal cannot revert the counters -------

    def test_d_a_background_read_predating_the_renewal_cannot_revert_counters(self):
        """A poll that read the panel BEFORE the renewal finishes after it.

        Why: the aggregate inbound list propagates a write more slowly than the
        client-level endpoint, so the next poll republishes the pre-mutation counters
        and the renewal appears to undo itself. The fence has to hold the verified
        counters against that read, and release them as soon as a read comes back at
        or above them so it can never pin a value the panel genuinely changed.
        """
        _row, _revision = self._seed_cache(cap=10 * GB, up=1 * GB, down=0,
                                           age_seconds=0)
        self.panel.cap = 10 * GB
        self.panel.up = 1 * GB
        self.panel.down = 0

        # The read already in flight: the panel answered before the renewal.
        started_before = copy.deepcopy(self.panel.aggregate_inbounds())
        self.assertEqual(started_before[0]['clientStats'][0]['up'], 1 * GB)

        # The account kept using traffic while the renewal was in flight, which is
        # exactly why the older read is not the present state.
        self.panel.up = 2 * GB
        revision_before = refresh_jobs.get_server_revision(self.server.id)
        payload = self._renew(volume=20, reset_traffic=False)

        expected_cap = 30 * GB
        self.assertTrue(payload['verify']['ok'], payload['verify'])
        self.assertEqual(payload['verify']['observed']['up'], 2 * GB)
        # The panel write carried the cached baseline (1 GB); the read-back is what
        # corrects the counters, so the cache must end up with 2 GB.
        self.assertEqual(self.v3_update.call_args.args[3]['up'], 1 * GB)
        row = self._cached_row()
        self.assertEqual(row['up'], 2 * GB)
        self.assertEqual(row['raw_client']['totalGB'], expected_cap)
        self.assertEqual(self._client_state(payload)['used_up'], 2 * GB)

        # The verified state left a fence for the background reads.
        fence = self._fence()
        self.assertIsNotNone(fence, 'no read-your-writes fence was recorded')
        self.assertEqual(fence['used_up'], 2 * GB)
        self.assertEqual(fence['used_down'], 0)
        self.assertEqual(fence['total_bytes'], expected_cap)
        # A fence must not outlive the propagation delay it covers.
        self.assertEqual(fence['expires_at'] - fence['verified_at'],
                         refresh_policy.client_fence_seconds())

        # The in-flight read finishes: it has the write's CONFIG (applied to the
        # client record immediately) but its traffic aggregation still says 1 GB.
        self.background_answers = [self.panel.aggregate_inbounds(up=1 * GB)]
        result = self._background_read()

        committed = GLOBAL_SERVER_DATA['inbounds'][0]['clients'][0]
        self.assertEqual(committed['up'], 2 * GB)
        self.assertEqual(committed['down'], 0)
        self.assertEqual(committed['up_formatted'], app_module.format_bytes(2 * GB))
        self.assertEqual(committed['down_formatted'], app_module.format_bytes(0))
        self.assertEqual(committed['raw_client']['totalGB'], expected_cap)
        self.assertEqual(committed['remaining_bytes'], 28 * GB)
        self.assertEqual(result['block'][0]['clients'][0]['up'], 2 * GB)
        # The mutation moved this server's revision, so a cycle that recorded the
        # older one is discarded instead of published (the fan-out's own guard).
        self.assertGreater(refresh_jobs.get_server_revision(self.server.id),
                           revision_before)
        # Still fenced: the panel has not caught up yet.
        self.assertIsNotNone(self._fence(now=fence['verified_at'] + 1))

        # The panel catches up: the same read now reports the traffic the renewal
        # saw. The fence has done its job and must be gone.
        self.background_answers = [self.panel.aggregate_inbounds(up=2 * GB)]
        self._background_read()
        self.assertEqual(GLOBAL_SERVER_DATA['inbounds'][0]['clients'][0]['up'], 2 * GB)
        self.assertIsNone(self._fence(now=fence['verified_at'] + 1))

    # -- E. the client-level read supplies what the aggregate lacks -----------

    def test_e_the_client_level_read_supplies_the_state_the_aggregate_lacks(self):
        """The v3 client read has the new state; the aggregate list is still old.

        Why: the two endpoints do not propagate a write at the same speed, so an
        aggregate-only read-back reports the pre-mutation cap and the response
        describes an account the operator does not have. The route reads the
        client-level endpoint for the authoritative fields, keeps the LARGER of the
        two counter readings (a lagging aggregate may then only ever add usage, never
        hide it), and the fence keeps that verified state against the lagging list.
        """
        cap = 5 * GB
        _row, _revision = self._seed_cache(cap=cap, up=0, down=0, age_seconds=0)
        self.panel.cap = cap
        self.panel.up = 0
        self.panel.down = 0
        # The aggregate list has not picked the write up; the client read has.
        self.panel.lag_config = True
        self.panel.direct = _raw_client(EMAIL, cap=25 * GB, up=4 * GB, down=1 * GB)

        payload = self._renew(volume=20, reset_traffic=False)

        expected_cap = 25 * GB
        self.assertTrue(payload['verify']['ok'], payload['verify'])
        observed = payload['verify']['observed']
        self.assertEqual(observed['totalGB'], expected_cap)
        self.assertEqual(observed['expiryTime'], 0)
        self.assertTrue(observed['enable'])
        # max(direct, aggregate): the direct read is 4/1, the lagging list 0/0.
        self.assertEqual(observed['up'], 4 * GB)
        self.assertEqual(observed['down'], 1 * GB)

        row = self._cached_row()
        self.assertEqual(row['raw_client']['totalGB'], expected_cap)
        self.assertEqual(row['up'], 4 * GB)
        self.assertEqual(row['down'], 1 * GB)
        self.assertEqual(row['remaining_bytes'], 20 * GB)
        state = self._client_state(payload)
        self.assertEqual(state['total_bytes'], expected_cap)
        self.assertEqual(state['used_up'], 4 * GB)
        self.assertEqual(state['used_down'], 1 * GB)
        self.assertEqual(state['remaining_bytes'], 20 * GB)

        fence = self._fence()
        self.assertIsNotNone(fence, 'no read-your-writes fence was recorded')
        self.assertEqual(fence['used_up'], 4 * GB)
        self.assertEqual(fence['used_down'], 1 * GB)
        self.assertEqual(fence['total_bytes'], expected_cap)

        # A background read whose aggregate has the write's config but not yet its
        # traffic must not take the counters back to 0/0.
        self.background_answers = [
            self.panel.aggregate_inbounds(cap=expected_cap, up=0, down=0)]
        self._background_read()
        committed = GLOBAL_SERVER_DATA['inbounds'][0]['clients'][0]
        self.assertEqual(committed['up'], 4 * GB)
        self.assertEqual(committed['down'], 1 * GB)
        self.assertEqual(committed['raw_client']['totalGB'], expected_cap)
        self.assertEqual(committed['remaining_bytes'], 20 * GB)
        self.assertIsNotNone(self._fence(now=fence['verified_at'] + 1))

        # Once the aggregate reports the verified counters the fence is released, so
        # it can never pin a value the panel really changed.
        self.background_answers = [
            self.panel.aggregate_inbounds(cap=expected_cap, up=4 * GB, down=1 * GB)]
        self._background_read()
        self.assertEqual(GLOBAL_SERVER_DATA['inbounds'][0]['clients'][0]['up'], 4 * GB)
        self.assertIsNone(self._fence(now=fence['verified_at'] + 1))

    # -- F. membership divergence and node pending are not success ------------

    def _renew_response(self, **payload):
        """Drive the route and return (business_status, body).

        The app rewrites API 4xx business errors to HTTP 200 and keeps the real code
        in ``X-Eve-Status`` (app.add_security_headers' downgrade, asserted in
        tests/test_renew_enable.py), so that header IS the status contract here.
        """
        body = {'mode': 'custom', 'days': 0, 'free': True}
        body.update(payload)
        response = self.http.post(
            '/api/client/%d/%d/%s/renew' % (self.server.id, INBOUND_ID, EMAIL),
            json=body)
        return (response.headers.get('X-Eve-Status') or response.status_code,
                response.get_json())

    def test_f_a_disabled_membership_is_never_a_successful_renewal(self):
        """Global client enabled, the SECOND inbound disabled: not applied-and-active.

        This is the mandatory regression for the reported bug. The old verification
        read the requested inbound, then let the global client record overwrite it, so
        this exact shape reported `ok=true`, charged the customer and sent the renewal
        SMS while the account was still inactive in the inbound it actually uses.
        """
        _row, _revision = self._seed_cache(cap=5 * GB, up=0, down=0, age_seconds=0)
        self.panel.cap = 5 * GB
        self.membership_ids = [INBOUND_ID, 24]
        self.membership_enable = {24: False}

        status, payload = self._renew_response(volume=20, reset_traffic=False)

        self.assertEqual(str(status), '409', payload)
        self.assertFalse(payload.get('success'))
        self.assertEqual(payload.get('code'), 'renew_not_verified')
        verify = payload['verify']
        self.assertTrue(verify['config_applied'], verify)
        self.assertFalse(verify['activation_config_converged'], verify)
        self.assertEqual(verify['final_state'], 'CONFIG_APPLIED_ACTIVATION_PENDING')
        self.assertEqual(verify['memberships']['disabled_ids'], [24])
        self.assertEqual(verify['memberships']['missing_ids'], [])
        self.assertEqual(verify['global']['enable'], True)
        # The membership layer is reported separately and is NOT overwritten by the
        # global one, which is the whole point of the fix.
        self.assertFalse(verify['observed']['enable'])
        # Nothing may be charged or notified for an unverified renewal.
        self.assertEqual(Transaction.query.count(), 0)

    def test_g_node_pending_is_pending_not_success(self):
        """nodePending: the panel committed the config, its node has not caught up."""
        _row, _revision = self._seed_cache(cap=5 * GB, up=0, down=0, age_seconds=0)
        self.panel.cap = 5 * GB
        self.node_pending = True

        status, payload = self._renew_response(volume=20, reset_traffic=False)

        self.assertEqual(str(status), '409', payload)
        verify = payload['verify']
        self.assertTrue(verify['config_applied'], verify)
        self.assertEqual(verify['node_pending'], True)
        self.assertEqual(verify['runtime_sync_state'], 'pending')
        self.assertEqual(verify['final_state'], 'CONFIG_APPLIED_ACTIVATION_PENDING')
        self.assertFalse(verify.get('ok'))
        self.assertEqual(Transaction.query.count(), 0)

    def test_h_a_fully_converged_renewal_still_succeeds(self):
        """The control: every layer agreeing must still renew, charge and return 200."""
        _row, _revision = self._seed_cache(cap=5 * GB, up=0, down=0, age_seconds=0)
        self.panel.cap = 5 * GB
        self.membership_ids = [INBOUND_ID, 24]

        payload = self._renew(volume=20, reset_traffic=False)

        verify = payload['verify']
        self.assertEqual(verify['final_state'], 'APPLIED_ACTIVE')
        self.assertTrue(verify['config_applied'])
        self.assertTrue(verify['activation_config_converged'])
        self.assertEqual(payload['client_state']['total_bytes'], 25 * GB)


if __name__ == '__main__':
    unittest.main()
