"""Capability routing: which panel API a mutation may use, and which evidence says so.

The production failure these tests encode: a renewal routed with one boolean. A
boolean cannot distinguish "the first-class client route does not exist" (v2.x,
v3.0.x) from "the credential was rejected" or "the panel never answered" - so an
auth or scope problem on a modern panel used to fall back to the legacy
inbound-based update, which the panel ignores or rejects, leaving a renewed
customer disabled.

Every claim in the version table is asserted here as behaviour, so a future edit
that widens a capability without evidence fails a test rather than production.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from panel.adapters import xui
from panel.services import panel_capabilities as caps_mod
from panel.services import xui_compat


def _compat(version_raw):
    version = xui_compat.normalize_version(version_raw)
    profile, _cert, _warnings = xui_compat.select_profile(version)
    return SimpleNamespace(detected_version=version_raw, profile=profile,
                           version=version)


class VersionMatrixTests(unittest.TestCase):
    """version (+ probe verdict) -> client API family and strategy."""

    def _caps(self, version_raw, *, route_proven, probe_state=None):
        compat = _compat(version_raw)
        state = probe_state or (
            caps_mod.PROBE_SUPPORTED if route_proven else caps_mod.PROBE_ROUTE_MISSING)
        return caps_mod.capabilities_from_version(
            xui_compat.normalize_version(version_raw), profile=compat.profile,
            client_route_proven=route_proven, probe_state=state)

    def test_v2_is_legacy(self):
        caps = self._caps('2.8.11', route_proven=False)
        self.assertEqual(caps.client_api_family, caps_mod.CLIENT_API_LEGACY)
        self.assertFalse(caps.client_get)
        self.assertEqual(caps_mod.select_renew_strategy(caps),
                         caps_mod.RenewStrategy.LEGACY_INBOUND)

    def test_v3_0_is_still_legacy_despite_major_3(self):
        # The whole point: major == 3 does not imply the first-class client API.
        caps = self._caps('3.0.0', route_proven=False)
        self.assertEqual(caps.client_api_family, caps_mod.CLIENT_API_LEGACY)
        self.assertEqual(caps_mod.select_renew_strategy(caps),
                         caps_mod.RenewStrategy.LEGACY_INBOUND)

    def test_a_proven_route_beats_a_legacy_looking_version(self):
        # A backported/unknown build that DOES answer /clients/get must be used as
        # first-class: the route is the authority, the version is a hint.
        caps = self._caps('3.0.0', route_proven=True)
        self.assertTrue(caps.first_class)
        self.assertEqual(caps_mod.select_renew_strategy(caps),
                         caps_mod.RenewStrategy.V3_EARLY)

    def test_an_unproven_route_is_never_guessed_into_first_class(self):
        # No probe verdict at all: the version alone may not promote a v3.0 panel.
        caps = caps_mod.capabilities_from_version(
            xui_compat.normalize_version('3.0.0'),
            profile=_compat('3.0.0').profile, client_route_proven=None,
            probe_state=caps_mod.PROBE_UNKNOWN)
        self.assertEqual(caps.client_api_family, caps_mod.CLIENT_API_LEGACY)
        self.assertFalse(caps.client_get)

    def test_v3_1_and_3_2_have_no_bulk_enable(self):
        for version in ('3.1.0', '3.2.0'):
            caps = self._caps(version, route_proven=True)
            self.assertTrue(caps.client_update)
            self.assertTrue(caps.bulk_adjust)
            self.assertFalse(caps.bulk_enable,
                             '%s must not claim bulkEnable' % version)
            self.assertFalse(caps.node_pending_response)
            self.assertEqual(caps_mod.select_renew_strategy(caps),
                             caps_mod.RenewStrategy.V3_EARLY)

    def test_node_pending_arrives_in_3_3_1_not_3_3_0(self):
        self.assertFalse(self._caps('3.3.0', route_proven=True).node_pending_response)
        self.assertTrue(self._caps('3.3.1', route_proven=True).node_pending_response)
        self.assertTrue(self._caps('3.4.0', route_proven=True).node_pending_response)
        self.assertEqual(
            caps_mod.select_renew_strategy(self._caps('3.4.0', route_proven=True)),
            caps_mod.RenewStrategy.V3_NODE_PENDING)

    def test_bulk_enable_arrives_in_3_5(self):
        self.assertFalse(self._caps('3.4.0', route_proven=True).bulk_enable)
        self.assertTrue(self._caps('3.5.0', route_proven=True).bulk_enable)
        self.assertTrue(self._caps('3.6.0', route_proven=True).bulk_enable)
        self.assertEqual(
            caps_mod.select_renew_strategy(self._caps('3.6.0', route_proven=True)),
            caps_mod.RenewStrategy.V3_BULK_ENABLE)

    def test_3_7_is_scoped_and_preserves_limit_hwid(self):
        caps = self._caps('3.7.0', route_proven=True)
        self.assertTrue(caps.scoped_tokens)
        self.assertTrue(caps.limit_hwid)
        self.assertTrue(caps.bulk_enable)
        self.assertEqual(caps_mod.select_renew_strategy(caps),
                         caps_mod.RenewStrategy.V3_SCOPED)

    def test_3_8_is_current(self):
        self.assertEqual(
            caps_mod.select_renew_strategy(self._caps('3.8.5', route_proven=True)),
            caps_mod.RenewStrategy.V3_CURRENT)

    def test_a_future_version_inherits_nothing(self):
        # 3.9/4.x must not silently inherit 3.8: only proven primitives are offered.
        caps = self._caps('3.9.0', route_proven=True)
        self.assertEqual(caps_mod.select_renew_strategy(caps),
                         caps_mod.RenewStrategy.UNKNOWN_FUTURE)
        self.assertFalse(caps.bulk_enable)
        self.assertFalse(caps.node_pending_response)
        self.assertTrue(caps.client_update)

    def test_an_unparsed_version_offers_only_the_oldest_primitive(self):
        caps = caps_mod.capabilities_from_version(
            None, profile=None, client_route_proven=True,
            probe_state=caps_mod.PROBE_SUPPORTED)
        self.assertTrue(caps.client_update)
        self.assertFalse(caps.bulk_enable)
        self.assertEqual(caps_mod.select_renew_strategy(caps),
                         caps_mod.RenewStrategy.UNKNOWN_FUTURE)

    def test_capabilities_view_is_credential_free(self):
        view = self._caps('3.8.5', route_proven=True).as_dict()
        self.assertNotIn('api_key', view)
        self.assertNotIn('api_token', view)
        self.assertNotIn('password', view)
        # `scoped_tokens` is a capability flag, not a credential: the guard is that
        # no key can hold a secret VALUE.
        for key, value in view.items():
            self.assertNotIsInstance(value, bytes, key)
            if key != 'scoped_tokens':
                self.assertNotIn('token', key)
        self.assertEqual(view['probe_state'], caps_mod.PROBE_SUPPORTED)


class ProbeFailureTests(unittest.TestCase):
    """Only a proven absent route may select legacy. Everything else fails closed."""

    def _server(self):
        return SimpleNamespace(id=77, host='http://127.0.0.1:2053', api_token='tok')

    def _call(self, probe_state, *, cached=None, version='3.8.0',
              legacy_state=caps_mod.PROBE_SUPPORTED):
        server = self._server()
        with mock.patch.object(xui, 'probe_v3_client_api', return_value=probe_state), \
                mock.patch.object(xui, 'probe_legacy_inbound_api',
                                  return_value=legacy_state), \
                mock.patch.object(xui_compat, 'cached_compatibility',
                                  return_value=_compat(version)):
            if cached is None:
                xui.XUI_CAPABILITY_CACHE.pop(77, None)
            else:
                xui.XUI_CAPABILITY_CACHE[77] = cached
            try:
                return caps_mod.capabilities_for(server, object())
            finally:
                xui.XUI_CAPABILITY_CACHE.pop(77, None)

    def test_a_route_missing_verdict_plus_a_legacy_panel_selects_legacy(self):
        # The legacy family must be POSITIVELY confirmed: POST /inbounds/onlines only
        # answers on the builds that still have the legacy client API.
        caps, reason = self._call(caps_mod.PROBE_ROUTE_MISSING,
                                  legacy_state=caps_mod.PROBE_SUPPORTED)
        self.assertEqual(caps.client_api_family, caps_mod.CLIENT_API_LEGACY)
        self.assertTrue(caps.legacy_inbound_update)
        self.assertIsNone(reason)

    def test_a_route_missing_verdict_alone_is_not_legacy(self):
        # A 404 can also be an aborted authentication, so it may not choose the legacy
        # write by itself. "Neither family answered" is unclassifiable, not legacy.
        for legacy_state in (caps_mod.PROBE_ROUTE_MISSING, caps_mod.PROBE_AUTH_INVALID,
                             caps_mod.PROBE_TRANSPORT_ERROR,
                             caps_mod.PROBE_INVALID_RESPONSE):
            caps, reason = self._call(caps_mod.PROBE_ROUTE_MISSING,
                                      legacy_state=legacy_state)
            self.assertEqual(
                caps_mod.select_renew_strategy(caps), caps_mod.RenewStrategy.BLOCKED,
                'legacy probe %s must block' % legacy_state)
            self.assertFalse(caps.legacy_inbound_update)
            self.assertTrue(reason)

    def test_a_supported_verdict_selects_first_class(self):
        caps, reason = self._call(caps_mod.PROBE_SUPPORTED)
        self.assertTrue(caps.first_class)
        self.assertIsNone(reason)

    def test_auth_scope_transport_and_garbage_never_select_legacy(self):
        for state in (caps_mod.PROBE_AUTH_INVALID, caps_mod.PROBE_SCOPE_INSUFFICIENT,
                      caps_mod.PROBE_TRANSPORT_ERROR, caps_mod.PROBE_INVALID_RESPONSE):
            caps, reason = self._call(state)
            self.assertEqual(
                caps_mod.select_renew_strategy(caps), caps_mod.RenewStrategy.BLOCKED,
                '%s must block, not downgrade to legacy' % state)
            self.assertFalse(caps.legacy_inbound_update,
                             '%s must not fall back to the legacy write' % state)
            self.assertTrue(reason)

    def test_a_previously_proven_panel_keeps_its_capability(self):
        import time as _time
        cached = {'v3_clients': True, 'expiry': _time.time() + 60}
        caps, reason = self._call(caps_mod.PROBE_AUTH_INVALID, cached=cached)
        self.assertTrue(caps.first_class)
        self.assertIsNone(reason)

    def test_a_probe_that_raises_blocks_instead_of_downgrading(self):
        server = self._server()
        with mock.patch.object(xui, 'probe_v3_client_api',
                               side_effect=RuntimeError('boom')), \
                mock.patch.object(xui_compat, 'cached_compatibility',
                                  return_value=_compat('3.8.0')):
            caps, reason = caps_mod.capabilities_for(server, object())
        self.assertEqual(caps_mod.select_renew_strategy(caps),
                         caps_mod.RenewStrategy.BLOCKED)
        self.assertFalse(caps.legacy_inbound_update)
        self.assertIn('probe', (reason or '').lower())


class BulkAdjustPlannerTests(unittest.TestCase):
    """bulkAdjust is a delta endpoint, not a general renewal engine."""

    def setUp(self):
        self.caps = caps_mod.capabilities_from_version(
            xui_compat.normalize_version('3.8.5'),
            profile=_compat('3.8.5').profile, client_route_proven=True,
            probe_state=caps_mod.PROBE_SUPPORTED)

    def test_a_plain_delta_extension_is_allowed(self):
        ok, reason = caps_mod.can_use_native_bulk_adjust(
            {'add_days': 30, 'add_bytes': 0}, {'depleted': False}, self.caps)
        self.assertTrue(ok, reason)

    def test_exact_cap_replacement_is_refused(self):
        for intent in ({'set_total_bytes': 100}, {'exact_expiry_ms': 123},
                       {'carry_over': True}, {'reset_traffic': True},
                       {'gift_bytes': 10 ** 9}, {'unlimited_volume': True},
                       {'unlimited_expiry': True}, {'start_after_first_use': True},
                       {'fractional_days': 1.5}):
            ok, reason = caps_mod.can_use_native_bulk_adjust(
                dict(intent, add_days=1), {'depleted': False}, self.caps)
            self.assertFalse(ok, '%s must not be sent as a bulkAdjust delta' % intent)
            self.assertTrue(reason)

    def test_a_still_depleted_account_is_refused(self):
        ok, reason = caps_mod.can_use_native_bulk_adjust(
            {'add_bytes': 10 ** 9}, {'depleted': True}, self.caps)
        self.assertFalse(ok)
        self.assertIn('depleted', reason)

    def test_a_manually_disabled_client_is_refused(self):
        # upstream's auto re-enable deliberately skips manually disabled clients.
        ok, reason = caps_mod.can_use_native_bulk_adjust(
            {'add_bytes': 10 ** 9}, {'manually_disabled': True}, self.caps)
        self.assertFalse(ok)
        self.assertIn('manual', reason)

    def test_a_panel_without_bulk_adjust_is_refused(self):
        early = caps_mod.capabilities_from_version(
            xui_compat.normalize_version('3.1.0'),
            profile=_compat('3.1.0').profile, client_route_proven=True,
            probe_state=caps_mod.PROBE_SUPPORTED)
        # 3.1 does expose bulkAdjust, so the refusal must come from the flag, not luck.
        self.assertTrue(early.bulk_adjust)
        no_adjust = caps_mod.PanelClientCapabilities(client_api_family='first_class',
                                                    bulk_adjust=False)
        ok, reason = caps_mod.can_use_native_bulk_adjust(
            {'add_days': 1}, {}, no_adjust)
        self.assertFalse(ok)
        self.assertIn('bulkAdjust', reason)


class MutationResultTests(unittest.TestCase):
    """The update response is parsed, not discarded."""

    def test_node_pending_is_extracted(self):
        result = xui.classify_mutation_result(
            True, {'success': True, 'obj': {'nodePending': True}})
        self.assertTrue(result.ok)
        self.assertTrue(result.node_pending)

    def test_an_older_panel_reports_no_node_pending(self):
        result = xui.classify_mutation_result(True, {'success': True, 'obj': {}})
        self.assertFalse(result.node_pending)
        self.assertTrue(result.ok)

    def test_a_per_email_skip_is_a_failure(self):
        result = xui.classify_mutation_result(
            True, {'success': True, 'obj': {'skipped': [
                {'email': 'a@b', 'reason': 'limit reached'}]}})
        self.assertFalse(result.ok)
        self.assertTrue(result.skipped)

    def test_success_false_on_http_200_is_a_failure(self):
        result = xui.classify_mutation_result(
            True, {'success': False, 'msg': 'invalid client'})
        self.assertFalse(result.ok)
        self.assertIn('invalid client', result.error)

    def test_a_transport_failure_may_be_partial(self):
        result = xui.classify_mutation_result(
            False, None, 'timeout', may_be_partial=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.partially_applied)

    def test_the_summary_carries_no_payload(self):
        summary = xui.classify_mutation_result(
            True, {'success': True, 'obj': {'nodePending': True}}).as_dict()
        self.assertEqual(set(summary), {'transport_ok', 'panel_success', 'node_pending',
                                        'skipped', 'partially_applied', 'need_restart',
                                        'error'})


if __name__ == '__main__':
    unittest.main()
