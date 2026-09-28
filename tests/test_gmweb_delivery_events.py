"""Contract v5 delivery-event diagnostics stay bounded, private and read-only."""

import unittest

from panel.services import gmweb_contract


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        self.content = b"x" if payload is not None else b""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class DeliveryEventClientTests(unittest.TestCase):
    def setUp(self):
        gmweb_contract.reset_cache()

    def test_filters_and_auth_are_forwarded_and_limit_is_capped(self):
        calls = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            return Response(payload={"events": [{
                "eventId": "dlr_1", "status": "delivered",
                "requestId": "send_1", "occurredAt": "2026-09-28T00:00:00Z",
            }]})

        result = gmweb_contract.fetch_delivery_events(
            "https://gmweb.example", "secret", {
                "from": "10", "to": "20", "status": "delivered",
                "requestId": "send_1", "callbackState": "delivered", "limit": "900",
            }, request_get=get)
        self.assertTrue(result["ok"])
        self.assertEqual(result["limit"], 100)
        self.assertEqual(calls[0][0], "https://gmweb.example/eve/v1/sms-delivery-events")
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(calls[0][1]["params"]["limit"], 100)

    def test_v4_absence_is_a_capability_result(self):
        result = gmweb_contract.fetch_delivery_events(
            "https://gmweb.example", "secret", request_get=lambda *a, **k: Response(404, {}))
        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "contract_missing")

    def test_auth_scope_timeout_and_server_failures_are_typed_without_secrets(self):
        for status, reason in ((401, "auth_failed"), (403, "scope_denied"),
                               (503, "gateway_http_503")):
            result = gmweb_contract.fetch_delivery_events(
                "https://gmweb.example", "top-secret",
                request_get=lambda *a, _status=status, **k: Response(_status, {}))
            self.assertEqual(result["reason"], reason)
            self.assertNotIn("top-secret", str(result))

        def timeout(*args, **kwargs):
            raise TimeoutError("includes no credentials")

        result = gmweb_contract.fetch_delivery_events(
            "https://gmweb.example", "top-secret", request_get=timeout)
        self.assertEqual(result["reason"], "gateway_unreachable:TimeoutError")
        self.assertNotIn("top-secret", str(result))

    def test_malformed_oversized_and_forbidden_events_are_rejected(self):
        payloads = [
            {"events": "not-a-list"},
            {"events": [{"eventId": "dlr_1", "status": "delivered", "to": "0912"}]},
            {"events": [{"eventId": "dlr_1", "status": "invented"}]},
            {"events": [{"eventId": "dlr_1", "status": "delivered"}] * 51},
        ]
        for payload in payloads:
            result = gmweb_contract.fetch_delivery_events(
                "https://gmweb.example", "secret", {"limit": 50},
                request_get=lambda *a, _payload=payload, **k: Response(200, _payload))
            self.assertEqual(result["reason"], "invalid_response", payload)

    def test_filter_validation_rejects_reverse_time_and_unknown_states(self):
        for filters in ({"from": 20, "to": 10}, {"status": "sent"},
                        {"callbackState": "unknown"}, {"requestId": "bad value"}):
            result = gmweb_contract.fetch_delivery_events(
                "https://gmweb.example", "secret", filters,
                request_get=lambda *a, **k: self.fail("invalid input reached network"))
            self.assertFalse(result["ok"])
            self.assertTrue(result["reason"].startswith("invalid_"))


if __name__ == "__main__":
    unittest.main()
