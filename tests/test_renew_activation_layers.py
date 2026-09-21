"""Renewal verification in layers, and activation repair that cannot re-bill.

The reported production bug: renew returns success, expiry and quota are updated,
and the client is sometimes still inactive. The old verification read ONE inbound
row, then let the global client record overwrite it, and then asked a single
question - "is enable false?" - so this passed:

    global        enable=true
    inbound 17    enable=true
    inbound 24    enable=false      <- the customer's actual inbound
    inbound 38    enable=true
    verdict                         ok=true

Every test here pins one layer of the answer: the divergence above, a node that
reports it has not synchronised, a traffic row that still says disabled, a
membership that is missing entirely, and an account that is still depleted so that
enabling it would be undone by the panel on its next cycle.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from panel.services import panel_capabilities as caps_mod
from panel.services import renew_activation as ra


def _client(email='user@example.com', *, enable=True, expiry=2_000_000_000_000,
            total=20 * 1024 ** 3, uuid='uuid-1'):
    return {'email': email, 'id': uuid, 'enable': enable, 'expiryTime': expiry,
            'totalGB': total}


def _inbound(inbound_id, email='user@example.com', *, enable=True, present=True):
    import json
    clients = [_client(email, enable=enable)] if present else []
    return {'id': inbound_id, 'settings': json.dumps({'clients': clients})}


EXPECTED = {'expiryTime': 2_000_000_000_000, 'totalGB': 20 * 1024 ** 3,
            'inbound_id': 17}


class LayerClassificationTests(unittest.TestCase):
    def test_the_reported_false_positive_is_now_a_pending_activation(self):
        # THE case from the task: global applied and active, one membership disabled.
        layers = ra.analyze_activation(
            expected=EXPECTED,
            global_client=_client(enable=True),
            inbound_ids=[17, 24, 38],
            memberships={17: _client(enable=True), 24: _client(enable=False),
                         38: _client(enable=True)},
            traffic={'available': True, 'enable': True},
        )
        self.assertTrue(layers.config_applied)
        self.assertFalse(layers.activation_converged)
        self.assertEqual(layers.disabled_inbound_ids, [24])
        self.assertEqual(layers.final_state, ra.STATE_ACTIVATION_PENDING)
        # The global layer is still reported separately and honestly.
        self.assertEqual(layers.global_enable, True)

    def test_every_layer_agreeing_is_applied_active(self):
        layers = ra.analyze_activation(
            expected=EXPECTED, global_client=_client(),
            inbound_ids=[17, 24],
            memberships={17: _client(), 24: _client()},
            traffic={'available': True, 'enable': True},
        )
        self.assertTrue(layers.activation_converged)
        self.assertEqual(layers.final_state, ra.STATE_APPLIED_ACTIVE)
        self.assertEqual(layers.missing_inbound_ids, [])
        self.assertEqual(layers.disabled_inbound_ids, [])

    def test_a_missing_membership_is_not_converged(self):
        layers = ra.analyze_activation(
            expected=EXPECTED, global_client=_client(), inbound_ids=[17, 24],
            memberships={17: _client(), 24: None},
            traffic={'available': True, 'enable': True},
        )
        self.assertEqual(layers.missing_inbound_ids, [24])
        self.assertFalse(layers.activation_converged)
        self.assertEqual(layers.final_state, ra.STATE_ACTIVATION_PENDING)

    def test_the_requested_inbound_is_always_a_membership(self):
        # Even when the panel's own inboundIds omits it: that divergence is the bug.
        layers = ra.analyze_activation(
            expected=EXPECTED, global_client=_client(), inbound_ids=[38],
            memberships={38: _client()}, traffic={'available': True, 'enable': True},
        )
        self.assertIn(17, layers.expected_inbound_ids)
        self.assertEqual(layers.missing_inbound_ids, [17])
        self.assertFalse(layers.activation_converged)

    def test_node_pending_is_pending_even_when_every_flag_reads_true(self):
        layers = ra.analyze_activation(
            expected=EXPECTED, global_client=_client(), inbound_ids=[17],
            memberships={17: _client()}, traffic={'available': True, 'enable': True},
            node_pending=True,
        )
        self.assertTrue(layers.config_applied)
        self.assertTrue(layers.activation_converged)
        self.assertEqual(layers.runtime_sync_state, ra.RUNTIME_PENDING)
        self.assertEqual(layers.final_state, ra.STATE_ACTIVATION_PENDING)

    def test_a_traffic_row_that_still_says_disabled_is_not_converged(self):
        layers = ra.analyze_activation(
            expected=EXPECTED, global_client=_client(), inbound_ids=[17],
            memberships={17: _client()},
            traffic={'available': True, 'enable': False},
        )
        self.assertFalse(layers.activation_converged)
        self.assertTrue(any('traffic' in note for note in layers.notes))

    def test_an_unavailable_traffic_read_is_not_a_verdict(self):
        # v3.1-v3.2 panels may not expose it: "not measured" must not become
        # "disabled", and it must not become proof of activation either.
        layers = ra.analyze_activation(
            expected=EXPECTED, global_client=_client(), inbound_ids=[17],
            memberships={17: _client()}, traffic={'available': False,
                                                  'reason': 'no route'},
        )
        self.assertTrue(layers.activation_converged)
        self.assertFalse(layers.traffic_available)
        self.assertEqual(layers.traffic_enable, None)
        self.assertEqual(layers.runtime_sync_state, ra.RUNTIME_NOT_EXPOSED)

    def test_config_not_applied_beats_activation(self):
        layers = ra.analyze_activation(
            expected=EXPECTED, global_client=_client(expiry=1), inbound_ids=[17],
            memberships={17: _client()}, traffic={'available': True, 'enable': True},
        )
        self.assertFalse(layers.config_applied)
        self.assertEqual(layers.final_state, ra.STATE_NOT_APPLIED)

    def test_a_partial_write_that_lost_the_config_is_reported_as_partial(self):
        layers = ra.analyze_activation(
            expected=EXPECTED, global_client=_client(expiry=1), inbound_ids=[17],
            memberships={17: _client()}, write_may_be_partial=True,
        )
        self.assertEqual(layers.final_state, ra.STATE_PARTIALLY_APPLIED)

    def test_auth_degraded_is_its_own_state(self):
        layers = ra.analyze_activation(expected=EXPECTED, global_client=_client(),
                                       inbound_ids=[17], memberships={17: _client()},
                                       auth_degraded=True)
        self.assertEqual(layers.final_state, ra.STATE_AUTH_DEGRADED)
        self.assertTrue(any('credential' in note for note in layers.notes))

    def test_a_client_missing_from_the_panel_is_not_applied(self):
        layers = ra.analyze_activation(expected=EXPECTED, global_client=None,
                                       inbound_ids=[17], memberships={})
        self.assertEqual(layers.final_state, ra.STATE_NOT_APPLIED)
        self.assertFalse(layers.config_applied)


class DepletionGuardTests(unittest.TestCase):
    """Enabling a still-depleted client is a lost race, not a repair."""

    def _layers(self, **kwargs):
        return ra.analyze_activation(expected=EXPECTED, **kwargs)

    def test_zero_remaining_volume_is_still_depleted(self):
        layers = self._layers(global_client=_client(total=10 ** 9),
                              inbound_ids=[17], memberships={17: _client()},
                              traffic={'available': True, 'enable': False,
                                       'up': 10 ** 9, 'down': 0})
        depleted, reason = ra.account_is_still_depleted(
            layers, {'totalGB': 10 ** 9, 'expiryTime': 2_000_000_000_000,
                     'now_ms': 1_900_000_000_000})
        self.assertTrue(depleted)
        self.assertIn('volume', reason)

    def test_an_expiry_still_in_the_past_is_still_depleted(self):
        layers = self._layers(global_client=_client(expiry=1_000),
                              inbound_ids=[17], memberships={17: _client()})
        depleted, reason = ra.account_is_still_depleted(
            layers, {'totalGB': 0, 'expiryTime': 1_000, 'now_ms': 2_000})
        self.assertTrue(depleted)
        self.assertIn('expiry', reason)

    def test_a_healthy_quota_is_not_depleted(self):
        layers = self._layers(global_client=_client(), inbound_ids=[17],
                              memberships={17: _client()},
                              traffic={'available': True, 'enable': True,
                                       'up': 10 ** 9, 'down': 0})
        depleted, reason = ra.account_is_still_depleted(
            layers, {'totalGB': 20 * 1024 ** 3, 'expiryTime': 2_000_000_000_000,
                     'now_ms': 1_900_000_000_000})
        self.assertFalse(depleted)
        self.assertIsNone(reason)


class ConvergenceTests(unittest.TestCase):
    """Repair is activation-only, bounded, and stops as soon as it converges."""

    def _converged(self):
        return ra.analyze_activation(expected=EXPECTED, global_client=_client(),
                                     inbound_ids=[17], memberships={17: _client()},
                                     traffic={'available': True, 'enable': True})

    def _pending(self):
        return ra.analyze_activation(expected=EXPECTED, global_client=_client(),
                                     inbound_ids=[17],
                                     memberships={17: _client(enable=False)},
                                     traffic={'available': True, 'enable': True})

    def test_nothing_is_written_when_activation_already_converged(self):
        repairs = []
        layers, history = ra.converge_activation(
            verify=self._converged, repair=lambda: repairs.append(1), attempts=3,
            sleep=lambda _n: None)
        self.assertEqual(repairs, [])
        self.assertEqual(layers.final_state, ra.STATE_APPLIED_ACTIVE)
        self.assertEqual(len(history), 1)

    def test_a_repair_is_attempted_then_reverified(self):
        state = {'n': 0}

        def verify():
            return self._pending() if state['n'] == 0 else self._converged()

        def repair():
            state['n'] += 1
            return {'transport_ok': True, 'panel_success': True}

        layers, history = ra.converge_activation(
            verify=verify, repair=repair, attempts=3, sleep=lambda _n: None)
        self.assertEqual(state['n'], 1)
        self.assertEqual(layers.final_state, ra.STATE_APPLIED_ACTIVE)
        self.assertTrue(any(step.get('repair') == 'called' for step in history))

    def test_the_attempt_count_is_bounded(self):
        calls = []
        layers, history = ra.converge_activation(
            verify=self._pending, repair=lambda: calls.append(1), attempts=2,
            sleep=lambda _n: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(layers.final_state, ra.STATE_ACTIVATION_PENDING)

    def test_a_vetoed_repair_is_recorded_and_never_written(self):
        calls = []
        layers, history = ra.converge_activation(
            verify=self._pending, repair=lambda: calls.append(1), attempts=3,
            sleep=lambda _n: None,
            should_repair=lambda _l: (False, 'nodePending: wait for the node'))
        self.assertEqual(calls, [])
        self.assertTrue(any(step.get('repair') == 'skipped' for step in history))
        self.assertIn('nodePending', history[-1]['reason'])

    def test_a_raising_repair_does_not_loop_forever(self):
        def boom():
            raise RuntimeError('panel exploded')

        layers, history = ra.converge_activation(
            verify=self._pending, repair=boom, attempts=5, sleep=lambda _n: None)
        self.assertEqual(layers.final_state, ra.STATE_ACTIVATION_PENDING)
        self.assertTrue(any(step.get('repair') == 'raised' for step in history))
        self.assertEqual(len([s for s in history if s.get('repair') == 'raised']), 1)

    def test_the_sleep_hook_is_used_so_tests_never_wait(self):
        waited = []
        ra.converge_activation(verify=self._pending, repair=lambda: None, attempts=1,
                               sleep=waited.append)
        self.assertEqual(waited, [1])

    def test_no_sleep_is_required_at_all(self):
        # A caller may pass sleep=None; the loop must still be correct.
        layers, _history = ra.converge_activation(
            verify=self._pending, repair=lambda: None, attempts=1)
        self.assertEqual(layers.final_state, ra.STATE_ACTIVATION_PENDING)


class MembershipMapTests(unittest.TestCase):
    def test_it_maps_only_the_inbounds_that_hold_the_client(self):
        inbounds = [_inbound(1, 'a@x'), _inbound(2, 'b@x'), _inbound(3, 'a@x')]
        mapping = ra.membership_map(inbounds, 'A@X')
        self.assertEqual(sorted(mapping), [1, 2, 3])
        self.assertIsNotNone(mapping[1])
        self.assertIsNone(mapping[2])
        self.assertIsNotNone(mapping[3])

    def test_it_restricts_to_the_panels_own_membership_list(self):
        inbounds = [_inbound(1, 'a@x'), _inbound(2, 'a@x')]
        mapping = ra.membership_map(inbounds, 'a@x', inbound_ids=[2])
        self.assertEqual(sorted(mapping), [2])

    def test_a_dict_settings_field_is_accepted(self):
        inbound = {'id': 5, 'settings': {'clients': [_client('a@x')]}}
        self.assertIsNotNone(ra.membership_map([inbound], 'a@x')[5])

    def test_broken_settings_do_not_raise(self):
        inbound = {'id': 6, 'settings': 'not json'}
        self.assertIsNone(ra.membership_map([inbound], 'a@x')[6])


class RouteLayerReaderTests(unittest.TestCase):
    """The route helper must read every layer and never let one speak for another."""

    def _caps(self):
        return caps_mod.PanelClientCapabilities(
            client_api_family=caps_mod.CLIENT_API_FIRST_CLASS,
            client_get=True, client_update=True, client_traffic=True,
            client_reset_traffic=True, bulk_adjust=True, bulk_enable=True,
            node_pending_response=True)

    def test_it_reads_global_memberships_and_traffic(self):
        from panel.routes import clients as clients_route

        seen = {}

        def read_global():
            seen['global'] = True
            return {'ok': True, 'client': _client(), 'inbound_ids': [17, 24]}

        def read_traffic():
            seen['traffic'] = True
            return {'available': True, 'enable': True}

        layers = clients_route._read_activation_layers(
            email='user@example.com', expected=EXPECTED, caps=self._caps(),
            inbounds=[_inbound(17), _inbound(24, enable=False)],
            read_global=read_global, read_traffic=read_traffic)

        self.assertTrue(seen.get('global') and seen.get('traffic'))
        self.assertTrue(layers.config_applied)
        self.assertEqual(layers.disabled_inbound_ids, [24])
        self.assertEqual(layers.final_state, ra.STATE_ACTIVATION_PENDING)

    def test_a_failing_global_read_does_not_raise(self):
        from panel.routes import clients as clients_route

        def read_global():
            raise RuntimeError('panel down')

        layers = clients_route._read_activation_layers(
            email='user@example.com', expected=EXPECTED, caps=self._caps(),
            inbounds=[_inbound(17)], read_global=read_global,
            read_traffic=lambda: {'available': False})
        self.assertFalse(layers.global_found)
        self.assertEqual(layers.final_state, ra.STATE_NOT_APPLIED)

    def test_traffic_is_not_read_when_the_capability_is_absent(self):
        from panel.routes import clients as clients_route

        calls = []
        legacy = caps_mod.PanelClientCapabilities(
            client_api_family=caps_mod.CLIENT_API_LEGACY)
        clients_route._read_activation_layers(
            email='user@example.com', expected=EXPECTED, caps=legacy,
            inbounds=[_inbound(17)],
            read_global=lambda: {'ok': True, 'client': _client(),
                                 'inbound_ids': [17]},
            read_traffic=lambda: calls.append(1) or {'available': True})
        self.assertEqual(calls, [])

    def test_the_trace_fields_are_structured_and_secret_free(self):
        from panel.routes import clients as clients_route

        layers = ra.analyze_activation(expected=EXPECTED, global_client=_client(),
                                       inbound_ids=[17], memberships={17: _client()},
                                       traffic={'available': True, 'enable': True})
        mutation = SimpleNamespace(panel_success=True, node_pending=False, skipped=[])
        fields = clients_route._activation_trace_fields(
            trace_id='abcd', strategy=caps_mod.RenewStrategy.V3_CURRENT,
            caps=self._caps(), mutation=mutation, layers=layers,
            operation_id=9, repair_attempts=1)
        for key in ('trace_id', 'operation_id', 'detected_version', 'compat_profile',
                    'strategy', 'capabilities', 'panel_success', 'node_pending',
                    'config_applied', 'global_enable', 'membership_count',
                    'disabled_membership_ids', 'missing_membership_ids',
                    'traffic_enable', 'runtime_sync_state', 'repair_attempt',
                    'final_state'):
            self.assertIn(key, fields)
        self.assertEqual(fields['final_state'], ra.STATE_APPLIED_ACTIVE)
        self.assertEqual(fields['strategy'], 'V3_CURRENT')
        self.assertNotIn('uuid', str(fields).lower())


class RouteWiringTests(unittest.TestCase):
    """The renew route must ask the planner, not a boolean."""

    def test_the_route_no_longer_routes_mutations_on_server_is_v3(self):
        import inspect
        from panel.routes import clients as clients_route
        source = inspect.getsource(clients_route.renew_client)
        # The capability planner owns the decision now; the old probe-boolean call
        # (with a session) must be gone from the renewal path.
        self.assertNotIn('server_is_v3(server, session_obj)', source)
        self.assertIn('panel_capabilities.capabilities_for', source)
        self.assertIn('RenewStrategy.BLOCKED', source)

    def test_the_route_keeps_the_mutation_response(self):
        import inspect
        from panel.routes import clients as clients_route
        source = inspect.getsource(clients_route.renew_client)
        self.assertIn('v3_update_client_result', source)
        self.assertNotIn('ok, _vr, verr = v3_update_client', source)

    def test_verification_does_not_collapse_memberships_into_the_global_client(self):
        import inspect
        from panel.routes import clients as clients_route
        source = inspect.getsource(clients_route.renew_client)
        self.assertNotIn('if direct_client:\n                                v_client = direct_client', source)
        self.assertIn('_read_activation_layers', source)

    def test_the_recheck_route_uses_the_same_layers(self):
        import inspect
        from panel.routes import clients as clients_route
        source = inspect.getsource(clients_route.verify_renew_client)
        self.assertIn('_read_activation_layers', source)
        self.assertNotIn('direct_client', source)
        self.assertIn('converge_activation', source)


if __name__ == '__main__':
    unittest.main()
