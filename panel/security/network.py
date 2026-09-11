"""Inbound proxy trust and outbound panel transport policy.

Two independent concerns live here.

Client identity: X-Forwarded-* headers are only trusted when the direct peer is
an allowed proxy. The default allows loopback, private and link-local peers
(nginx on the same host, or on a container network); a peer connecting from a
public address is never allowed to speak for the client, so rate limits and
audit records keep the real address. TrustedProxyMiddleware records the resolved
client address on the WSGI environ, strips forged forwarding headers, and must
wrap Werkzeug ProxyFix from the outside so stripping happens first.

Panel transport: X-UI credentials must not cross the network in plaintext.
http:// is accepted for a loopback panel or when the operator explicitly sets
EVE_ALLOW_INSECURE_PANEL=1; every other plaintext panel URL is refused with an
actionable error instead of quietly sending the password in the clear.

EVE_TRUSTED_PROXIES overrides the default peer policy with a comma-separated
list of IPs/CIDRs, or * to trust every peer (the pre-hardening behaviour).
"""
import ipaddress
import logging
import os
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

INSECURE_PANEL_ENV = "EVE_ALLOW_INSECURE_PANEL"
TRUSTED_PROXIES_ENV = "EVE_TRUSTED_PROXIES"

# Headers a reverse proxy may set. They are removed before the application sees
# the request when the direct peer is not allowed to proxy for the client.
FORWARDED_HEADERS = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_FORWARDED_PROTO",
    "HTTP_X_FORWARDED_HOST",
    "HTTP_X_FORWARDED_PORT",
    "HTTP_X_FORWARDED_PREFIX",
    "HTTP_X_REAL_IP",
    "HTTP_FORWARDED",
)

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_untrusted_forwarding_warned = False


class InsecurePanelTransportError(RuntimeError):
    """A panel URL would send credentials over plaintext HTTP."""


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def allow_insecure_panel() -> bool:
    """True when the operator explicitly allowed plaintext panel credentials."""
    return _env_flag(INSECURE_PANEL_ENV)


def _parse_ip(value):
    text = str(value or "").strip().strip("[]")
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def is_loopback_host(host) -> bool:
    """True for localhost/127.0.0.0-8/::1 style hosts (same-machine panels)."""
    text = str(host or "").strip().strip("[]").lower()
    if not text:
        return False
    if text in _LOOPBACK_NAMES or text.endswith(".localhost"):
        return True
    ip = _parse_ip(text)
    return bool(ip and ip.is_loopback)


def trusted_proxy_policy():
    """Return None (default policy), "*" (trust any peer) or a network tuple."""
    raw = os.environ.get(TRUSTED_PROXIES_ENV)
    if raw is None:
        return None
    raw = raw.strip()
    if raw in {"*", "any", "all"}:
        return "*"
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid %s entry: %s", TRUSTED_PROXIES_ENV, entry)
    return tuple(networks)


def peer_is_trusted(address, policy=None) -> bool:
    """True when this direct peer may speak for the client via X-Forwarded-*."""
    ip = _parse_ip(address)
    if ip is None:
        return False
    policy = trusted_proxy_policy() if policy is None else policy
    if policy == "*":
        return True
    if policy is None:
        # Default: the reverse proxy is local or on a container/private network.
        return bool(ip.is_loopback or ip.is_private or ip.is_link_local)
    return any(ip in network for network in policy)


def resolve_client_ip(peer, forwarded_for, policy=None) -> str:
    """Resolve the client address, honouring X-Forwarded-For only from a proxy.

    The chain is walked right to left, skipping proxies we trust, so a client
    that prepends its own X-Forwarded-For entries cannot change the result: the
    rightmost address the trusted proxy appended wins.
    """
    policy = trusted_proxy_policy() if policy is None else policy
    peer_text = str(peer or "").strip()
    if not peer_is_trusted(peer_text, policy):
        return peer_text
    chain = [part.strip() for part in str(forwarded_for or "").split(",") if part.strip()]
    if not chain:
        return peer_text
    for candidate in reversed(chain):
        if peer_is_trusted(candidate, policy):
            continue
        return candidate
    return chain[0]


def _warn_untrusted_forwarding_once(peer) -> None:
    global _untrusted_forwarding_warned
    if _untrusted_forwarding_warned:
        return
    _untrusted_forwarding_warned = True
    logger.warning(
        "Ignoring X-Forwarded-* headers from untrusted peer %s; set %s if that "
        "peer really is a reverse proxy.", peer, TRUSTED_PROXIES_ENV,
    )


class TrustedProxyMiddleware:
    """Record the real client address and drop forged forwarding headers.

    Wrap the application outside werkzeug.middleware.proxy_fix.ProxyFix; the
    environ keys it writes (eve.peer_addr, eve.client_ip) are the authoritative
    client identity for logs, audit rows and the rate limiter.
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        policy = trusted_proxy_policy()
        peer = environ.get("REMOTE_ADDR") or ""
        environ["eve.peer_addr"] = peer
        if peer_is_trusted(peer, policy):
            environ["eve.client_ip"] = resolve_client_ip(
                peer, environ.get("HTTP_X_FORWARDED_FOR"), policy,
            )
        else:
            environ["eve.client_ip"] = peer
            if any(key in environ for key in FORWARDED_HEADERS):
                _warn_untrusted_forwarding_once(peer)
            for key in FORWARDED_HEADERS:
                environ.pop(key, None)
        return self.app(environ, start_response)


def client_ip(default: str = "") -> str:
    """Effective client address for the current request.

    Safe to call outside a request context (returns ``default``), because the
    rate limiter resolves its key lazily.
    """
    try:
        from flask import request
        environ = request.environ
    except Exception:
        return default
    resolved = environ.get("eve.client_ip")
    if resolved:
        return resolved
    peer = environ.get("eve.peer_addr") or environ.get("REMOTE_ADDR") or ""
    return resolve_client_ip(peer, environ.get("HTTP_X_FORWARDED_FOR")) or default


def limiter_client_key() -> str:
    """flask-limiter key: the resolved client address, never a forged header."""
    from flask_limiter.util import get_remote_address
    return client_ip() or get_remote_address()


def inspect_panel_url(url) -> dict:
    """Classify a configured panel URL without contacting it."""
    parsed = urlsplit(str(url or "").strip())
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    return {
        "scheme": (parsed.scheme or "").lower(),
        "host": host,
        "port": port,
        "loopback": is_loopback_host(host),
    }


def enforce_panel_transport(url, *, allow_insecure=None) -> dict:
    """Refuse plaintext panel credentials for a non-loopback host.

    Returns the classification (so callers can log it). Raises
    InsecurePanelTransportError for a URL that would leak the panel password.
    """
    info = inspect_panel_url(url)
    if info["scheme"] not in {"http", "https"}:
        raise InsecurePanelTransportError(
            "Panel URL must start with http:// or https://."
        )
    if info["scheme"] == "https":
        return info
    allowed = allow_insecure_panel() if allow_insecure is None else bool(allow_insecure)
    if info["loopback"] or allowed:
        return info
    raise InsecurePanelTransportError(
        "Refusing to send panel credentials over plaintext HTTP to "
        f"{info['host'] or 'a remote host'}. Use https:// for this server, or set "
        f"{INSECURE_PANEL_ENV}=1 to explicitly allow plaintext panel access."
    )
