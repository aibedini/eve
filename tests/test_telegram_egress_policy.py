"""Phase 0.5-B: the egress policy is explicit and fail-closed.

The defect these tests pin: route FAILOVER used to be the only thing deciding
whether a message could leave the host directly, so a proxy that was merely
cooling down silently dropped out of the ordering and the direct route was
used next. The policy now decides the maximum route set once, and the
transport may only order what the policy allowed.
"""
import base64
import os
import unittest
from unittest import mock

os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from panel import telegram_egress as egress  # noqa: E402
from telegram_bot_runtime import (  # noqa: E402
    TelegramApiError, TelegramBotApi, TelegramEgressUnavailable, TelegramRoute,
)


class _Recorder:
    """Records every outbound attempt so a test can prove a route was NEVER used."""

    def __init__(self, outcome='ok'):
        self.attempts = []
        self._outcome = outcome

    def post(self, url, proxies=None, **kwargs):
        self.attempts.append({'url': url, 'proxies': proxies,
                              'via': 'direct' if not proxies else 'managed'})
        return self._response()

    def get(self, url, proxies=None, **kwargs):
        self.attempts.append({'url': url, 'proxies': proxies,
                              'via': 'direct' if not proxies else 'managed'})
        return self._response()

    def close(self):
        pass

    def _response(self):
        if self._outcome == 'ok':
            return mock.Mock(status_code=200, content=b'{}',
                             json=lambda: {'ok': True, 'result': {'message_id': 1}})
        raise __import__('requests').ConnectionError('proxy refused the connection')

    def direct_attempts(self):
        return [a for a in self.attempts if a['via'] == 'direct']

    def managed_attempts(self):
        return [a for a in self.attempts if a['via'] == 'managed']


class _FailManagedRecorder(_Recorder):
    """The managed route is down but the direct route answers: the failover case."""

    def __init__(self):
        super().__init__(outcome='ok')
        self._managed_failures = 0

    def post(self, url, proxies=None, **kwargs):
        self.attempts.append({'url': url, 'proxies': proxies,
                              'via': 'direct' if not proxies else 'managed'})
        if proxies and self._managed_failures < 1:
            self._managed_failures += 1
            import requests
            raise requests.ConnectionError('proxy refused the connection')
        return self._response()


MANAGED = TelegramRoute('xray://de-02', {'https': 'socks5h://127.0.0.1:1081'})
DIRECT = TelegramRoute('direct')


_TOKEN_SEQ = {'n': 0}


def _api(policy, *, managed=True, recorder=None):
    # A UNIQUE token per transport: the route-state (preferred route + cooldowns)
    # is keyed by the token hash and lives in thread-local storage, so sharing one
    # token would leak cooldowns between tests.
    _TOKEN_SEQ['n'] += 1
    routes = [MANAGED, DIRECT] if managed else [DIRECT]
    api = TelegramBotApi('123:token-%d' % _TOKEN_SEQ['n'], routes, policy=policy)
    api._session = recorder or _Recorder()
    return api


class EgressDecisionTests(unittest.TestCase):
    def test_the_legacy_modes_map_to_what_they_actually_did(self):
        self.assertEqual(egress.normalize_policy('proxy_only'), egress.PROXY_REQUIRED)
        self.assertEqual(egress.normalize_policy('proxy_first'), egress.PROXY_PREFERRED)
        self.assertEqual(egress.normalize_policy('direct_only'), egress.DIRECT_ONLY)
        self.assertEqual(egress.normalize_policy('auto'), egress.DIRECT_PREFERRED)

    def test_policy_names_are_idempotent_and_validated(self):
        for policy in egress.EGRESS_POLICIES:
            self.assertEqual(egress.normalize_policy(policy), policy)
            self.assertTrue(egress.is_valid_policy(policy))
        for mode in ('auto', 'direct_only', 'proxy_first', 'proxy_only'):
            self.assertTrue(egress.is_valid_policy(mode))
        self.assertFalse(egress.is_valid_policy('whatever'))
        self.assertFalse(egress.is_valid_policy(''))
        # An unknown value is never silently treated as permission for direct.
        self.assertNotIn(egress.DIRECT_ROUTE,
                         egress.decide('whatever', has_managed=True).allowed)

    def test_strict_policies_never_allow_the_direct_route(self):
        for policy in (egress.NEVER_DIRECT, egress.PROXY_REQUIRED,
                       egress.PANEL_ACCOUNT_REQUIRED):
            decision = egress.decide(policy, has_managed=True)
            self.assertFalse(decision.allow_direct, policy)
            self.assertNotIn(egress.DIRECT_ROUTE, decision.allowed, policy)

    def test_proxy_preferred_allows_direct_because_policy_says_so(self):
        decision = egress.decide(egress.PROXY_PREFERRED, has_managed=True)
        self.assertTrue(decision.allow_direct)
        self.assertEqual(decision.allowed,
                         ('managed', egress.DIRECT_ROUTE))
        self.assertEqual(decision.reason, 'policy_allows_direct_fallback')

    def test_a_strict_policy_with_no_managed_route_fails_closed(self):
        decision = egress.decide(egress.PROXY_REQUIRED, has_managed=False)
        self.assertFalse(decision.usable)
        self.assertEqual(decision.reason, 'no_managed_route_configured')


