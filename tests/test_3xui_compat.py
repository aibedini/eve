import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    XUI_CAPABILITY_CACHE,
    _json_field,
    _probe_v3_client_api,
    generate_client_link,
    server_is_v3,
    v3_attach_client,
)


class _Response:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else '')
        self.content = self.text.encode()
        self.headers = {'Content-Type': 'application/json' if payload is not None else 'text/html'}

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class _Session:
    def __init__(self, get_response=None, post_response=None):
        self.get_response = get_response
        self.post_response = post_response
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_response


class XuiCompatibilityTests(unittest.TestCase):
    def test_audited_upstream_refs_are_recorded_in_the_contract_fixture(self):
        fixture = Path(__file__).parent / 'fixtures' / 'xui' / 'README.md'
        text = fixture.read_text(encoding='utf-8')
        self.assertIn('f727d04f6522bb94a8fb52e8352fdcafb51c11e1', text)
        self.assertIn('837addf66e945a80080273b5d2a315dea765d748', text)

    def setUp(self):
        XUI_CAPABILITY_CACHE.clear()

    def tearDown(self):
        XUI_CAPABILITY_CACHE.clear()

    def test_cookie_authenticated_v3_is_detected_by_endpoint_capability(self):
        server = SimpleNamespace(id=4101, host='https://panel.example/base', api_token='')
        session = _Session(get_response=_Response(200, {
            'success': False,
            'msg': 'client not found',
            'obj': None,
        }))

        self.assertTrue(_probe_v3_client_api(server, session))
        self.assertTrue(server_is_v3(server))
        self.assertEqual(len(session.get_calls), 1)
        self.assertIn('/base/panel/api/clients/get/__eve_capability_probe__', session.get_calls[0][0])

    def test_legacy_html_or_missing_route_is_not_misdetected_as_v3(self):
        server = SimpleNamespace(id=4102, host='https://legacy.example', api_token='')
        session = _Session(get_response=_Response(404, None, '<html>not found</html>'))

        self.assertFalse(_probe_v3_client_api(server, session))
        self.assertFalse(server_is_v3(server))

    def test_invalid_bearer_response_does_not_override_capability_detection(self):
        server = SimpleNamespace(id=4104, host='https://panel.example', api_token='bad-token')
        session = _Session(get_response=_Response(401, {
            'success': False,
            'msg': 'unauthorized',
        }))

        self.assertFalse(server_is_v3(server, session, force_probe=True))

    def test_nested_and_string_inbound_json_both_remain_supported(self):
        value = {'clients': [{'email': 'new-panel'}]}
        self.assertEqual(_json_field(value), value)
        self.assertEqual(_json_field('{"clients":[{"email":"old-panel"}]}')['clients'][0]['email'],
                         'old-panel')

    def test_native_attach_uses_protocol_aware_v3_endpoint(self):
        server = SimpleNamespace(id=4103, host='https://panel.example/root', api_token='token')
        session = _Session(post_response=_Response(200, {'success': True, 'obj': {}}))

        ok, _payload, error = v3_attach_client(server, session, 'alice@example.com', [7, 9])

        self.assertTrue(ok, error)
        self.assertEqual(len(session.post_calls), 1)
        url, kwargs = session.post_calls[0]
        self.assertTrue(url.endswith('/root/panel/api/clients/alice%40example.com/attach'))
        self.assertEqual(kwargs['json'], {'inboundIds': [7, 9]})

    def test_wireguard_fallback_link_contains_generated_contract_fields(self):
        # RFC 7748-style 32-byte private material; the implementation derives
        # the server public key exactly as 3x-ui does when only secretKey exists.
        inbound = {
            'protocol': 'wireguard',
            'port': 51820,
            'remark': 'WG',
            'settings': {
                'secretKey': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
                'mtu': 1420,
                'dns': '1.1.1.1',
            },
            'streamSettings': {},
        }
        client = {
            'email': 'alice',
            'privateKey': 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=',
            'allowedIPs': ['10.0.0.2/32'],
            'preSharedKey': 'shared/key=',
            'keepAlive': 25,
        }

        link = generate_client_link(client, inbound, 'https://vpn.example:2053')

        parsed = urlparse(link)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, 'wireguard')
        self.assertEqual(parsed.hostname, 'vpn.example')
        self.assertEqual(parsed.port, 51820)
        self.assertEqual(query['address'], ['10.0.0.2/32'])
        self.assertEqual(query['mtu'], ['1420'])
        self.assertEqual(query['dns'], ['1.1.1.1'])
        self.assertEqual(query['presharedkey'], ['shared/key='])
        self.assertEqual(query['keepalive'], ['25'])
        self.assertTrue(query['publickey'][0])

    def test_mtproto_fallback_link_uses_per_client_secret_and_adtag(self):
        inbound = {
            'protocol': 'mtproto',
            'port': 443,
            'remark': 'MTProto',
            'settings': {'clients': []},
            'streamSettings': {},
        }
        client = {
            'email': 'alice',
            'Secret': 'dd00000000000000000000000000000000',
            'AdTag': 'sponsor-channel',
        }

        link = generate_client_link(client, inbound, 'https://vpn.example:2053')

        parsed = urlparse(link)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, 'tg')
        self.assertEqual(parsed.netloc, 'proxy')
        self.assertEqual(query['server'], ['vpn.example'])
        self.assertEqual(query['port'], ['443'])
        self.assertEqual(query['secret'], ['dd00000000000000000000000000000000'])
        self.assertEqual(query['adtag'], ['sponsor-channel'])


