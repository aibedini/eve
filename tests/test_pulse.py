"""Unit tests for pulse.py — no real xray, no real network."""

import io
import itertools
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pulse  # noqa: E402

VLESS_URI = "vless://11111111-2222-3333-4444-555555555555@example.com:443?security=none&type=tcp#demo"


class FakeProcess:
    """Stands in for the xray subprocess."""

    def __init__(self, wait_behaviour=None):
        self.terminated = False
        self.killed = False
        self.returncode = None
        self._wait_behaviour = list(wait_behaviour or [0])

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        behaviour = self._wait_behaviour.pop(0) if self._wait_behaviour else 0
        if isinstance(behaviour, Exception):
            raise behaviour
        self.returncode = behaviour
        return behaviour


class FakeResponse:
    def __init__(self, status_code=204, text="", chunks=None):
        self.status_code = status_code
        self.text = text
        self._chunks = chunks or []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"http {self.status_code}")

    def iter_content(self, chunk_size=1):
        return iter(self._chunks)


class FakeSession:
    """Scripted stand-in for requests.Session."""

    def __init__(self, responses=None, default=None):
        self._responses = list(responses or [])
        self._default = default if default is not None else FakeResponse()
        self.calls = []
        self.uploaded_bytes = 0

    def _next(self):
        item = self._responses.pop(0) if self._responses else self._default
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self._next()

    def post(self, url, data=None, **kwargs):
        self.calls.append(url)
        response = self._next()
        if data is not None:
            for chunk in data:  # stream the body like requests would
                self.uploaded_bytes += len(chunk)
        return response


def _probe_mocks(process):
    """Patch binary lookup, Popen, and the SOCKS handshake probe."""
    return (
        mock.patch("pulse.find_xray_binary", return_value="/fake/xray"),
        mock.patch("pulse.subprocess.Popen", return_value=process),
        mock.patch("pulse._socks_ready", return_value=True),
    )


class RunProbeLifecycleTest(unittest.TestCase):
    def test_success_starts_and_tears_down_xray(self):
        process = FakeProcess()
        patch_bin, patch_popen, patch_ready = _probe_mocks(process)
        session = FakeSession()
        with patch_bin, patch_popen as popen, patch_ready:
            profile = pulse.quick_profile(
                site_checks=[pulse.SiteCheck(name="ex", url="https://example.com")]
            )
            result = pulse.run_probe(pulse.PulseConfig(uri=VLESS_URI), profile,
                                     session_factory=lambda port: session)
        self.assertEqual(popen.call_count, 1)
        args = popen.call_args[0][0]
        self.assertEqual(args[:2], ["/fake/xray", "run"])
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertEqual(result.verdict, pulse.VERDICT_HEALTHY)
        self.assertIn("latency", result.tests)
        self.assertIn("loss", result.tests)
        self.assertIn("sites", result.tests)
        self.assertNotIn("download", result.tests)
        self.assertNotIn("upload", result.tests)
        self.assertEqual(result.label, "demo")
        self.assertEqual(result.scheme, "vless")

    def test_full_profile_runs_download(self):
        process = FakeProcess()
        patch_bin, patch_popen, patch_ready = _probe_mocks(process)
        download = FakeResponse(status_code=200, chunks=[b"x" * 1000, b"y" * 1000])
        session = FakeSession(default=FakeResponse())
        original_get = session.get

        def get(url, **kwargs):
            if "__down" in url:
                return download
            return original_get(url, **kwargs)

        session.get = get
        with patch_bin, patch_popen, patch_ready:
            result = pulse.run_probe(pulse.PulseConfig(uri=VLESS_URI),
                                     pulse.full_profile(),
                                     session_factory=lambda port: session)
        self.assertTrue(process.terminated)
        self.assertEqual(result.tests["download"]["bytes"], 2000)
        self.assertFalse(result.tests["download"]["partial"])
        # full profile also runs the upload test through the same session
        self.assertIn("upload", result.tests)
        self.assertEqual(result.tests["upload"]["bytes"], 2_000_000)
        self.assertFalse(result.tests["upload"]["partial"])
        self.assertEqual(session.uploaded_bytes, 2_000_000)

    def test_teardown_on_unexpected_exception(self):
        process = FakeProcess()
        patch_bin, patch_popen, patch_ready = _probe_mocks(process)
        with patch_bin, patch_popen, patch_ready, \
                mock.patch("pulse._run_latency", side_effect=RuntimeError("boom")):
            result = pulse.run_probe(pulse.PulseConfig(uri=VLESS_URI),
                                     pulse.quick_profile(),
                                     session_factory=lambda port: FakeSession())
        self.assertTrue(process.terminated)
        self.assertEqual(result.verdict, pulse.VERDICT_DOWN)
        self.assertIn("boom", result.error)

    def test_kill_when_terminate_stalls(self):
        process = FakeProcess(
            wait_behaviour=[subprocess.TimeoutExpired("xray", 5), 0]
        )
        pulse._terminate_xray(process)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)

    def test_missing_xray_binary(self):
        with mock.patch("pulse.find_xray_binary", return_value=None), \
                mock.patch("pulse.subprocess.Popen") as popen:
            result = pulse.run_probe(pulse.PulseConfig(uri=VLESS_URI), pulse.quick_profile())
        self.assertEqual(result.verdict, pulse.VERDICT_DOWN)
        self.assertIn("xray runtime", result.error)
        popen.assert_not_called()

    def test_invalid_uri_never_spawns_xray(self):
        with mock.patch("pulse.find_xray_binary", return_value="/fake/xray"), \
                mock.patch("pulse.subprocess.Popen") as popen:
            result = pulse.run_probe(pulse.PulseConfig(uri="http://nope"), pulse.quick_profile())
        self.assertEqual(result.verdict, pulse.VERDICT_DOWN)
        self.assertIsNotNone(result.error)
        popen.assert_not_called()

    def test_ready_timeout_tears_down(self):
        process = FakeProcess()
        patch_bin, patch_popen, _ = _probe_mocks(process)
        with patch_bin, patch_popen, \
                mock.patch("pulse._wait_for_ready", return_value="start_timeout"):
            result = pulse.run_probe(pulse.PulseConfig(uri=VLESS_URI),
                                     pulse.quick_profile(),
                                     session_factory=lambda port: FakeSession())
        self.assertTrue(process.terminated)
        self.assertEqual(result.verdict, pulse.VERDICT_DOWN)
        self.assertEqual(result.error, "start_timeout")


