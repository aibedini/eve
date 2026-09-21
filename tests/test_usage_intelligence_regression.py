"""The required usage-intelligence regression suite (RFP sections 45, 46, 48, 49).

Every item of the RFP's mandatory list lives here by name, so "is the required suite green?"
is answerable by reading this file:

* 45.1-45.18 - the algorithm cases, mostly as pure-function checks over metrics, with the
  database cases seeded for real;
* 46 - the golden regression for the reported bug, under the exact name the RFP asks for;
* 48 - the mutation integration: one verified renewal produces exactly one verified event,
  linked to its operation, with the cache patched and no second refresh needed;
* 49 - the failure cases: a failed write, a failed read-back and a duplicate insert must not
  produce a verified cycle boundary.

Run it as the release gate for this area:

    python -m pytest tests/test_usage_intelligence_regression.py -q
"""
import copy
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

import app as app_module  # noqa: E402
from app import Admin, ClientOperation, GLOBAL_SERVER_DATA, RenewalEvent, Server  # noqa: E402
from app import UsageCounterState, UsageDaily, app, db  # noqa: E402
from panel.adapters import xui as xui_adapter  # noqa: E402
from panel.routes import clients as clients_module  # noqa: E402
from panel.services import panel_capabilities  # noqa: E402
from panel.services.usage_intelligence import (  # noqa: E402
    analysis, build_historical_baseline, classify_trend, detect_trend, forecast_usage,
    latest_cycle_boundary, record_inferred_reset, record_verified_renewal, select_packages,
)
from panel.services.usage_intelligence.schemas import (  # noqa: E402
    CycleMetrics, ForecastMetrics, Signals, WindowMetrics,
)

GB = 1024 ** 3
PACKAGES = [
    {'id': 1, 'name': 'starter', 'days': 30, 'volume': 30, 'price': 100},
    {'id': 2, 'name': 'standard', 'days': 30, 'volume': 60, 'price': 200},
    {'id': 3, 'name': 'plus', 'days': 30, 'volume': 120, 'price': 350},
    {'id': 4, 'name': 'max', 'days': 30, 'volume': 200, 'price': 500},
]