# --------------------------------------------------------------------------- #
# 3x-ui 3.7.x / 3.8.x version-gated compatibility (specs/001-3xui-37-38-compat)
# --------------------------------------------------------------------------- #

from app import (  # noqa: E402
    COMPAT_CACHE,
    PROFILE_BASELINE_V3,
    PROFILE_XUI_3_7,
    PROFILE_XUI_3_8,
    PanelVersion,
    _classify_probe_response,
    _probe_headers_for,
    _v3_client_payload,
    detect_lifecycle_automation,
    normalize_version,
    process_inbounds,
    preserved_limit_hwid,
    resolve_compatibility,
    select_profile,
)
from panel.adapters.xui import (  # noqa: E402
    PROBE_AUTH_INVALID,
    PROBE_INVALID_RESPONSE,
    PROBE_ROUTE_MISSING,
    PROBE_SCOPE_INSUFFICIENT,
    PROBE_SUPPORTED,
    _UNSET,
)
from panel.services import subscription as subscription_service  # noqa: E402


class _StatusResponse:
    def __init__(self, status_code, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else '')
        self.content = self.text.encode()
        self.headers = {'Content-Type': 'application/json' if payload is not None else 'text/html'}

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class VersionNormalizationTests(unittest.TestCase):
    """A version is data, never a string comparison."""

    def test_accepts_upstream_shapes_and_an_optional_v_prefix(self):
        for raw, family in (
            ('3.7.0', (3, 7)), ('v3.7.0', (3, 7)), ('3.7', (3, 7)), ('3.7.9', (3, 7)),
            ('3.8.0', (3, 8)), ('v3.8.0', (3, 8)), ('3.8.1', (3, 8)),
            ('3.8.0+build.1', (3, 8)),
        ):
            parsed = normalize_version(raw)
            self.assertTrue(parsed.is_parsed, raw)
            self.assertEqual(parsed.family, family, raw)

    def test_unusable_values_are_unknown_not_a_default(self):
        for raw in ('dev+', '', None, 'garbage', 'unknown', 'main-abcdef1'):
            parsed = normalize_version(raw)
            self.assertFalse(parsed.is_parsed, repr(raw))
            self.assertIsNone(parsed.family, repr(raw))

    def test_patch_differences_stay_in_the_same_family(self):
        self.assertEqual(normalize_version('3.7.0').family, normalize_version('3.7.9').family)
        self.assertEqual(normalize_version('3.8.0').family, normalize_version('3.8.7').family)