class LatencyStatsTest(unittest.TestCase):
    def test_avg_min_max_jitter(self):
        profile = pulse.ProbeProfile(latency_attempts=3)
        clock = itertools.chain([0.0, 0.1, 0.1, 0.3, 0.3, 0.6])
        with mock.patch("pulse.time.monotonic", side_effect=lambda: next(clock)):
            result = pulse._run_latency(FakeSession(), profile)
        self.assertEqual(result["successes"], 3)
        self.assertEqual(result["samples_ms"], [100.0, 200.0, 300.0])
        self.assertEqual(result["avg_ms"], 200.0)
        self.assertEqual(result["min_ms"], 100.0)
        self.assertEqual(result["max_ms"], 300.0)
        self.assertAlmostEqual(result["jitter_ms"], 100.0)

    def test_single_sample_zero_jitter(self):
        profile = pulse.ProbeProfile(latency_attempts=1)
        clock = itertools.chain([0.0, 0.05])
        with mock.patch("pulse.time.monotonic", side_effect=lambda: next(clock)):
            result = pulse._run_latency(FakeSession(), profile)
        self.assertEqual(result["jitter_ms"], 0.0)

    def test_failures_recorded(self):
        profile = pulse.ProbeProfile(latency_attempts=2)
        session = FakeSession(responses=[requests.ConnectionError("refused"),
                                         FakeResponse(status_code=500)])
        result = pulse._run_latency(session, profile)
        self.assertEqual(result["successes"], 0)
        self.assertEqual(len(result["errors"]), 2)
        self.assertIsNone(result["avg_ms"])


class LossTest(unittest.TestCase):
    def test_loss_percentage(self):
        profile = pulse.ProbeProfile(loss_requests=4)
        session = FakeSession(responses=[
            FakeResponse(), FakeResponse(),
            requests.ConnectionError("drop"), FakeResponse(),
        ])
        with mock.patch("pulse.time.monotonic",
                        side_effect=itertools.count(0, 0.05).__next__):
            result = pulse._run_loss(session, profile)
        self.assertEqual(result["requests"], 4)
        self.assertEqual(result["successes"], 3)
        self.assertEqual(result["loss_pct"], 25.0)
        self.assertEqual(result["latency_avg_ms"], 50.0)

    def test_total_loss(self):
        profile = pulse.ProbeProfile(loss_requests=3)
        session = FakeSession(responses=[requests.Timeout()] * 3)
        result = pulse._run_loss(session, profile)
        self.assertEqual(result["loss_pct"], 100.0)
        self.assertIsNone(result["latency_avg_ms"])