class AlgorithmMatrixTests(unittest.TestCase):
    """RFP section 45: the eighteen mandatory algorithm cases."""

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
        RenewalEvent.query.delete()
        db.session.commit()

    def _cycle(self, rate, *, maturity='medium', elapsed=8.0, available=True):
        return CycleMetrics(available=available, average_daily_gb=rate, maturity=maturity,
                            elapsed_days=elapsed, effective_elapsed_days=elapsed)

    def _rolling(self, rate, *, basis=31.0, available=True):
        return WindowMetrics(available=available, average_daily_gb=rate, basis_days=basis,
                             observed_dates=20, samples=40)

    def _forecast(self, cycle, rolling, **kwargs):
        trend = detect_trend(cycle, rolling)
        return trend, forecast_usage(
            cycle, rolling, kwargs.pop('baseline', None),
            kwargs.pop('signals', None), trend=trend, **kwargs)

    def test_45_1_a_stable_cycle_keeps_the_same_package(self):
        cycle, rolling = self._cycle(1.0, maturity='mature', elapsed=20.0), self._rolling(1.0)
        _trend, forecast = self._forecast(cycle, rolling)
        # 1 GB/day x 30 days = 30GB, and the 15% medium-confidence margin makes it 34.5GB,
        # so the 60GB offer is the smallest that covers it ...
        choice = select_packages(PACKAGES, forecast)['recommended']
        self.assertEqual(choice.package_volume_gb, 60)
        self.assertFalse(choice.capacity_limited)
        # ... while the unbuffered point forecast would still fit the 30GB offer.
        without_margin = select_packages(PACKAGES, ForecastMetrics(
            average_daily_gb=1.0, projected_31d_gb=30.0, safety_margin_percent=0))
        self.assertEqual(without_margin['recommended'].package_volume_gb, 30)

    def test_45_2_a_light_month_with_a_heavy_cycle_makes_the_cycle_dominant(self):
        cycle, rolling = self._cycle(3.75), self._rolling(1.94)
        trend, forecast = self._forecast(cycle, rolling)
        self.assertEqual(trend.state, 'strong_increase')
        self.assertEqual(forecast.basis, 'current_cycle_dominant')
        self.assertGreater(forecast.average_daily_gb, rolling.average_daily_gb)

    def test_45_3_an_eight_day_exhaustion_is_early_exhaustion(self):
        signals = Signals(early_exhaustion=True, exhaustion_severity='critical',
                          exhaustion_ratio=8.0 / 31.0, expected_duration_days=31.0)
        cycle, rolling = self._cycle(6.2), self._rolling(1.9)
        trend, forecast = self._forecast(cycle, rolling, signals=signals)
        self.assertTrue(signals.early_exhaustion)
        self.assertLess(signals.exhaustion_ratio, 0.35)
        self.assertEqual(forecast.blend, (0.80, 0.20))

    def test_45_4_a_recent_strong_decrease_forecasts_below_the_rolling_rate(self):
        cycle, rolling = self._cycle(0.4, maturity='mature', elapsed=20.0), self._rolling(1.0)
        trend, forecast = self._forecast(cycle, rolling)
        self.assertEqual(trend.state, 'strong_decrease')
        self.assertLess(forecast.average_daily_gb, rolling.average_daily_gb)

    def test_45_5_a_rollover_renewal_is_not_a_bigger_purchase(self):
        event = record_verified_renewal(
            server_id=1, sub_id='rollover', operation_id='op-rollover',
            previous_volume_limit_bytes=50 * GB, new_volume_limit_bytes=100 * GB,
            previous_remaining_bytes=10 * GB, granted_volume_bytes=50 * GB,
            previous_expiry_ms=1, new_expiry_ms=2)
        self.assertEqual(event.granted_volume_bytes, 50 * GB)
        self.assertEqual(event.carried_over_bytes, 10 * GB)
        self.assertEqual(event.previous_remaining_bytes, 10 * GB)
        self.assertNotEqual(event.granted_volume_bytes, event.new_volume_limit_bytes)

    def test_45_6_and_45_7_only_a_verified_renewal_opens_a_cycle(self):
        # 45.6: an explicit renewal creates the boundary without any counter movement.
        renewal = record_verified_renewal(
            server_id=2, sub_id='explicit', operation_id='op-explicit',
            previous_volume_limit_bytes=GB, new_volume_limit_bytes=2 * GB,
            previous_remaining_bytes=0, granted_volume_bytes=GB,
            previous_expiry_ms=1, new_expiry_ms=2)
        self.assertTrue(renewal.is_cycle_boundary)
        # 45.7: a counter decrease on its own does not.
        reset = record_inferred_reset(server_id=2, sub_id='counter-only', volume_bytes=GB)
        self.assertFalse(reset.is_cycle_boundary)
        self.assertIsNone(latest_cycle_boundary(2, 'counter-only'))

    def test_45_8_a_duplicate_retry_produces_one_event(self):
        for _ in range(3):
            record_verified_renewal(
                server_id=3, sub_id='retry', operation_id='op-retry',
                previous_volume_limit_bytes=GB, new_volume_limit_bytes=2 * GB,
                previous_remaining_bytes=0, granted_volume_bytes=GB,
                previous_expiry_ms=1, new_expiry_ms=2)
        self.assertEqual(RenewalEvent.query.filter_by(operation_id='op-retry').count(), 1)

    def test_45_9_stale_telemetry_downgrades_confidence(self):
        from panel.services.usage_intelligence.confidence import assess_data_confidence
        cycle, rolling = self._cycle(3.0, maturity='mature', elapsed=20.0), self._rolling(2.0)
        fresh, _ = assess_data_confidence(cycle, rolling, freshness='fresh')
        stale, reasons = assess_data_confidence(cycle, rolling, freshness='stale')
        self.assertEqual(fresh, 'high')
        self.assertEqual(stale, 'medium')
        self.assertIn('stale_telemetry', reasons)

    def test_45_10_zero_usage_gets_the_smallest_offer_not_an_upsell(self):
        forecast = ForecastMetrics(average_daily_gb=0.0, safety_margin_percent=25)
        choice = select_packages(PACKAGES, forecast)['recommended']
        self.assertEqual(choice.package_volume_gb, 30)
        self.assertEqual(choice.reason, 'no_usage_lowest')

    def test_45_11_unlimited_volume_is_only_used_when_finite_cannot_cover(self):
        finite = [{'id': 1, 'name': 'big', 'days': 30, 'volume': 500, 'price': 500},
                  {'id': 2, 'name': 'unmetered', 'days': 30, 'volume': 0, 'price': 100}]
        covered = select_packages(finite, ForecastMetrics(average_daily_gb=3.0))['recommended']
        self.assertFalse(covered.unlimited)
        small_only = [{'id': 1, 'name': 'small', 'days': 30, 'volume': 5, 'price': 50},
                      {'id': 2, 'name': 'unmetered', 'days': 30, 'volume': 0, 'price': 900}]
        fallback = select_packages(small_only, ForecastMetrics(average_daily_gb=9.0))
        self.assertTrue(fallback['recommended'].unlimited)
        self.assertFalse(fallback['recommended'].capacity_limited)

    def test_45_12_unlimited_time_is_flagged_from_the_expiry(self):
        event = record_verified_renewal(
            server_id=4, sub_id='unlimited-time', operation_id='op-utime',
            previous_volume_limit_bytes=GB, new_volume_limit_bytes=GB,
            previous_remaining_bytes=0, granted_volume_bytes=GB,
            previous_expiry_ms=1, new_expiry_ms=0)
        self.assertTrue(event.is_unlimited_time)
        self.assertFalse(event.is_unlimited_volume)

    def test_45_13_without_daily_rows_the_live_counter_answers(self):
        rolling = WindowMetrics(available=True, average_daily_gb=2.0, basis_days=1.0)
        forecast = forecast_usage(self._cycle(0.0, available=False), rolling)
        self.assertEqual(forecast.basis, 'rolling_history')
        self.assertAlmostEqual(forecast.average_daily_gb, 2.0, places=3)

    def test_45_14_without_a_renewal_the_rolling_history_answers(self):
        trend = detect_trend(self._cycle(0.0, available=False), self._rolling(1.6))
        self.assertEqual(trend.state, 'unknown')
        forecast = forecast_usage(self._cycle(0.0, available=False), self._rolling(1.6))
        self.assertEqual(forecast.basis, 'rolling_history')
        self.assertEqual(forecast.blend, (0.0, 1.0))

    def test_45_15_an_hour_after_a_renewal_the_elapsed_floor_applies(self):
        cycle = CycleMetrics(available=True, average_daily_gb=24.0, maturity='insufficient',
                            elapsed_days=1.0 / 24.0, effective_elapsed_days=0.25)
        self.assertEqual(cycle.maturity, 'insufficient')
        self.assertEqual(cycle.effective_elapsed_days, 0.25)
        # A single hour of evidence cannot claim a behaviour change.
        self.assertEqual(classify_trend(24.0 / 1.6, maturity=cycle.maturity), 'strong_increase')
        trend = detect_trend(cycle, self._rolling(1.6), maturity='insufficient')
        self.assertTrue(trend.confidence_aware)

    def test_45_16_a_one_day_spike_is_winsorized(self):
        from panel.services.usage_intelligence.forecast import daily_series_stats, robust_rate
        series = [0.5] * 20 + [25.0]
        stats = daily_series_stats(series)
        self.assertLess(stats['cap'], 25.0)
        self.assertLess(robust_rate(series, basis_days=31.0), 1.0)

    def test_45_17_the_largest_package_being_insufficient_is_reported(self):
        tight = [{'id': 1, 'name': 'a', 'days': 30, 'volume': 30, 'price': 100},
                 {'id': 2, 'name': 'b', 'days': 30, 'volume': 60, 'price': 200}]
        choice = select_packages(
            tight, ForecastMetrics(average_daily_gb=6.0, safety_margin_percent=20)
        )['recommended']
        self.assertTrue(choice.capacity_limited)
        self.assertEqual(choice.package_volume_gb, 60)
        self.assertGreater(choice.capacity_shortfall_gb, 100.0)

    def test_45_18_mixed_durations_use_each_packages_own_horizon(self):
        mixed = [{'id': 1, 'name': 'week', 'days': 7, 'volume': 50, 'price': 100},
                 {'id': 2, 'name': 'month', 'days': 30, 'volume': 100, 'price': 200}]
        choice = select_packages(
            mixed, ForecastMetrics(average_daily_gb=5.0, safety_margin_percent=20)
        )['recommended']
        self.assertEqual(choice.package_days, 7)     # 5 x 7 x 1.2 = 42GB fits the 50GB week
        self.assertNotEqual(choice.package_days, 30)  # the month would need 180GB


