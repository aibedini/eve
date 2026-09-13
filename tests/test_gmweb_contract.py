"""Phase 28 tests: the GMweb consumer contract, checked behaviourally.

The previous contract test grepped messaging.py for path fragments. It could not
catch a wrong method, a missing header, a dropped idempotency key or a response
that is not parsed. These tests run the real client functions against a local
fake gateway and assert the wire contract, and they fail if an endpoint path is
hardcoded again instead of coming from shared/eve-gmweb-contract-v1.json.
"""
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from panel.jobs.messaging import (  # noqa: E402
    _cancel_sms_via_gmweb,
    _get_gmweb_send_capacity,
    _send_sms_via_gmweb,
    _sms_status_endpoint,
)
from panel.services import gmweb_contract  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "shared" / "eve-gmweb-contract-v1.json"


class FakeGateway:
    """A local HTTP server that records requests and replays a script."""

    def __init__(self, script=None):
        self.requests = []
        self.script = list(script or [])
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "body": body,
                })
                status, headers, payload = (outer.script.pop(0) if outer.script
                                            else (200, {}, b"{}"))
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.port

    def config(self, api_key="gw-secret"):
        return {"provider": "gmweb", "base_url": self.base_url,
                "api_key": api_key, "timeout_seconds": 5}

    def close(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass


class ContractFileTests(unittest.TestCase):
    def test_the_lifecycle_metric_names_are_declared(self):
        """The gateway publishes these; EVE's dashboards read them by exact name."""
        metrics = gmweb_contract.load_contract()["lifecycleMetrics"]
        self.assertIn("sms_invalidations_total", metrics)
        self.assertIn("sms_stale_generation_rejections_total", metrics)
        self.assertIn("sms_sent_after_revocation_total", metrics)

    def test_the_contract_declares_the_version_scopes_and_endpoints(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["consumer"], "eve")
        # v3 adds the Android-bridge block and the lifecycle metric names on top
        # of the v2 invalidation surface; the consumer reads paths from the file,
        # so the only thing that must move is this expectation.
        self.assertEqual(contract["version"], 3)
        self.assertEqual(contract["projectKeyDefaults"]["scopes"],
                         ["sms.send", "sms.status", "sms.cancel", "sms.capacity",
                          "sms.invalidate"])
        keys = {entry["key"] for entry in contract["endpoints"]}
        self.assertEqual(keys, {"ready", "send", "send_status", "send_cancel",
                                "send_capacity", "post_invalidate"})
        for entry in contract["endpoints"]:
            self.assertIn(entry["scope"], contract["projectKeyDefaults"]["scopes"])
        self.assertEqual(contract["transport"]["idempotencyHeader"], "Idempotency-Key")
        self.assertEqual(contract["sendRequest"]["priorityLanes"],
                         {"critical": 1, "expired": 3, "expiring": 6,
                          "announcement": 10})
        self.assertEqual(contract["errorResponse"]["rateLimitStatus"], 429)

    def test_the_consumer_reads_the_paths_from_the_contract(self):
        source = (ROOT / "panel" / "jobs" / "messaging.py").read_text(encoding="utf-8")
        self.assertIn("gmweb_contract.endpoint_path(", source)
        for literal in ('"/send/capacity"', "'/send/capacity'", '"/send"',
                        "'/send'", "'/send/status/'", '"/send/cancel/"'):
            self.assertNotIn(literal, source, literal)

    def test_declared_scopes_are_exposed_by_the_module(self):
        self.assertEqual(gmweb_contract.contract_version(), 3)
        self.assertEqual(gmweb_contract.declared_scopes(),
                         ["sms.send", "sms.status", "sms.cancel", "sms.capacity",
                          "sms.invalidate"])

    def test_the_invalidation_endpoint_is_declared_with_its_scope(self):
        entry = next(item for item in gmweb_contract.load_contract()["endpoints"]
                     if item["key"] == "post_invalidate")
        self.assertEqual(entry["method"], "POST")
        self.assertEqual(entry["path"], "/send/invalidate")
        self.assertEqual(entry["scope"], "sms.invalidate")
        self.assertEqual(gmweb_contract.endpoint_path("post_invalidate"),
                         "/send/invalidate")

    def test_the_send_payload_meta_block_is_declared(self):
        meta = gmweb_contract.load_contract()["sendRequest"]["meta"]
        self.assertEqual(
            set(meta["fields"]),
            {"source", "serviceKey", "notificationKind", "generation",
             "correlationId", "requiresValidation"})
        self.assertEqual(meta["invalidationEligibleKinds"],
                         ["near_expiry", "low_volume", "expired", "volume_ended"])

    def test_the_invalidation_contract_declares_idempotency_and_its_counts(self):
        request = gmweb_contract.load_contract()["invalidationRequest"]
        self.assertEqual(request["required"], ["source", "serviceKey"])
        response = gmweb_contract.load_contract()["invalidationResponse"]
        self.assertEqual(response["example"]["currentGeneration"], 18)
        self.assertEqual(
            set(response["fields"]),
            {"ok", "currentGeneration", "cancelledPending", "revokedActive",
             "revokedInflight", "alreadyTerminal", "matched", "replayed"})
        self.assertIn("eventId", request["fields"])


class BaseUrlTests(unittest.TestCase):
    def test_https_is_accepted_without_a_warning(self):
        result = gmweb_contract.validate_base_url("https://gw.example.com/api/")
        self.assertEqual(result["base"], "https://gw.example.com/api")
        self.assertIsNone(result["reason"])
        self.assertIsNone(result["warning"])

    def test_plaintext_to_a_remote_host_warns_but_is_allowed(self):
        result = gmweb_contract.validate_base_url("http://gw.example.com")
        self.assertIsNone(result["reason"])
        self.assertIn("plaintext", result["warning"])

    def test_plaintext_to_a_local_or_private_host_is_silent(self):
        for url in ("http://127.0.0.1:9000", "http://localhost:9000",
                    "http://10.1.2.3:9000", "http://192.168.1.10",
                    "http://172.16.5.4", "http://gw.internal"):
            result = gmweb_contract.validate_base_url(url)
            self.assertIsNone(result["reason"], url)
            self.assertIsNone(result["warning"], url)

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "gopher://host", "ftp://host/gw",
                    "javascript:alert(1)"):
            result = gmweb_contract.validate_base_url(url)
            self.assertIsNone(result["base"], url)
            self.assertTrue(result["reason"].startswith("invalid_gateway_scheme"),
                            (url, result))

    def test_credentials_and_malformed_urls_are_refused(self):
        self.assertEqual(
            gmweb_contract.validate_base_url("https://user:pass@gw.example.com")["reason"],
            "gateway_userinfo_not_allowed")
        self.assertEqual(gmweb_contract.validate_base_url("https://")["reason"],
                         "invalid_gateway_url")
        self.assertEqual(gmweb_contract.validate_base_url("https://gw.example.com/?x=1")["reason"],
                         "invalid_gateway_url")
        self.assertEqual(gmweb_contract.validate_base_url("")["reason"],
                         "gateway_not_configured")

    def test_headers_are_built_from_the_contract(self):
        headers = gmweb_contract.request_headers("k", json_body=True,
                                                 idempotency_key="📶-key")
        self.assertEqual(headers["Authorization"], "Bearer k")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertTrue(headers["Idempotency-Key"])
        headers["Idempotency-Key"].encode("latin-1")

    def test_unknown_endpoint_key_raises(self):
        with self.assertRaises(KeyError):
            gmweb_contract.endpoint_path("does_not_exist")


class GatewayBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.addCleanup(self.gateway.close)

    def test_send_matches_the_declared_request(self):
        self.gateway.script = [(202, {}, {"requestId": "req-1", "status": "sent"})]
        result = _send_sms_via_gmweb("09120000000", "hello", cfg=self.gateway.config(),
                                     priority="announcement",
                                     idempotency_key="idem-1")
        self.assertTrue(result["accepted"], result)
        self.assertTrue(result["sent"], result)
        self.assertEqual(result["request_id"], "req-1")
        self.assertEqual(result["status_url"], "/send/status/req-1")
        self.assertEqual(len(self.gateway.requests), 1)
        request = self.gateway.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/send")
        self.assertEqual(request["headers"]["authorization"], "Bearer gw-secret")
        self.assertEqual(request["headers"]["content-type"], "application/json")
        self.assertEqual(request["headers"]["idempotency-key"], "idem-1")
        body = json.loads(request["body"].decode("utf-8"))
        self.assertEqual(body, {"to": "09120000000", "text": "hello",
                                "priority": "announcement"})

    def test_rate_limit_is_reported_with_retry_after(self):
        self.gateway.script = [(429, {"Retry-After": "7"},
                                {"error": "rate_limited"})]
        result = _send_sms_via_gmweb("09120000000", "hi", cfg=self.gateway.config(),
                                     priority="critical", idempotency_key="idem-2")
        self.assertFalse(result["sent"])
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["retry_after_seconds"], 7)
        self.assertIn("rate_limited", result["reason"])

    def test_a_5xx_is_retried_once_with_the_same_idempotency_key(self):
        self.gateway.script = [
            (500, {}, {"error": "temporary"}),
            (200, {}, {"requestId": "req-2", "status": "queued"}),
        ]
        result = _send_sms_via_gmweb("09120000000", "retry me",
                                     cfg=self.gateway.config(), priority="expiring",
                                     idempotency_key="stable-key")
        self.assertEqual(len(self.gateway.requests), 2, self.gateway.requests)
        self.assertEqual(
            self.gateway.requests[0]["headers"]["idempotency-key"], "stable-key")
        self.assertEqual(
            self.gateway.requests[1]["headers"]["idempotency-key"], "stable-key")
        self.assertTrue(result["request_id"] == "req-2", result)

    def test_without_an_idempotency_key_there_is_no_retry(self):
        self.gateway.script = [(500, {}, {"error": "temporary"})]
        _send_sms_via_gmweb("09120000000", "no retry", cfg=self.gateway.config(),
                            priority="expired")
        self.assertEqual(len(self.gateway.requests), 1)

    def test_capacity_response_is_validated(self):
        self.gateway.script = [(200, {}, {
            "priorities": {"critical": 1, "expired": 2},
            "announcement": {"limit": 100, "pending": 10, "available": 90,
                             "recommendedBatchSize": 25},
        })]
        result = _get_gmweb_send_capacity(cfg=self.gateway.config())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["announcement"]["available"], 90)
        self.assertEqual(result["announcement"]["recommended_batch_size"], 25)
        self.assertEqual(self.gateway.requests[0]["path"], "/send/capacity")

        self.gateway.script = [(200, {}, ["not", "a", "dict"])]
        broken = _get_gmweb_send_capacity(cfg=self.gateway.config())
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["reason"], "invalid_capacity_response")

    def test_cancel_uses_the_declared_path_with_a_quoted_reference(self):
        self.gateway.script = [(200, {}, {"ok": True, "status": "cancelled"})]
        result = _cancel_sms_via_gmweb("req/1 2", cfg=self.gateway.config())
        self.assertTrue(result["cancelled"], result)
        self.assertEqual(self.gateway.requests[0]["method"], "POST")
        self.assertEqual(self.gateway.requests[0]["path"], "/send/cancel/req%2F1%202")

    def test_an_invalid_base_url_never_reaches_the_gateway(self):
        cfg = {"provider": "gmweb", "base_url": "file:///etc/passwd",
               "api_key": "gw-secret", "timeout_seconds": 5}
        result = _send_sms_via_gmweb("09120000000", "hi", cfg=cfg,
                                     priority="critical")
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "invalid_gateway_scheme:file")
        self.assertEqual(self.gateway.requests, [])

    def test_status_url_from_a_foreign_origin_is_not_followed(self):
        class Row:
            status_url = "https://evil.example.com/send/status/x"
            request_id = "req-9"

        endpoint = _sms_status_endpoint(self.gateway.base_url, Row())
        self.assertEqual(endpoint,
                         "%s/send/status/req-9" % self.gateway.base_url)

    def test_status_url_on_the_gateway_origin_is_followed(self):
        class Row:
            request_id = "req-9"

        Row.status_url = "%s/send/status/req-9" % self.gateway.base_url
        self.assertEqual(_sms_status_endpoint(self.gateway.base_url, Row()),
                         Row.status_url)


if __name__ == "__main__":
    unittest.main()
