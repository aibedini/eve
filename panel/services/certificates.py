"""TLS certificate monitoring for the panel and its panel endpoints.

Two sources are covered:

* the certificate file the reverse proxy serves the panel with (parsed from
  disk, so expiry is reported even for a self-signed or already-expired cert);
* every configured X-UI endpoint that uses https, probed with a real handshake
  against the platform trust store (or a configured CA bundle).

Verification is never disabled here either: an endpoint whose certificate does
not validate is reported as a verification failure, not silently downgraded to
an unverified handshake. This keeps the guarantee enforced by
tests/test_network_hardening.py true.

Thresholds and cadence come from the environment:

* EVE_CERT_WARN_DAYS (default 21) - warn below this many days;
* EVE_CERT_CRIT_DAYS (default 7) - critical below this many days;
* EVE_CERT_CHECK_INTERVAL_SECONDS (default 21600 = 6 h) - cache lifetime.
"""
import logging
import os
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_WARN_DAYS = 21
DEFAULT_CRIT_DAYS = 7
DEFAULT_CHECK_INTERVAL_SECONDS = 21600

STATES = ("ok", "warning", "critical", "expired", "error", "unknown")

_cache_lock = threading.Lock()
_cache = {"at": 0.0, "report": None}


def _env_int(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


def warn_days() -> int:
    return max(1, _env_int("EVE_CERT_WARN_DAYS", DEFAULT_WARN_DAYS))


def critical_days() -> int:
    return max(1, _env_int("EVE_CERT_CRIT_DAYS", DEFAULT_CRIT_DAYS))


def check_interval_seconds() -> int:
    return max(60, _env_int("EVE_CERT_CHECK_INTERVAL_SECONDS", DEFAULT_CHECK_INTERVAL_SECONDS))


def _as_utc(value):
    """Normalise a cryptography datetime to naive UTC.

    cryptography >= 42 returns timezone-aware values; the rest of this module
    (and datetime.utcnow) works in naive UTC, so an aware value is converted to
    UTC explicitly - not to the server's local time.
    """
    if value is None:
        return None
    try:
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        pass
    return value


def days_remaining(not_after, now=None):
    if not_after is None:
        return None
    moment = now or datetime.utcnow()
    try:
        return int((not_after - moment).total_seconds() // 86400)
    except Exception:
        return None


def certificate_state(days, *, warn=None, crit=None):
    """Classify a certificate by days remaining."""
    if days is None:
        return "unknown"
    if days < 0:
        return "expired"
    if days < (crit if crit is not None else critical_days()):
        return "critical"
    if days < (warn if warn is not None else warn_days()):
        return "warning"
    return "ok"


def _name_to_str(name) -> str:
    try:
        return name.rfc4514_string()
    except Exception:
        return ""


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def describe_certificate(cert) -> dict:
    """Extract display-safe fields; never touches the private key."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtensionOID, NameOID

    not_after = _as_utc(getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after)
    not_before = _as_utc(getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before)
    sans = []
    try:
        extension = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        sans = [str(value) for value in extension.value.get_values_for_type(x509.DNSName)]
    except Exception:
        sans = []
    try:
        fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    except Exception:
        fingerprint = None
    issuer_cn = ""
    subject_cn = ""
    try:
        issuer_cn = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except Exception:
        pass
    try:
        subject_cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except Exception:
        pass
    return {
        "subject": _name_to_str(cert.subject),
        "issuer": _name_to_str(cert.issuer),
        "subject_cn": subject_cn,
        "issuer_cn": issuer_cn,
        "not_before": not_before.isoformat() if not_before else None,
        "not_after": not_after.isoformat() if not_after else None,
        "serial": format(cert.serial_number, "x"),
        "sans": sans,
        "fingerprint_sha256": fingerprint,
        "self_signed": cert.issuer == cert.subject,
    }


def inspect_certificate_file(path, *, now=None) -> dict:
    """Read and classify a PEM certificate on disk."""
    report = {
        "source": "file",
        "path": path or None,
        "present": False,
        "readable": False,
        "state": "unknown",
        "error_code": None,
        "error": None,
        "days_remaining": None,
    }
    if not path:
        report["error_code"] = "not_configured"
        return report
    if not os.path.isfile(path):
        report["error_code"] = "missing"
        report["error"] = "certificate file not found"
        return report
    report["present"] = True
    try:
        with open(path, "rb") as handle:
            pem = handle.read()
    except OSError as exc:
        report["error_code"] = "unreadable"
        report["error"] = exc.strerror or "cannot read certificate file"
        return report
    report["readable"] = True
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(pem)
    except Exception:
        report["error_code"] = "invalid_pem"
        report["error"] = "certificate file is not a valid PEM certificate"
        report["state"] = "error"
        return report
    report.update(describe_certificate(cert))
    remaining = days_remaining(_parse_iso(report.get("not_after")), now)
    report["days_remaining"] = remaining
    report["state"] = certificate_state(remaining)
    return report


def _split_target(target):
    """Return (hostname, port, scheme) for a host, host:port or URL."""
    text = str(target or "").strip()
    if not text:
        return "", 443, ""
    if "://" in text:
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        scheme = (parsed.scheme or "").lower()
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError:
            port = 443
        return host, port, scheme
    if text.startswith("["):
        end = text.find("]")
        host = text[1:end] if end > 0 else text.strip("[]")
        rest = text[end + 1:] if end > 0 else ""
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else 443
        return host, port, "https"
    if text.count(":") == 1:
        host, _, raw_port = text.partition(":")
        return host, int(raw_port) if raw_port.isdigit() else 443, "https"
    return text, 443, "https"


def _first_cn(rdns) -> str:
    for entry in rdns or ():
        for key, value in entry or ():
            if key == "commonName":
                return value
    return ""


def probe_tls_endpoint(target, *, timeout=5.0, ca_bundle=None, now=None) -> dict:
    """Open a verified TLS connection and report the peer certificate.

    Verification is never disabled: a handshake that fails validation is
    reported as expired, hostname_mismatch or verification_failed.
    """
    host, port, scheme = _split_target(target)
    result = {
        "source": "endpoint",
        "host": host,
        "port": port,
        "scheme": scheme or "https",
        "verified": False,
        "state": "unknown",
        "error_code": None,
        "error": None,
        "days_remaining": None,
        "not_after": None,
        "issuer_cn": None,
        "subject_cn": None,
        "sans": [],
    }
    if not host:
        result["error_code"] = "invalid_host"
        result["error"] = "no hostname to probe"
        result["state"] = "error"
        return result
    if scheme and scheme != "https":
        result["error_code"] = "not_https"
        result["error"] = "endpoint is not an https URL"
        result["state"] = "error"
        return result
    try:
        context = ssl.create_default_context(cafile=ca_bundle)
    except Exception:
        result["error_code"] = "ca_bundle_invalid"
        result["error"] = "configured CA bundle could not be loaded"
        result["state"] = "error"
        return result
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                peer = tls.getpeercert() or {}
    except ssl.SSLCertVerificationError as exc:
        message = getattr(exc, "verify_message", None) or str(exc)
        lowered = str(message).lower()
        if "expired" in lowered:
            result["error_code"] = "expired"
            result["state"] = "expired"
        elif "hostname" in lowered or "ip address mismatch" in lowered:
            result["error_code"] = "hostname_mismatch"
            result["state"] = "error"
        else:
            result["error_code"] = "verification_failed"
            result["state"] = "error"
        result["error"] = str(message)[:300]
        return result
    except (socket.timeout, TimeoutError):
        result["error_code"] = "timeout"
        result["error"] = "no TLS response within {0:g}s".format(timeout)
        result["state"] = "error"
        return result
    except ssl.SSLError as exc:
        result["error_code"] = "tls_error"
        result["error"] = str(exc)[:300]
        result["state"] = "error"
        return result
    except OSError as exc:
        result["error_code"] = "unreachable"
        result["error"] = (exc.strerror or "connection failed")[:300]
        result["state"] = "error"
        return result
    result["verified"] = True
    not_after = None
    if peer.get("notAfter"):
        try:
            not_after = datetime.utcfromtimestamp(ssl.cert_time_to_seconds(peer["notAfter"]))
        except Exception:
            not_after = None
    remaining = days_remaining(not_after, now)
    result.update({
        "not_after": not_after.isoformat() if not_after else None,
        "days_remaining": remaining,
        "state": certificate_state(remaining),
        "subject_cn": _first_cn(peer.get("subject")),
        "issuer_cn": _first_cn(peer.get("issuer")),
        "sans": [value for _kind, value in (peer.get("subjectAltName") or ())],
    })
    return result


def build_tls_report(*, cert_path=None, endpoints=(), ca_bundle=None, timeout=5.0, now=None) -> dict:
    """Collect the local certificate plus one probe per https endpoint."""
    moment = now or datetime.utcnow()
    local = inspect_certificate_file(cert_path, now=moment)
    entries = []
    seen = set()
    for endpoint in endpoints:
        target = str(endpoint or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        entries.append(probe_tls_endpoint(target, timeout=timeout, ca_bundle=ca_bundle, now=moment))
    summary = {"ok": 0, "warning": 0, "critical": 0, "expired": 0, "error": 0, "unknown": 0}
    considered = 0
    if cert_path:
        considered += 1
        state = local.get("state") or "unknown"
        summary[state] = summary.get(state, 0) + 1
    for entry in entries:
        considered += 1
        state = entry.get("state") or "unknown"
        summary[state] = summary.get(state, 0) + 1
    return {
        "checked_at": moment.isoformat() + "Z",
        "thresholds": {"warn_days": warn_days(), "critical_days": critical_days()},
        "local": local,
        "endpoints": entries,
        "summary": summary,
        "healthy": summary["ok"] == considered and considered > 0,
    }


def get_tls_report(*, refresh=False, max_age=None, **kwargs) -> dict:
    """Return a cached report, rebuilding it when stale or forced."""
    age_limit = check_interval_seconds() if max_age is None else max(0, int(max_age))
    reference = time.monotonic()
    with _cache_lock:
        cached = _cache.get("report")
        age = reference - float(_cache.get("at") or 0.0)
        if cached is not None and not refresh and age < age_limit:
            return cached
    report = build_tls_report(**kwargs)
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["report"] = report
    return report


def invalidate_cache() -> None:
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["report"] = None