class TransportFailClosedTests(unittest.TestCase):
    """The proof that matters: a forbidden route is NEVER contacted."""

    def test_never_direct_with_the_proxy_down_makes_zero_direct_attempts(self):
        recorder = _Recorder(outcome='fail')
        api = _api(egress.NEVER_DIRECT, recorder=recorder)
        with self.assertRaises(TelegramEgressUnavailable):
            api.send_message(1, 'hello')
        self.assertEqual(recorder.direct_attempts(), [])
        self.assertTrue(recorder.managed_attempts())  # it did try the policy route

    def test_proxy_required_with_the_proxy_down_makes_zero_direct_attempts(self):
        recorder = _Recorder(outcome='fail')
        api = _api(egress.PROXY_REQUIRED, recorder=recorder)
        with self.assertRaises(TelegramEgressUnavailable):
            api.send_message(1, 'hello')
        self.assertEqual(recorder.direct_attempts(), [])

    def test_panel_account_required_with_no_managed_route_makes_zero_attempts(self):
        recorder = _Recorder()
        api = _api(egress.PANEL_ACCOUNT_REQUIRED, managed=False, recorder=recorder)
        with self.assertRaises(TelegramEgressUnavailable):
            api.send_message(1, 'hello')
        self.assertEqual(recorder.attempts, [])
        self.assertFalse(api.can_send())

    def test_a_cooling_down_proxy_does_not_hand_the_turn_to_direct(self):
        recorder = _Recorder(outcome='fail')
        api = _api(egress.PROXY_REQUIRED, recorder=recorder)
        with self.assertRaises(TelegramEgressUnavailable):
            api.send_message(1, 'first')
        # The managed route is now in cooldown; the old ordering would have
        # emptied the active set and moved on to whatever was left.
        before = len(recorder.direct_attempts())
        with self.assertRaises(TelegramEgressUnavailable):
            api.send_message(1, 'second')
        self.assertEqual(len(recorder.direct_attempts()), before)
        self.assertEqual(recorder.direct_attempts(), [])

    def test_proxy_preferred_does_fall_back_because_the_policy_allows_it(self):
        recorder = _FailManagedRecorder()
        api = _api(egress.PROXY_PREFERRED, recorder=recorder)
        result, route = api.send_message(1, 'hello')
        self.assertEqual(route, 'direct')
        self.assertTrue(recorder.managed_attempts())
        self.assertTrue(recorder.direct_attempts())

    def test_direct_preferred_tries_direct_first(self):
        recorder = _Recorder()
        api = _api(egress.DIRECT_PREFERRED, recorder=recorder)
        _result, route = api.send_message(1, 'hello')
        self.assertEqual(route, 'direct')
        self.assertEqual(len(recorder.attempts), 1)

    def test_direct_only_never_touches_a_managed_route(self):
        recorder = _Recorder()
        api = _api(egress.DIRECT_ONLY, recorder=recorder)
        _result, route = api.send_message(1, 'hello')
        self.assertEqual(route, 'direct')
        self.assertEqual(recorder.managed_attempts(), [])

    def test_the_transport_without_a_policy_keeps_its_historical_behaviour(self):
        recorder = _FailManagedRecorder()
        api = TelegramBotApi('123:token-policyless', [MANAGED, DIRECT])
        api._session = recorder
        _result, route = api.send_message(1, 'hello')
        self.assertEqual(route, 'direct')  # unchanged for policy-less callers
        self.assertIsNone(api.policy)

    def test_egress_unavailable_is_retryable_and_typed(self):
        recorder = _Recorder(outcome='fail')
        api = _api(egress.NEVER_DIRECT, recorder=recorder)
        try:
            api.send_message(1, 'hello')
            self.fail('expected TelegramEgressUnavailable')
        except TelegramEgressUnavailable as exc:
            self.assertTrue(exc.retryable)  # a dependency outage can recover
            self.assertEqual(exc.policy, egress.NEVER_DIRECT)
            self.assertEqual(exc.code, 'egress_unavailable')
            self.assertIsInstance(exc, TelegramApiError)
            self.assertGreaterEqual(exc.attempts, 1)

    def test_a_forbidden_route_is_absent_even_from_the_ordered_list(self):
        api = _api(egress.NEVER_DIRECT, recorder=_Recorder())
        self.assertEqual([route.name for route in api._ordered_routes()],
                         ['xray://de-02'])
        api = _api(egress.PROXY_PREFERRED, recorder=_Recorder())
        self.assertEqual([route.name for route in api._ordered_routes()],
                         ['xray://de-02', 'direct'])

    def test_document_upload_follows_the_same_policy(self):
        recorder = _Recorder(outcome='fail')
        api = _api(egress.PROXY_REQUIRED, recorder=recorder)
        with self.assertRaises(TelegramEgressUnavailable):
            api.send_document(1, b'payload', filename='x.txt')
        self.assertEqual(recorder.direct_attempts(), [])


if __name__ == '__main__':
    unittest.main()