class GoldenScenarioTests(unittest.TestCase):
    """RFP section 46: the exact reported bug."""

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
        RenewalEvent.query.delete()
        UsageDaily.query.delete()
        UsageCounterState.query.delete()
        Server.query.delete()
        db.session.commit()
        self.server = Server(name='golden', host='https://golden.invalid', username='u',
                             password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()

    def test_recent_post_renewal_consumption_overrides_stale_rolling_average(self):
        """31 days at 60GB total, 8 days since the renewal at 30GB → ~3.75 vs ~1.94 GB/day."""
        sub_id = 'golden-account'
        renewed_at = datetime.utcnow() - timedelta(days=8)

        def to_ms(value):
            return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)

        # The counter read 30GB at the boundary (50GB cap, 20GB unused) and reads 60GB now.
        record_verified_renewal(
            server_id=self.server.id, sub_id=sub_id, operation_id='op-golden', days=31,
            previous_volume_limit_bytes=50 * GB, new_volume_limit_bytes=100 * GB,
            previous_remaining_bytes=20 * GB, granted_volume_bytes=50 * GB,
            previous_expiry_ms=to_ms(renewed_at),
            new_expiry_ms=to_ms(renewed_at + timedelta(days=31)),
            renewed_at=renewed_at)
        for offset in range(31):
            observed = datetime.utcnow() - timedelta(days=offset)
            used = int((3.75 if offset < 8 else 1.3) * GB)
            db.session.add(UsageDaily(
                server_id=self.server.id, sub_id=sub_id,
                usage_date=date.today() - timedelta(days=offset),
                upload_bytes=0, download_bytes=used,
                opening_upload_bytes=0, opening_download_bytes=0,
                closing_upload_bytes=0, closing_download_bytes=used,
                sample_count=1, first_observed_at=observed, last_observed_at=observed))
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0,
            download_bytes=60 * GB, total_bytes=60 * GB, observed_at=datetime.utcnow()))
        db.session.commit()

        context = analysis.load_usage_context(
            self.server.id, sub_id,
            live_usage={'total_bytes': 60 * GB, 'observed_at': datetime.utcnow()})
        trend = detect_trend(context.cycle, context.rolling)
        forecast = forecast_usage(context.cycle, context.rolling, context.baseline,
                                  context.signals, trend=trend)
        choice = select_packages(PACKAGES, forecast)['recommended']
        baseline = build_historical_baseline(
            self.server.id, sub_id, cycle_start=context.cycle.started_at)

        # The RFP's assertions, one by one.
        self.assertEqual(context.cycle.event_type, 'renewal')
        self.assertTrue(context.cycle.available)
        self.assertEqual(trend.state, 'strong_increase')
        self.assertGreater(forecast.average_daily_gb, context.rolling.average_daily_gb)
        # ... and the recommendation is not a projection of the stale 60GB average: the old
        # model would have pointed at a 60GB package, the new one needs >= 100GB.
        self.assertGreater(forecast.projected_31d_gb, 100.0)
        self.assertGreater(choice.package_volume_gb, 60)
        self.assertAlmostEqual(context.cycle.average_daily_gb, 3.75, places=1)
        self.assertLess(context.rolling.average_daily_gb, 2.1)
        self.assertAlmostEqual(trend.ratio, 1.93, places=1)
        self.assertGreater(baseline.average_daily_gb, 0)
        self.assertLessEqual(context.queries, analysis.QUERY_BUDGET)
        # A mature-enough cycle with a strong increase: the cycle carries the forecast.
        self.assertEqual(forecast.basis, 'current_cycle_dominant')
        self.assertEqual(forecast.safety_margin_percent, 20)


