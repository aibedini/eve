"""GMweb gateway contract (phase 28).

The consumer side of the SMS gateway integration is defined by
`shared/eve-gmweb-contract-v1.json`. This module is the one place that reads it,
so the endpoint paths, the URL rules and the request headers cannot drift from
the declared contract: a mismatch fails the contract test instead of failing a
send in production.

Rules enforced here:

* the gateway base URL must be an absolute http(s) URL with a host, no userinfo,
  no query and no fragment. Anything else (file://, gopher://, an embedded
  credential, a bare hostname) is refused before the API key can be sent
  anywhere;
* plaintext http to anything that is not a local or private address is allowed
  but reported as a warning, because the bearer key travels in clear text;
* endpoint paths come from the contract file, with path parameters quoted.
"""
import json
import os
import re
from urllib.parse import quote, urlparse

import requests

CONTRACT_FILENAME = "eve-gmweb-contract-v1.json"
_CONTRACT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "shared", CONTRACT_FILENAME)

_PRIVATE_IPV4 = re.compile(
    r"^(10\.|127\.|192\.168\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.)")
_LOCAL_NAMES = ("localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0")

_contract_cache = None


def load_contract() -> dict:
    """The declared contract, or an empty dict when the file is unreadable."""
    global _contract_cache
    if _contract_cache is not None:
        return _contract_cache
    try:
        with open(_CONTRACT_PATH, encoding="utf-8") as handle:
            _contract_cache = json.load(handle)
    except Exception:
        _contract_cache = {}
    return _contract_cache


def contract_version() -> int:
    try:
        return int(load_contract().get("version") or 0)
    except (TypeError, ValueError):
        return 0


def declared_scopes() -> list:
    defaults = load_contract().get("projectKeyDefaults") or {}
    scopes = defaults.get("scopes")
    return list(scopes) if isinstance(scopes, list) else []


def _transport_health_contract() -> dict:
    block = load_contract().get("transportHealthResponse")
    return block if isinstance(block, dict) else {}


def transport_health_contract_version() -> int:
    """Declared version of the transport-health RESPONSE contract.

    This is deliberately not the contract file's own `version`: the file version
    describes the whole Eve<->GMweb surface, while this one is the version the
    provider must echo in the response body so a half-upgraded deployment is
    detectable instead of silently misread.
    """
    try:
        return int(_transport_health_contract().get("contractVersion") or 0)
    except (TypeError, ValueError):
        return 0


def transport_health_sections() -> dict:
    """Declared section -> field names for the transport-health response."""
    sections = _transport_health_contract().get("sections")
    return sections if isinstance(sections, dict) else {}


def transport_health_probe_states() -> list:
    """The shared probe vocabulary, so both sides name the same failures."""
    states = _transport_health_contract().get("probeStates")
    return list(states) if isinstance(states, list) else []


def transport_health_optional_diagnostics() -> list:
    values = _transport_health_contract().get("optionalDiagnostics")
    return list(values) if isinstance(values, list) else []


def _delivery_event_contract() -> dict:
    block = load_contract().get("deliveryEventSearch")
    return block if isinstance(block, dict) else {}


def delivery_event_filters() -> list:
    values = _delivery_event_contract().get("filters")
    return list(values) if isinstance(values, list) else []


def delivery_event_maximum_limit() -> int:
    try:
        return max(1, min(100, int(_delivery_event_contract().get("maximumLimit") or 100)))
    except (TypeError, ValueError):
        return 100


_DELIVERY_STATUS = frozenset(("delivered", "failed"))
_CALLBACK_STATE = frozenset(("pending", "retry_wait", "delivering", "delivered",
                             "dead_letter"))
_SAFE_DELIVERY_FIELDS = {
    "eventId": (196, None),
    "traceId": (64, None),
    "messageId": (128, None),
    "requestId": (120, None),
    "gatewayRequestId": (120, None),
    "jobId": (120, None),
    "eveNotificationId": (120, re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,119}$")),
    "deviceId": (64, None),
    "status": (16, _DELIVERY_STATUS),
    "occurredAt": (64, None),
    "receivedAt": (64, None),
    "callbackState": (24, _CALLBACK_STATE),
}


def normalize_delivery_event_filters(values: dict | None) -> dict:
    """Validate and bound the v5 delivery-event search query."""
    raw = values if isinstance(values, dict) else {}
    allowed = set(delivery_event_filters())
    query = {}
    for key in allowed:
        value = raw.get(key)
        if value is None or value == "":
            continue
        if key in ("from", "to"):
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_%s" % key) from exc
            if number < 0:
                raise ValueError("invalid_%s" % key)
            query[key] = number
        elif key == "limit":
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_limit") from exc
            query[key] = max(1, min(delivery_event_maximum_limit(), number))
        elif key == "status":
            if str(value) not in _DELIVERY_STATUS:
                raise ValueError("invalid_status")
            query[key] = str(value)
        elif key == "callbackState":
            if str(value) not in _CALLBACK_STATE:
                raise ValueError("invalid_callback_state")
            query[key] = str(value)
        else:
            text = str(value).strip()
            maximum = 196 if key == "eventId" else 120
            if not text or len(text) > maximum or not text.isascii() or any(
                    ord(char) < 33 or ord(char) > 126 for char in text):
                raise ValueError("invalid_%s" % key)
            query[key] = text
    if query.get("from") is not None and query.get("to") is not None:
        if query["from"] > query["to"]:
            raise ValueError("invalid_time_range")
    query.setdefault("limit", min(50, delivery_event_maximum_limit()))
    return query