class DownloadTest(unittest.TestCase):
    def test_mbps_math(self):
        profile = pulse.ProbeProfile()
        chunks = [b"x" * 500_000, b"y" * 500_000]
        session = FakeSession(responses=[FakeResponse(status_code=200, chunks=chunks)])
        with mock.patch("pulse.time.monotonic",
                        side_effect=itertools.count(0, 0.5).__next__):
            result = pulse._run_download(session, profile)
        self.assertEqual(result["bytes"], 1_000_000)
        self.assertEqual(result["seconds"], 1.5)
        self.assertAlmostEqual(result["mbps"], 1_000_000 * 8 / 1.5 / 1_000_000, places=3)
        self.assertFalse(result["partial"])

    def test_overall_timeout_marks_partial(self):
        profile = pulse.ProbeProfile(download_timeout=30.0)
        session = FakeSession(responses=[FakeResponse(status_code=200, chunks=[b"z" * 100])])
        with mock.patch("pulse.time.monotonic",
                        side_effect=itertools.count(0, 100.0).__next__):
            result = pulse._run_download(session, profile)
        self.assertTrue(result["partial"])
        self.assertIn("timeout", result["error"])

    def test_request_failure_marks_partial(self):
        profile = pulse.ProbeProfile()
        session = FakeSession(responses=[requests.ConnectionError("stalled")])
        result = pulse._run_download(session, profile)
        self.assertTrue(result["partial"])
        self.assertEqual(result["bytes"], 0)
        self.assertIn("stalled", result["error"])


class UploadTest(unittest.TestCase):
    def test_mbps_math(self):
        profile = pulse.ProbeProfile(upload_bytes=1_000_000,
                                     upload_chunk_bytes=500_000)
        session = FakeSession(responses=[FakeResponse(status_code=200)])
        with mock.patch("pulse.time.monotonic",
                        side_effect=itertools.count(0, 0.5).__next__):
            result = pulse._run_upload(session, profile)
        self.assertEqual(result["bytes"], 1_000_000)
        self.assertEqual(session.uploaded_bytes, 1_000_000)
        self.assertEqual(result["seconds"], 1.5)
        self.assertAlmostEqual(result["mbps"], 1_000_000 * 8 / 1.5 / 1_000_000, places=3)
        self.assertFalse(result["partial"])

    def test_overall_timeout_marks_partial(self):
        profile = pulse.ProbeProfile(upload_timeout=30.0)
        session = FakeSession(responses=[FakeResponse(status_code=200)])
        with mock.patch("pulse.time.monotonic",
                        side_effect=itertools.count(0, 100.0).__next__):
            result = pulse._run_upload(session, profile)
        self.assertTrue(result["partial"])
        self.assertIn("timeout", result["error"])

    def test_request_failure_marks_partial(self):
        profile = pulse.ProbeProfile()
        session = FakeSession(responses=[requests.ConnectionError("reset")])
        result = pulse._run_upload(session, profile)
        self.assertTrue(result["partial"])
        self.assertEqual(result["bytes"], 0)
        self.assertIn("reset", result["error"])

    def test_http_error_marks_partial(self):
        profile = pulse.ProbeProfile(upload_bytes=1000)
        session = FakeSession(responses=[FakeResponse(status_code=503)])
        result = pulse._run_upload(session, profile)
        self.assertTrue(result["partial"])
        self.assertIn("503", result["error"])