class ProfileSelectionTests(unittest.TestCase):
    """The gating rule: only certified families get non-baseline behaviour."""

    def test_exactly_37_and_38_select_their_profiles(self):
        self.assertIs(select_profile(normalize_version('3.7.0'))[0], PROFILE_XUI_3_7)
        self.assertIs(select_profile(normalize_version('3.7.9'))[0], PROFILE_XUI_3_7)
        self.assertIs(select_profile(normalize_version('3.8.0'))[0], PROFILE_XUI_3_8)
        self.assertIs(select_profile(normalize_version('3.8.1'))[0], PROFILE_XUI_3_8)

    def test_future_versions_never_inherit_the_38_profile(self):
        for raw in ('3.9.0', '3.9.9', '4.0.0', '5.1.0'):
            profile, certification, warnings = select_profile(normalize_version(raw))
            self.assertIs(profile, PROFILE_BASELINE_V3, raw)
            self.assertNotEqual(profile.name, PROFILE_XUI_3_8.name, raw)
            self.assertEqual(certification, 'certification_required', raw)
            self.assertIn('future_version_uncertified', warnings, raw)

    def test_existing_supported_families_keep_their_status(self):
        for raw in ('3.3.1', '3.5.0', '3.6.1'):
            profile, certification, warnings = select_profile(normalize_version(raw))
            self.assertIs(profile, PROFILE_BASELINE_V3, raw)
            self.assertEqual(certification, 'supported', raw)
            self.assertEqual(list(warnings), [], raw)

    def test_unknown_version_is_baseline_unverified_never_guessed(self):
        for raw in ('dev+', '', None, 'garbage'):
            profile, certification, warnings = select_profile(normalize_version(raw))
            self.assertIs(profile, PROFILE_BASELINE_V3, repr(raw))
            self.assertEqual(certification, 'unverified', repr(raw))
            self.assertIn('panel_version_unknown', warnings, repr(raw))

    def test_only_38_declares_the_randomized_path_and_tuic_capabilities(self):
        self.assertTrue(PROFILE_XUI_3_8.random_subscription_paths)
        self.assertFalse(PROFILE_XUI_3_7.random_subscription_paths)
        self.assertTrue(PROFILE_XUI_3_8.tuic)
        self.assertFalse(PROFILE_XUI_3_7.tuic)

    def test_37_not_38_requires_the_bearer_hint_header(self):
        # Evidence: checkAPIAuth differs between the tags; 3.7 answers 404 unless
        # X-Requested-With is present, 3.8 answers 401 on the Bearer alone.
        self.assertTrue(PROFILE_XUI_3_7.bearer_hint_header_required)
        self.assertFalse(PROFILE_XUI_3_7.bearer_rejection_is_401)
        self.assertFalse(PROFILE_XUI_3_8.bearer_hint_header_required)
        self.assertTrue(PROFILE_XUI_3_8.bearer_rejection_is_401)


class ProbeClassificationTests(unittest.TestCase):
    """An auth failure is never evidence that a route is absent."""

    def test_status_codes_map_to_distinct_outcomes(self):
        self.assertEqual(_classify_probe_response(_StatusResponse(401, {'success': False})), PROBE_AUTH_INVALID)
        self.assertEqual(_classify_probe_response(_StatusResponse(403, {'success': False, 'msg': 'scope'})), PROBE_SCOPE_INSUFFICIENT)
        self.assertEqual(_classify_probe_response(_StatusResponse(404, None, '<html>')), PROBE_ROUTE_MISSING)
        self.assertEqual(_classify_probe_response(_StatusResponse(200, {'success': False, 'msg': 'client not found'})), PROBE_SUPPORTED)
        self.assertEqual(_classify_probe_response(_StatusResponse(200, None, 'not json')), PROBE_INVALID_RESPONSE)

    def test_401_and_403_are_never_route_missing(self):
        for code in (401, 403):
            outcome = _classify_probe_response(_StatusResponse(code, {'success': False}))
            self.assertNotEqual(outcome, PROBE_ROUTE_MISSING, code)

    def test_hint_header_is_sent_only_where_it_changes_the_answer(self):
        from types import SimpleNamespace
        from panel.services.xui_compat import PanelCompatibility, SOURCE_SERVER_STATUS, CONF_AUTHORITATIVE
        import panel.services.xui_compat as compat_mod

        def headers_for(profile):
            server = SimpleNamespace(id=7701)
            compat_mod.COMPAT_CACHE[int(server.id)] = {
                'value': PanelCompatibility(server_id=7701, profile=profile,
                                            detection_source=SOURCE_SERVER_STATUS,
                                            confidence=CONF_AUTHORITATIVE),
                'expiry': __import__('time').time() + 60,
            }
            return _probe_headers_for(server)

        try:
            self.assertIn('X-Requested-With', headers_for(PROFILE_XUI_3_7))
            self.assertNotIn('X-Requested-With', headers_for(PROFILE_XUI_3_8))
            self.assertNotIn('X-Requested-With', headers_for(PROFILE_BASELINE_V3))
        finally:
            compat_mod.COMPAT_CACHE.clear()


