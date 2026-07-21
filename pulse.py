"""Eve Pulse — ephemeral Xray-based health checks for proxy configs.

Spins up a loopback-only Xray instance for a given config URI
(vless/vmess/trojan/ss), runs health probes through its local SOCKS5
inbound, and always tears the instance down again. Dependency-light on
purpose (stdlib + requests + telegram_xray) so it can run both from the
CLI and from the Flask app later.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional
from urllib.parse import unquote, urlparse

import requests

from telegram_xray import build_xray_config_from_uri, find_xray_binary

DEFAULT_LATENCY_ENDPOINT = "https://cp.cloudflare.com/generate_204"
DEFAULT_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=10000000"

VERDICT_HEALTHY = "healthy"
VERDICT_DEGRADED = "degraded"
VERDICT_DOWN = "down"

# tests that produce no usable signal at all count as 100% loss
_TOTAL_FAILURE_LOSS = 100.0


@dataclass
class SiteCheck:
    """A single custom URL to GET through the proxy."""

    name: str
    url: str
    expect_substring: Optional[str] = None
    timeout: float = 10.0


@dataclass
class PulseConfig:
    """The proxy config under test plus optional display metadata."""

    uri: str
    label: str = ""
    server: str = ""
    inbound: str = ""


@dataclass
class ProbeProfile:
    """Which tests to run, their parameters, and verdict thresholds."""

    run_latency: bool = True
    run_download: bool = True
    run_loss: bool = True
    run_sites: bool = True

    latency_endpoint: str = DEFAULT_LATENCY_ENDPOINT
    latency_attempts: int = 5
    request_timeout: float = 10.0

    download_url: str = DEFAULT_DOWNLOAD_URL
    download_timeout: float = 30.0
    download_chunk_bytes: int = 65536

    loss_requests: int = 10

    site_checks: List[SiteCheck] = field(default_factory=list)

    ready_timeout: float = 10.0
    probe_timeout: float = 120.0

    down_loss_pct: float = 50.0
    down_latency_ms: float = 3000.0
    degraded_loss_pct: float = 10.0
    degraded_latency_ms: float = 800.0


def quick_profile(site_checks: Optional[List[SiteCheck]] = None) -> ProbeProfile:
    """Latency, loss, and site checks only — no large download."""
    return ProbeProfile(run_download=False, site_checks=site_checks or [])


def full_profile(site_checks: Optional[List[SiteCheck]] = None) -> ProbeProfile:
    """Everything, including the download-speed test."""
    return ProbeProfile(site_checks=site_checks or [])


@dataclass
class ProbeResult:
    """Structured outcome of one probe. ``to_dict()`` is JSON-safe."""

    label: str
    scheme: str
    verdict: str = VERDICT_DOWN
    started_at: float = 0.0
    duration_ms: float = 0.0
    socks_port: Optional[int] = None
    error: Optional[str] = None
    tests: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _config_label(config: PulseConfig) -> str:
    if config.label:
        return config.label
    parsed = urlparse((config.uri or "").strip())
    if parsed.fragment:
        return unquote(parsed.fragment)
    return parsed.hostname or config.uri[:32]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _socks_ready(port: int, timeout: float = 1.0) -> bool:
    """True when the local SOCKS5 port answers a no-auth greeting."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as sock:
            sock.sendall(b"\x05\x01\x00")
            return sock.recv(2) == b"\x05\x00"
    except OSError:
        return False