class SiteCheckTest(unittest.TestCase):
    def test_pass_fail_and_substring(self):
        profile = pulse.ProbeProfile(site_checks=[
            pulse.SiteCheck(name="ok", url="https://a.example",
                            expect_substring="Welcome"),
            pulse.SiteCheck(name="missing", url="https://b.example",
                            expect_substring="Nope"),
            pulse.SiteCheck(name="down", url="https://c.example"),
            pulse.SiteCheck(name="err", url="https://d.example"),
        ])
        session = FakeSession(responses=[
            FakeResponse(status_code=200, text="Welcome aboard"),
            FakeResponse(status_code=200, text="Hello there"),
            FakeResponse(status_code=503, text="oops"),
            requests.ConnectionError("unreachable"),
        ])
        result = pulse._run_sites(session, profile)
        checks = {entry["name"]: entry for entry in result["checks"]}
        self.assertTrue(checks["ok"]["ok"])
        self.assertFalse(checks["missing"]["ok"])
        self.assertIn("substring", checks["missing"]["error"])
        self.assertFalse(checks["down"]["ok"])
        self.assertEqual(checks["down"]["status"], 503)
        self.assertFalse(checks["err"]["ok"])
        self.assertEqual(result["passed"], 1)
        self.assertEqual(result["total"], 4)


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.profile = pulse.ProbeProfile()

    def verdict(self, tests, error=None):
        return pulse._compute_verdict(self.profile, tests, error)

    def test_error_is_down(self):
        self.assertEqual(self.verdict({}, error="xray died"), pulse.VERDICT_DOWN)

    def test_healthy(self):
        tests = {"latency": {"successes": 5, "avg_ms": 200.0},
                 "loss": {"loss_pct": 0.0}}
        self.assertEqual(self.verdict(tests), pulse.VERDICT_HEALTHY)

    def test_down_loss_boundary(self):
        at = {"loss": {"loss_pct": 50.0}, "latency": {"successes": 1, "avg_ms": 100.0}}
        over = {"loss": {"loss_pct": 50.01}, "latency": {"successes": 1, "avg_ms": 100.0}}
        self.assertNotEqual(self.verdict(at), pulse.VERDICT_DOWN)
        self.assertEqual(self.verdict(over), pulse.VERDICT_DOWN)

    def test_down_latency_boundary(self):
        at = {"latency": {"successes": 1, "avg_ms": 3000.0}}
        over = {"latency": {"successes": 1, "avg_ms": 3000.01}}
        self.assertEqual(self.verdict(at), pulse.VERDICT_DEGRADED)
        self.assertEqual(self.verdict(over), pulse.VERDICT_DOWN)

    def test_degraded_boundaries(self):
        ok = {"latency": {"successes": 1, "avg_ms": 800.0}, "loss": {"loss_pct": 10.0}}
        slow = {"latency": {"successes": 1, "avg_ms": 800.01}}
        lossy = {"loss": {"loss_pct": 10.01}}
        self.assertEqual(self.verdict(ok), pulse.VERDICT_HEALTHY)
        self.assertEqual(self.verdict(slow), pulse.VERDICT_DEGRADED)
        self.assertEqual(self.verdict(lossy), pulse.VERDICT_DEGRADED)

    def test_all_sites_fail_is_down_partial_is_degraded(self):
        all_fail = {"sites": {"total": 2, "passed": 0,
                              "checks": [{"ok": False}, {"ok": False}]}}
        some_fail = {"sites": {"total": 2, "passed": 1,
                               "checks": [{"ok": True}, {"ok": False}]}}
        self.assertEqual(self.verdict(all_fail), pulse.VERDICT_DOWN)
        self.assertEqual(self.verdict(some_fail), pulse.VERDICT_DEGRADED)

    def test_total_latency_failure_without_loss_test_is_down(self):
        tests = {"latency": {"successes": 0, "avg_ms": None}}
        self.assertEqual(self.verdict(tests), pulse.VERDICT_DOWN)

    def test_partial_download_is_degraded(self):
        tests = {"download": {"partial": True}}
        self.assertEqual(self.verdict(tests), pulse.VERDICT_DEGRADED)


class ResultSerializationTest(unittest.TestCase):
    def test_to_dict_json_round_trip(self):
        process = FakeProcess()
        patch_bin, patch_popen, patch_ready = _probe_mocks(process)
        with patch_bin, patch_popen, patch_ready:
            result = pulse.run_probe(pulse.PulseConfig(uri=VLESS_URI, label="L"),
                                     pulse.quick_profile(),
                                     session_factory=lambda port: FakeSession())
        payload = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(payload["label"], "L")
        self.assertEqual(payload["verdict"], pulse.VERDICT_HEALTHY)
        self.assertEqual(payload["scheme"], "vless")
        self.assertIsInstance(payload["tests"]["latency"]["samples_ms"], list)
        self.assertIsInstance(payload["socks_port"], int)


class CliTest(unittest.TestCase):
    def test_parse_site(self):
        site = pulse._parse_site("example=https://example.com::Welcome")
        self.assertEqual(site.name, "example")
        self.assertEqual(site.url, "https://example.com")
        self.assertEqual(site.expect_substring, "Welcome")
        site = pulse._parse_site("plain=https://example.com/a?x=1")
        self.assertEqual(site.url, "https://example.com/a?x=1")
        self.assertIsNone(site.expect_substring)
        with self.assertRaises(Exception):
            pulse._parse_site("no-equals-sign")

    def test_main_prints_json(self):
        canned = pulse.ProbeResult(label="L", scheme="vless",
                                   verdict=pulse.VERDICT_HEALTHY)
        buffer = io.StringIO()
        with mock.patch("pulse.run_probe", return_value=canned) as run, \
                redirect_stdout(buffer):
            code = pulse.main(["--config", VLESS_URI, "--profile", "quick",
                               "--site", "ex=https://example.com"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["verdict"], "healthy")
        profile = run.call_args[0][1]
        self.assertFalse(profile.run_download)
        self.assertFalse(profile.run_upload)
        self.assertEqual(profile.site_checks[0].name, "ex")

    def test_main_down_exit_code(self):
        canned = pulse.ProbeResult(label="L", scheme="vless",
                                   verdict=pulse.VERDICT_DOWN, error="dead")
        with mock.patch("pulse.run_probe", return_value=canned), \
                redirect_stdout(io.StringIO()):
            code = pulse.main(["--config", VLESS_URI, "--profile", "full"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