def project_delivery_event(value: dict) -> dict:
    """Allowlist one privacy-safe GMweb read-model event."""
    if not isinstance(value, dict):
        raise ValueError("invalid_delivery_event")
    forbidden = {str(name).lower() for name in
                 (_delivery_event_contract().get("forbiddenData") or [])}
    if any(str(key).lower() in forbidden for key in value):
        raise ValueError("forbidden_delivery_event_data")
    event = {}
    for key, (maximum, validator) in _SAFE_DELIVERY_FIELDS.items():
        raw = value.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or len(text) > maximum or not text.isascii():
            raise ValueError("invalid_delivery_event_%s" % key)
        if isinstance(validator, frozenset) and text not in validator:
            raise ValueError("invalid_delivery_event_%s" % key)
        if hasattr(validator, "fullmatch") and not validator.fullmatch(text):
            raise ValueError("invalid_delivery_event_%s" % key)
        event[key] = text
    if not event.get("eventId") or not event.get("status"):
        raise ValueError("invalid_delivery_event")
    return event


def fetch_delivery_events(base_url: str, api_key: str, filters: dict | None = None,
                          *, timeout: int = 5, verify=True, request_get=None) -> dict:
    """Read GMweb's optional v5 diagnostics without mutating EVE evidence."""
    validated = validate_base_url(base_url)
    base = validated.get("base")
    if not base or not str(api_key or "").strip():
        return {"ok": False, "available": False,
                "reason": validated.get("reason") or "gateway_not_configured"}
    try:
        query = normalize_delivery_event_filters(filters)
    except ValueError as exc:
        return {"ok": False, "available": True, "reason": str(exc)}
    getter = request_get or requests.get
    try:
        response = getter(
            base + endpoint_path("sms_delivery_events"),
            headers=request_headers(api_key), params=query,
            timeout=max(1, min(15, int(timeout or 5))), verify=verify)
    except Exception as exc:
        return {"ok": False, "available": True,
                "reason": "gateway_unreachable:%s" % type(exc).__name__}
    status_code = int(response.status_code)
    if status_code in (404, 405, 501):
        return {"ok": False, "available": False, "reason": "contract_missing",
                "status_code": status_code}
    if status_code == 401:
        return {"ok": False, "available": True, "reason": "auth_failed",
                "status_code": status_code}
    if status_code == 403:
        return {"ok": False, "available": True, "reason": "scope_denied",
                "status_code": status_code}
    if status_code >= 400:
        return {"ok": False, "available": True,
                "reason": "gateway_http_%s" % status_code, "status_code": status_code}
    try:
        payload = response.json() if response.content else {}
        rows = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > query["limit"]:
            raise ValueError("invalid_delivery_events_response")
        events = [project_delivery_event(row) for row in rows]
    except (TypeError, ValueError):
        return {"ok": False, "available": True, "reason": "invalid_response",
                "status_code": status_code}
    return {"ok": True, "available": True, "events": events,
            "limit": query["limit"], "status_code": status_code}


def endpoint_path(name: str, **params) -> str:
    """Declared path for one contract key, with path parameters quoted."""
    for entry in load_contract().get("endpoints") or []:
        if not isinstance(entry, dict) or entry.get("key") != name:
            continue
        path = str(entry.get("path") or "")
        for key, value in params.items():
            path = path.replace("{%s}" % key, quote(str(value), safe=""))
        return path
    raise KeyError("unknown gmweb endpoint: %s" % name)


def is_local_host(hostname: str) -> bool:
    host = str(hostname or "").strip().lower()
    if host in _LOCAL_NAMES:
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    return bool(_PRIVATE_IPV4.match(host))


def validate_base_url(raw) -> dict:
    """Return {base, reason, warning} for a configured gateway URL."""
    text = str(raw or "").strip()
    if not text:
        return {"base": None, "reason": "gateway_not_configured", "warning": None}
    try:
        parsed = urlparse(text)
    except Exception:
        return {"base": None, "reason": "invalid_gateway_url", "warning": None}
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return {"base": None,
                "reason": "invalid_gateway_scheme:%s" % (scheme or "missing"),
                "warning": None}
    if not parsed.hostname:
        return {"base": None, "reason": "invalid_gateway_url", "warning": None}
    if parsed.username or parsed.password:
        return {"base": None, "reason": "gateway_userinfo_not_allowed",
                "warning": None}
    if parsed.query or parsed.fragment:
        return {"base": None, "reason": "invalid_gateway_url", "warning": None}
    base = text.rstrip("/")
    warning = None
    if scheme == "http" and not is_local_host(parsed.hostname):
        warning = ("gateway_transport_is_plaintext:%s" % parsed.hostname)
    return {"base": base, "reason": None, "warning": warning}


def request_headers(api_key: str, *, json_body: bool = False,
                    idempotency_key: str | None = None) -> dict:
    """Headers every authenticated GMweb call sends."""
    headers = {
        "Authorization": "Bearer %s" % str(api_key or ""),
        "Accept": "application/json",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        # HTTP headers must be latin-1; a non-latin-1 key makes requests raise
        # UnicodeEncodeError and the send fails silently. The transform is
        # stable, so retries keep the same key and stay de-duplicated.
        headers["Idempotency-Key"] = (
            str(idempotency_key).encode("latin-1", "ignore").decode("latin-1") or "k")
    return headers


def reset_cache():
    """Drop the cached contract (tests)."""
    global _contract_cache
    _contract_cache = None