def _wait_for_ready(process, port: int, timeout: float) -> Optional[str]:
    """Poll for the SOCKS handshake; return an error string or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return f"xray exited with status {process.returncode} before SOCKS was ready"
        if _socks_ready(port):
            return None
        time.sleep(0.2)
    return "xray did not open its local SOCKS port in time"


def _terminate_xray(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _default_session_factory(port: int) -> requests.Session:
    proxy = f"socks5h://127.0.0.1:{int(port)}"
    session = requests.Session()
    session.proxies = {"http": proxy, "https": proxy}
    return session


def _run_latency(session, profile: ProbeProfile) -> dict:
    samples = []
    errors = []
    for _ in range(max(1, profile.latency_attempts)):
        start = time.monotonic()
        try:
            response = session.get(profile.latency_endpoint, timeout=profile.request_timeout)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            if response.status_code < 400:
                samples.append(elapsed_ms)
            else:
                errors.append(f"http {response.status_code}")
        except Exception as exc:  # timeouts, proxy failures, TLS errors
            errors.append(str(exc))
    result = {
        "endpoint": profile.latency_endpoint,
        "attempts": max(1, profile.latency_attempts),
        "successes": len(samples),
        "samples_ms": [round(value, 2) for value in samples],
        "errors": errors,
        "avg_ms": None,
        "min_ms": None,
        "max_ms": None,
        "jitter_ms": None,
    }
    if samples:
        result["avg_ms"] = round(statistics.fmean(samples), 2)
        result["min_ms"] = round(min(samples), 2)
        result["max_ms"] = round(max(samples), 2)
        result["jitter_ms"] = round(statistics.stdev(samples), 2) if len(samples) > 1 else 0.0
    return result


def _run_loss(session, profile: ProbeProfile) -> dict:
    successes = 0
    samples = []
    total = max(1, profile.loss_requests)
    for _ in range(total):
        start = time.monotonic()
        try:
            response = session.get(profile.latency_endpoint, timeout=profile.request_timeout)
            if response.status_code < 400:
                successes += 1
                samples.append((time.monotonic() - start) * 1000.0)
        except Exception:
            pass
    loss_pct = round(100.0 * (total - successes) / total, 2)
    return {
        "endpoint": profile.latency_endpoint,
        "requests": total,
        "successes": successes,
        "loss_pct": loss_pct,
        "latency_avg_ms": round(statistics.fmean(samples), 2) if samples else None,
        "latency_jitter_ms": round(statistics.stdev(samples), 2) if len(samples) > 1 else None,
    }


def _run_download(session, profile: ProbeProfile) -> dict:
    result = {
        "url": profile.download_url,
        "bytes": 0,
        "seconds": None,
        "mbps": None,
        "partial": False,
        "error": None,
    }
    start = time.monotonic()
    deadline = start + profile.download_timeout
    try:
        with session.get(profile.download_url, stream=True,
                         timeout=profile.request_timeout) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=profile.download_chunk_bytes):
                if time.monotonic() > deadline:
                    result["partial"] = True
                    result["error"] = "overall download timeout exceeded"
                    break
                if chunk:
                    result["bytes"] += len(chunk)
    except Exception as exc:
        result["partial"] = True
        result["error"] = str(exc)
    elapsed = time.monotonic() - start
    result["seconds"] = round(elapsed, 3)
    if elapsed > 0 and result["bytes"] > 0:
        result["mbps"] = round(result["bytes"] * 8 / elapsed / 1_000_000, 3)
    return result


def _run_sites(session, profile: ProbeProfile) -> dict:
    checks = []
    for site in profile.site_checks:
        entry = {"name": site.name, "url": site.url, "ok": False,
                 "status": None, "latency_ms": None, "error": None}
        start = time.monotonic()
        try:
            response = session.get(site.url, timeout=site.timeout)
            entry["status"] = response.status_code
            entry["latency_ms"] = round((time.monotonic() - start) * 1000.0, 2)
            if response.status_code >= 400:
                entry["error"] = f"http {response.status_code}"
            elif site.expect_substring and site.expect_substring not in response.text:
                entry["error"] = "expected substring not found"
            else:
                entry["ok"] = True
        except Exception as exc:
            entry["error"] = str(exc)
        checks.append(entry)
    return {
        "checks": checks,
        "total": len(checks),
        "passed": sum(1 for entry in checks if entry["ok"]),
    }


def _compute_verdict(profile: ProbeProfile, tests: dict, error: Optional[str]) -> str:
    if error:
        return VERDICT_DOWN
    latency = tests.get("latency")
    loss = tests.get("loss")
    sites = tests.get("sites")
    download = tests.get("download")

    loss_pct = loss.get("loss_pct") if loss else None
    if loss_pct is None and latency and latency.get("successes") == 0:
        loss_pct = _TOTAL_FAILURE_LOSS
    avg_ms = latency.get("avg_ms") if latency else None

    if loss_pct is not None and loss_pct > profile.down_loss_pct:
        return VERDICT_DOWN
    if avg_ms is not None and avg_ms > profile.down_latency_ms:
        return VERDICT_DOWN
    if sites and sites.get("checks") and sites["passed"] == 0:
        return VERDICT_DOWN

    if loss_pct is not None and loss_pct > profile.degraded_loss_pct:
        return VERDICT_DEGRADED
    if avg_ms is not None and avg_ms > profile.degraded_latency_ms:
        return VERDICT_DEGRADED
    if sites and sites.get("checks") and sites["passed"] < sites["total"]:
        return VERDICT_DEGRADED
    if download and download.get("partial"):
        return VERDICT_DEGRADED
    return VERDICT_HEALTHY


def run_probe(
    config: PulseConfig,
    profile: ProbeProfile,
    session_factory: Optional[Callable[[int], object]] = None,
) -> ProbeResult:
    """Probe one config end to end: xray up, tests, xray down (always)."""
    started_at = time.time()
    start = time.monotonic()
    result = ProbeResult(
        label=_config_label(config),
        scheme=urlparse((config.uri or "").strip()).scheme.lower(),
        started_at=round(started_at, 3),
    )

    xray_bin = find_xray_binary()
    if not xray_bin:
        result.error = "xray runtime was not found; configure XRAY_BIN"
        result.duration_ms = round((time.monotonic() - start) * 1000.0, 2)
        result.verdict = _compute_verdict(profile, result.tests, result.error)
        return result

    port = _free_port()
    result.socks_port = port
    try:
        xray_config = build_xray_config_from_uri(config.uri, port)
    except ValueError as exc:
        result.error = str(exc)
        result.duration_ms = round((time.monotonic() - start) * 1000.0, 2)
        result.verdict = _compute_verdict(profile, result.tests, result.error)
        return result

    config_path = None
    process = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="pulse-", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(xray_config, handle)
            config_path = handle.name
        process = subprocess.Popen(
            [xray_bin, "run", "-config", config_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, shell=False,
        )
        ready_error = _wait_for_ready(process, port, profile.ready_timeout)
        if ready_error:
            result.error = ready_error
            return result

        session = (session_factory or _default_session_factory)(port)
        deadline = start + profile.probe_timeout
        phases = []
        if profile.run_latency:
            phases.append(("latency", _run_latency))
        if profile.run_loss:
            phases.append(("loss", _run_loss))
        if profile.run_download:
            phases.append(("download", _run_download))
        if profile.run_sites and profile.site_checks:
            phases.append(("sites", _run_sites))
        for name, runner in phases:
            if time.monotonic() > deadline:
                result.tests[name] = {"skipped": "overall probe timeout exceeded"}
                continue
            result.tests[name] = runner(session, profile)
    except Exception as exc:
        result.error = f"probe failed: {exc}"
    finally:
        if process is not None:
            _terminate_xray(process)
        if config_path:
            try:
                os.unlink(config_path)
            except OSError:
                pass

    result.duration_ms = round((time.monotonic() - start) * 1000.0, 2)
    result.verdict = _compute_verdict(profile, result.tests, result.error)
    return result


def _parse_site(value: str) -> SiteCheck:
    """Parse ``name=url[::expect]`` from the CLI into a SiteCheck."""
    expect = None
    if "::" in value:
        value, expect = value.rsplit("::", 1)
    name, sep, url = value.partition("=")
    if not sep or not name or not url:
        raise argparse.ArgumentTypeError(
            f"invalid --site {value!r}; expected name=url[::expect]"
        )
    return SiteCheck(name=name, url=url, expect_substring=expect or None)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Eve Pulse config health check")
    parser.add_argument("--config", required=True, help="proxy config URI (vless/vmess/trojan/ss)")
    parser.add_argument("--label", default="", help="display label for the config")
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    parser.add_argument("--site", action="append", type=_parse_site, default=[],
                        metavar="name=url[::expect]", help="custom site check (repeatable)")
    args = parser.parse_args(argv)

    profile_factory = full_profile if args.profile == "full" else quick_profile
    profile = profile_factory(site_checks=args.site)
    result = run_probe(PulseConfig(uri=args.config, label=args.label), profile)
    json.dump(result.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result.verdict != VERDICT_DOWN else 1


if __name__ == "__main__":
    raise SystemExit(main())