class ClientDeviceLimitPreservationTests(unittest.TestCase):
    """The P0 defect: the panel writes limitHwid unconditionally and defaults an
    absent key to 0, so EVE must echo the authoritative value."""

    def test_preserved_value_is_emitted_as_a_sibling(self):
        payload = _v3_client_payload({'email': 'a@b.c', 'id': 'uuid-1'}, limit_hwid=2)
        self.assertEqual(payload['limitHwid'], 2)
        self.assertNotIn('limitHwid', payload['id'])
        self.assertEqual(payload['id'], 'uuid-1')

    def test_absent_value_is_omitted_and_never_defaulted_to_zero(self):
        payload = _v3_client_payload({'email': 'a@b.c', 'id': 'uuid-1'})
        self.assertNotIn('limitHwid', payload)

    def test_a_stored_zero_is_a_real_value_and_round_trips(self):
        payload = _v3_client_payload({'email': 'a@b.c', 'id': 'uuid-1'}, limit_hwid=0)
        self.assertEqual(payload['limitHwid'], 0)

    def test_reader_only_consults_the_field_it_is_allowed_to_read(self):
        self.assertEqual(preserved_limit_hwid({'limitHwid': 3}), 3)
        self.assertEqual(preserved_limit_hwid({'limitHwid': 0}), 0)
        self.assertIsNone(preserved_limit_hwid({'email': 'a@b.c'}))
        self.assertIsNone(preserved_limit_hwid(None))
        self.assertIsNone(preserved_limit_hwid({'limitHwid': None}))
        self.assertIsNone(preserved_limit_hwid({'limitHwid': 'nonsense'}))

    def test_only_the_device_limit_is_on_the_preservation_allowlist(self):
        from panel.services.xui_compat import PRESERVED_CLIENT_FIELDS
        self.assertEqual(tuple(PRESERVED_CLIENT_FIELDS), ('limitHwid',))

    def test_unrelated_fields_are_carried_through_unchanged(self):
        client = {
            'email': 'a@b.c', 'id': 'uuid-1', 'flow': 'xtls-rprx-vision',
            'subId': 'sub123', 'comment': 'keep me', 'enable': True,
            'expiryTime': 1790000000000, 'totalGB': 1073741824,
            'resetDay': 5, 'resetMax': 3, 'trafficReset': 'monthly',
            'trafficResetDay': 9, 'keepAlive': 25,
        }
        payload = _v3_client_payload(client, limit_hwid=2)
        for key, value in client.items():
            if key == 'id':
                continue
            self.assertEqual(payload[key], value, key)
        self.assertEqual(payload['limitHwid'], 2)

    def test_secret_bearing_fields_are_never_invented(self):
        payload = _v3_client_payload({'email': 'a@b.c', 'id': 'uuid-1'}, limit_hwid=2)
        for forbidden in ('apiToken', 'password_hash', 'privateKey', 'preSharedKey'):
            self.assertNotIn(forbidden, payload)


class LifecycleAutomationTests(unittest.TestCase):
    """EVE stays the lifecycle authority; panel-side automation is surfaced, not adopted."""

    def test_quiet_clients_report_nothing(self):
        self.assertIsNone(detect_lifecycle_automation({'email': 'a@b.c', 'resetDay': 0, 'resetMax': 0, 'trafficReset': 'never'}))
        self.assertIsNone(detect_lifecycle_automation(None))

    def test_calendar_renewal_is_reported_as_partially_managed(self):
        finding = detect_lifecycle_automation({'email': 'a@b.c', 'resetDay': 15, 'resetMax': 0, 'trafficReset': 'never'})
        self.assertIsNotNone(finding)
        self.assertEqual(finding['managed_state'], 'partially_managed')
        self.assertEqual(finding['warning'], 'panel_lifecycle_automation_detected')

    def test_traffic_reset_cycle_is_reported(self):
        self.assertIsNotNone(detect_lifecycle_automation({'email': 'a@b.c', 'trafficReset': 'monthly'}))
        self.assertIsNotNone(detect_lifecycle_automation({'email': 'a@b.c', 'resetMax': 4}))

    def test_client_read_path_surfaces_partial_management_without_lifecycle_event(self):
        server = SimpleNamespace(
            id=3801,
            name='3.8 panel',
            host='https://panel.example',
            panel_type='v3',
            sub_port=None,
            sub_path='/configured/',
            json_path='/json/',
        )
        user = SimpleNamespace(role='superadmin', id=1, is_superadmin=True)
        inbounds = [{
            'id': 7,
            'enable': True,
            'settings': {'clients': [{
                'id': 'client-uuid',
                'email': 'managed@example.test',
                'enable': True,
                'expiryTime': 0,
                'totalGB': 0,
                'resetDay': 15,
                'resetMax': 2,
                'trafficReset': 'monthly',
                'trafficResetDay': 1,
            }]},
            'clientStats': [],
        }]
        resolve_compatibility(3801, '3.8.0', source='server_status', confidence='authoritative')

        from app import app
        with app.app_context(), mock.patch('app.server_is_v3', return_value=True), mock.patch(
                'panel.services.lifecycle.handle_successful_service_lifecycle_change') as lifecycle_event:
            processed, _stats = process_inbounds(inbounds, server, user)

        client = processed[0]['clients'][0]
        self.assertEqual(client['managed_state'], 'partially_managed')
        self.assertEqual(
            client['lifecycle_automation']['warning'],
            'panel_lifecycle_automation_detected',
        )
        self.assertEqual(client['lifecycle_automation']['fields']['resetDay'], 15)
        lifecycle_event.assert_not_called()
        self.assertIn(
            'panel_lifecycle_automation_detected',
            subscription_service.xui_compat.cached_compatibility(3801).warnings,
        )