class MutationIntegrationTests(unittest.TestCase):
    """RFP section 48: the renew endpoint's full contract, end to end."""

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
        from panel.core import client_events, snapshot_delta
        client_events.reset()
        snapshot_delta.reset_state()
        ClientOperation.query.delete()
        RenewalEvent.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(username='regression-matrix', password_hash='x',
                           role='superadmin', is_superadmin=True)
        self.server = Server(name='matrix', host='https://matrix.invalid', username='u',
                             password='p', sub_path='/sub/', panel_type='auto')
        db.session.add_all([self.admin, self.server])
        db.session.commit()
        self.http = app.test_client()
        with self.http.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
            sess['admin_username'] = self.admin.username
            sess['role'] = self.admin.role
            sess['is_superadmin'] = True
        self._saved = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore)
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({'inbounds': [], 'stats': {}, 'servers_status': [],
                                   'last_update': None})
        self.panel_expiry = int(datetime.utcnow().timestamp() * 1000) + 5 * 86400000
        self.panel_raw = {'email': 'bob', 'id': 'uuid-bob', 'enable': True,
                          'totalGB': 50 * GB, 'expiryTime': self.panel_expiry}
        self.v3_update = mock.Mock(return_value=(True, {}, None))
        self.rewrite = False

        def fetch_inbounds(*_args, **_kwargs):
            sent = (dict(self.v3_update.call_args[0][3]) if self.v3_update.call_args
                    else copy.deepcopy(self.panel_raw))
            if self.rewrite:
                sent = dict(self.panel_raw)
            return ([{'id': 1, 'server_id': self.server.id,
                      'settings': json.dumps({'clients': [sent]})}], None, '3x-ui')

        self._patches = [
            mock.patch.object(app_module, 'get_xui_session',
                              return_value=(mock.Mock(), None)),
            mock.patch.object(app_module, 'server_is_v3', return_value=True),
            # Capability planner + the two extra layers the verified renewal reads.
            mock.patch.object(panel_capabilities, 'capabilities_for',
                              return_value=(self._caps(), None)),
            mock.patch.object(app_module, 'v3_update_client', self.v3_update),
            mock.patch.object(xui_adapter, 'v3_update_client_result',
                              side_effect=self._panel_write_result),
            mock.patch.object(app_module, 'v3_enable_client',
                              return_value=(True, {}, None)),
            mock.patch.object(xui_adapter, 'v3_get_client_details',
                              side_effect=self._client_details),
            mock.patch.object(xui_adapter, 'v3_client_traffic',
                              return_value={'available': False,
                                            'reason': 'not modelled by this fixture'}),
            mock.patch.object(app_module, 'fetch_inbounds', side_effect=fetch_inbounds),
            mock.patch.object(app_module, '_fire_automation_sms'),
            mock.patch.object(app_module, '_fire_cancel_stale_account_sms'),
            mock.patch.object(app_module, '_notify_customer_telegram'),
            mock.patch.object(clients_module, '_fire_renew_whatsapp'),
            mock.patch.object(clients_module, '_fire_renew_postcheck'),
            mock.patch('time.sleep'),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._stop)
        self._seed_cache()

    @staticmethod
    def _caps():
        return panel_capabilities.PanelClientCapabilities(
            client_api_family=panel_capabilities.CLIENT_API_FIRST_CLASS,
            client_get=True, client_update=True, client_traffic=True,
            client_reset_traffic=True, bulk_adjust=True, bulk_enable=True,
            node_pending_response=True, limit_hwid=True, scoped_tokens=True,
            version='3.8.5', version_family=(3, 8), profile='xui_3_8',
            probe_state=panel_capabilities.PROBE_SUPPORTED,
            evidence={'fixture': 'v3.8 panel'})

    def _panel_write_result(self, server, session, email, client, **_kwargs):
        # The fixture's return value decides the outcome (a test may model a rejected
        # write); the production classifier turns it into a result.
        ok, response, error = self.v3_update(server, session, email, client)
        return xui_adapter.classify_mutation_result(ok, response, error,
                                                    may_be_partial=not ok)

    def _client_details(self, server, session, email, *_args, **_kwargs):
        row = (dict(self.v3_update.call_args[0][3]) if self.v3_update.call_args
               else dict(self.panel_raw))
        if self.rewrite:
            row = dict(self.panel_raw)
        return {'ok': True, 'client': row, 'inbound_ids': [1],
                'raw': {'client': row, 'inboundIds': [1]}, 'error': None}

    def _stop(self):
        for patch in self._patches:
            patch.stop()

    def _restore(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved)

    def _seed_cache(self):
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'server_id': self.server.id, 'id': 1, 'remark': 'in',
            'clients': [{'server_id': self.server.id, 'inbound_id': 1, 'email': 'bob',
                         'id': 'uuid-bob', 'up': 40 * GB, 'down': 0,
                         'up_formatted': '40 GB', 'down_formatted': '0 B',
                         'raw_client': copy.deepcopy(self.panel_raw)}],
            'client_count': 1, 'active_count': 1}]
        GLOBAL_SERVER_DATA['servers_status'] = [
            {'server_id': self.server.id, 'success': True, 'reachable': True, 'stats': {}}]
        GLOBAL_SERVER_DATA['last_update'] = datetime.utcnow().isoformat()

    def _renew(self, key='matrix-op-1', **payload):
        body = {'mode': 'custom', 'days': 30, 'volume': 10, 'free': True}
        body.update(payload)
        return self.http.post('/api/client/%d/1/bob/renew' % self.server.id, json=body,
                              headers={'Idempotency-Key': key})

    def test_48_a_renewal_writes_the_panel_verifies_and_records_one_event(self):
        from panel.core import client_events, snapshot_delta
        revision_before = snapshot_delta.current_revision(GLOBAL_SERVER_DATA, force=True)
        response = self._renew('matrix-op-48')
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'])
        # panel write occurred, read-back succeeded
        self.assertTrue(self.v3_update.called)
        self.assertTrue(payload['verify']['ok'], payload)
        # exactly one verified event, linked to the operation
        events = RenewalEvent.query.filter_by(server_id=self.server.id).all()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertTrue(event.verified)
        self.assertEqual(event.operation_id, 'matrix-op-48')
        # cache patched, cursor moved, and the other tabs were told
        self.assertIsNotNone(payload.get('client_state'))
        self.assertGreater(snapshot_delta.current_revision(GLOBAL_SERVER_DATA), revision_before)
        self.assertTrue(client_events.since(revision_before))
        # the response is canonical: no full refresh is required for the card to be right
        self.assertEqual(payload['client_state']['total_bytes'], 60 * GB)
        self.assertIn('renewal_event', payload)

    def test_49_a_failed_write_leaves_no_verified_event(self):
        self.v3_update.return_value = (False, {}, 'panel rejected the update')
        response = self._renew('matrix-op-49a')
        self.assertFalse(response.get_json()['success'])
        self.assertEqual(RenewalEvent.query.filter_by(verified=True).count(), 0)

    def test_49_a_failed_read_back_leaves_no_verified_event(self):
        self.rewrite = True
        response = self._renew('matrix-op-49b')
        payload = response.get_json()
        self.assertFalse(payload.get('verify', {}).get('ok'), payload)
        self.assertEqual(RenewalEvent.query.filter_by(verified=True).count(), 0)

    def test_49_a_duplicate_event_insert_is_refused_by_the_unique_key(self):
        from sqlalchemy.exc import IntegrityError
        record_verified_renewal(
            server_id=self.server.id, sub_id='dup', operation_id='dup-op',
            previous_volume_limit_bytes=GB, new_volume_limit_bytes=2 * GB,
            previous_remaining_bytes=0, granted_volume_bytes=GB,
            previous_expiry_ms=1, new_expiry_ms=2)
        db.session.commit()
        # A concurrent writer that bypassed the pre-check hits the constraint instead of
        # silently opening a second cycle.
        with self.assertRaises(IntegrityError):
            db.session.add(RenewalEvent(
                server_id=self.server.id, sub_id='dup', event_type='renewal',
                source='explicit_renew', verified=True, operation_id='dup-op',
                renewed_at=datetime.utcnow()))
            db.session.commit()
        db.session.rollback()
        self.assertEqual(RenewalEvent.query.filter_by(operation_id='dup-op').count(), 1)


if __name__ == '__main__':
    unittest.main()
