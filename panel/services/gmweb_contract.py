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