class SubscriptionPathAuthorityTests(unittest.TestCase):
    def setUp(self):
        COMPAT_CACHE.clear()
        subscription_service.SUBSCRIPTION_PROFILE_CACHE.clear()
        self.server = SimpleNamespace(
            id=3802,
            name='3.8 panel',
            host='https://panel.example:8443',
            sub_port=2096,
            sub_path='/configured-sub/',
            json_path='/configured-json/',
        )

    def tearDown(self):
        COMPAT_CACHE.clear()
        subscription_service.SUBSCRIPTION_PROFILE_CACHE.clear()

    def _metadata(self, settings):
        session = _Session(post_response=_Response(200, {'success': True, 'obj': settings}))
        return subscription_service.fetch_subscription_profile_metadata(
            self.server,
            session_obj=session,
        )

    def test_38_randomized_subscription_path_is_authoritative(self):
        resolve_compatibility(3802, '3.8.0')
        metadata = self._metadata({
            'subPath': '/randomabcdefghijkl/',
            'subJsonPath': '/random-json/',
            'subClashPath': '/random-clash/',
        })

        paths = subscription_service.resolve_subscription_paths(
            self.server, profile_metadata=metadata)

        self.assertEqual(paths['sub_path'], '/randomabcdefghijkl/')
        self.assertEqual(paths['sub_json_path'], '/random-json/')
        self.assertEqual(paths['sub_clash_path'], '/random-clash/')
        self.assertEqual(paths['source'], 'panel')
        self.assertFalse(paths['fallback'])

    def test_operator_changed_panel_path_takes_precedence(self):
        resolve_compatibility(3802, '3.8.4')
        metadata = self._metadata({'subPath': '/operator-selected/'})

        url = subscription_service.build_panel_subscription_url(
            self.server, 'subscriber-id', profile_metadata=metadata)
        paths = subscription_service.resolve_subscription_paths(
            self.server, profile_metadata=metadata)

        self.assertEqual(url, 'https://panel.example:2096/operator-selected/subscriber-id')
        self.assertEqual(paths['sub_json_path'], '/configured-json/')
        self.assertEqual(paths['source'], 'panel_partial_fallback')
        self.assertTrue(paths['fallback'])
        self.assertIn('subscription_path_fallback', paths['warnings'])

    def test_settings_failure_uses_explicit_observable_fallback(self):
        resolve_compatibility(3802, '3.8.0')
        metadata = self._metadata({})

        paths = subscription_service.resolve_subscription_paths(
            self.server, profile_metadata=metadata)

        self.assertEqual(paths['sub_path'], '/configured-sub/')
        self.assertEqual(paths['source'], 'configured_fallback')
        self.assertTrue(paths['fallback'])
        self.assertIn('subscription_path_fallback', paths['warnings'])
        self.assertIn(
            'subscription_path_fallback',
            subscription_service.xui_compat.cached_compatibility(3802).warnings,
        )

    def test_pre_38_behavior_ignores_panel_advertised_path(self):
        resolve_compatibility(3802, '3.7.9')
        metadata = self._metadata({'subPath': '/panel-value-must-not-apply/'})

        paths = subscription_service.resolve_subscription_paths(
            self.server, profile_metadata=metadata)

        self.assertEqual(paths['sub_path'], '/configured-sub/')
        self.assertEqual(paths['source'], 'configured')
        self.assertFalse(paths['fallback'])


if __name__ == '__main__':
    unittest.main()
