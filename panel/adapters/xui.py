"""3x-ui / X-UI panel adapter (extracted from app.py).

Session/cookie auth against X-UI panels, v3 client-API capability probing,
multi-inbound membership reconciliation, and inbound/online/status fetchers.
Helpers that still live in app.py are reached via deferred in-function
imports to avoid a module-level cycle.
"""
import base64
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import quote

import requests
from sqlalchemy import func, or_

from panel.core.redis_client import (
    bump_server_revision,
    publish_snapshot_to_redis,
    serialized_server_snapshot_write,
)
from panel.extensions import db
from panel.security import (
    InsecurePanelTransportError, enforce_panel_transport, outbound_tls_verify,
    panel_tls_verify,
)
from panel.models import (
    Admin,
    ClientOwnership,
    PanelAPI,
    Transaction,
    get_panel_api,
)
# Version-gated behaviour lives in one place. This adapter consumes it; it must
# never grow its own version comparisons (specs/001-3xui-37-38-compat FR-009).
from panel.services import xui_compat

# Session cache for X-UI panels to speed up API calls
XUI_SESSION_CACHE = {}  # server_id -> {'session': requests.Session, 'expiry': float}
XUI_SESSION_TTL = 600  # 10 minutes cache
XUI_CAPABILITY_CACHE = {}  # server_id -> {'v3_clients': bool, 'expiry': float}
XUI_CAPABILITY_TTL = 600
#: server_id -> {'state': PROBE_*, 'expiry': float}. The legacy-family verdict, kept
#: separately because it answers a different question than the v3 route probe.
XUI_LEGACY_PROBE_CACHE = {}


def session_tls_verify(session_obj, server=None):
    """The TLS policy one X-UI request must use.

    get_xui_session()/get_xui_cookie_session() pin the per-server policy on the
    requests.Session, so a request that passes an explicit verify= must ask this
    helper: hardcoding a value would keep verifying an allow_insecure panel while
    the rest of the flow skipped it (or the other way round).
    """
    if server is not None:
        return panel_tls_verify(server)
    verify = getattr(session_obj, "verify", None)
    if verify is None:
        return outbound_tls_verify("EVE_XUI_CA_BUNDLE")
    return verify


def invalidate_xui_caches(server_id=None, host=None, username=None) -> None:
    """Drop every X-UI session/capability/cookie cache for one server.

    Called whenever host, username, password, api_token, allow_insecure or
    panel_type changes, so a session built under the old security or auth policy
    is never reused (an allow_insecure flip must not keep a verified/unverified
    session alive).
    """
    if server_id is not None:
        XUI_SESSION_CACHE.pop(server_id, None)
        XUI_CAPABILITY_CACHE.pop(server_id, None)
        XUI_LEGACY_PROBE_CACHE.pop(server_id, None)
        # The detected version and its profile are cached per server too: a
        # changed host or token must never leave a stale profile in place.
        xui_compat.invalidate_compatibility(server_id)
    host_key = str(host or "").strip()
    user_key = str(username or "").strip()
    if not host_key:
        return
    prefixes = ("%s|%s" % (host_key, user_key),) if user_key else ()
    for key in list(XUI_COOKIE_SESSION_CACHE):
        text = str(key)
        if text == host_key or text.startswith(host_key + "|") or any(
                text.startswith(prefix) for prefix in prefixes):
            XUI_COOKIE_SESSION_CACHE.pop(key, None)

def extract_base_and_webpath(host_url):
    """Extract base URL and webpath from panel URL.
    Example: http://1.2.3.4:8080/webpath/ -> (http://1.2.3.4:8080, /webpath)
    """
    from urllib.parse import urlparse
    parsed = urlparse(host_url.rstrip('/'))
    base = f"{parsed.scheme}://{parsed.netloc}"
    webpath = parsed.path.rstrip('/') if parsed.path and parsed.path != '/' else ''
    return base, webpath


def _safe_response_json(resp: requests.Response):
    """Best-effort JSON parse for upstream panel responses.

    Returns (data, error_message). Never raises JSONDecodeError.
    """
    try:
        raw = resp.content or b''
        if not raw:
            return None, f"Empty response (status {resp.status_code})"
        return resp.json(), None
    except Exception:
        try:
            content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        except Exception:
            content_type = ''
        try:
            text = (resp.text or '')
        except Exception:
            text = ''
        snippet = re.sub(r"\s+", " ", (text[:200] if text else '')).strip()
        if not snippet:
            snippet = '<no body>'
        return None, f"Non-JSON response (status {resp.status_code}, content-type {content_type}): {snippet}"


def _format_panel_connection_error(server, exc=None):
    """Return a short user-facing panel connection error.

    Raw requests exceptions include noisy pool/socket internals that are useful
    in logs but confusing in the UI.
    """
    try:
        base, _ = extract_base_and_webpath(getattr(server, 'host', '') or '')
    except Exception:
        base = getattr(server, 'host', '') or 'panel host'

    return (
        f"Panel connection timed out for {base}. "
        "The server panel is not reachable right now. "
        "Check panel URL/IP, port, firewall, web path and panel type."
    )


def get_server_api_token(server) -> str:
    """Decrypt the stored 3x-ui v3 API token (Bearer), or '' if none."""
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import decrypt_server_password
    raw = getattr(server, 'api_token', '') or ''
    if not raw:
        return ''
    try:
        return decrypt_server_password(raw)
    except Exception:
        return raw


def _remember_v3_capability(server, supported: bool):
    try:
        sid = int(getattr(server, 'id'))
    except (TypeError, ValueError):
        return
    XUI_CAPABILITY_CACHE[sid] = {
        'v3_clients': bool(supported),
        'expiry': time.time() + XUI_CAPABILITY_TTL,
    }


PROBE_SUPPORTED = "SUPPORTED"
PROBE_ROUTE_MISSING = "ROUTE_MISSING"
PROBE_AUTH_INVALID = "AUTH_INVALID"
PROBE_SCOPE_INSUFFICIENT = "SCOPE_INSUFFICIENT"
PROBE_TRANSPORT_ERROR = "TRANSPORT_ERROR"
PROBE_INVALID_RESPONSE = "INVALID_RESPONSE"


def _probe_headers_for(server) -> dict:
    """Request headers the capability probe must use for this panel.

    On the 3.7 profile a rejected Bearer is answered 404 unless the request
    carries X-Requested-With: XMLHttpRequest, which makes "bad credential" and
    "route absent" indistinguishable. 3.8 answers 401 on the Bearer alone, and
    older panels answer the same either way - so the header is only sent where
    upstream evidence shows it changes the outcome.
    """
    headers = {"Accept": "application/json"}
    compat = xui_compat.cached_compatibility(getattr(server, "id", None))
    if compat is not None and compat.profile.bearer_hint_header_required:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


def _classify_probe_response(resp) -> str:
    """Map one probe HTTP response onto a typed outcome.

    The distinction that matters: an authentication or authorization failure is
    NOT evidence that the route is absent. Collapsing them is how a scoped token
    turns a modern panel into a "legacy" one and leaves renewed users inactive.
    """
    status = getattr(resp, "status_code", None)
    if status == 401:
        return PROBE_AUTH_INVALID
    if status == 403:
        return PROBE_SCOPE_INSUFFICIENT
    if status == 404:
        return PROBE_ROUTE_MISSING
    if status != 200:
        return PROBE_ROUTE_MISSING if status == 405 else PROBE_INVALID_RESPONSE
    payload, parse_error = _safe_response_json(resp)
    if parse_error or not isinstance(payload, dict):
        return PROBE_INVALID_RESPONSE
    if "success" not in payload and "obj" not in payload and "msg" not in payload:
        return PROBE_INVALID_RESPONSE
    return PROBE_SUPPORTED


def probe_v3_client_api(server, session_obj, *, force=False) -> str:
    """Typed capability probe. Returns one of the PROBE_* outcomes.

    Only a definitive answer about the ROUTE may update the capability cache.
    Authentication and authorization failures leave the cached capability
    untouched, because they say nothing about whether the route exists.
    """
    try:
        sid = int(getattr(server, "id"))
    except (TypeError, ValueError):
        sid = None
    if not force and sid is not None:
        cached = XUI_CAPABILITY_CACHE.get(sid)
        if cached and time.time() < float(cached.get("expiry") or 0):
            return PROBE_SUPPORTED if cached.get("v3_clients") else PROBE_ROUTE_MISSING

    base, webpath = extract_base_and_webpath(server.host)
    url = "%s%s/panel/api/clients/get/__eve_capability_probe__" % (base, webpath)
    try:
        resp = session_obj.get(url, verify=session_tls_verify(session_obj), timeout=(3, 8),
                               headers=_probe_headers_for(server))
        outcome = _classify_probe_response(resp)
    except Exception:
        # A transient probe failure must not overwrite a previously known result.
        if sid is not None and sid in XUI_CAPABILITY_CACHE:
            return PROBE_SUPPORTED if XUI_CAPABILITY_CACHE[sid].get("v3_clients") else PROBE_ROUTE_MISSING
        return PROBE_TRANSPORT_ERROR

    if outcome == PROBE_SUPPORTED:
        _remember_v3_capability(server, True)
    elif outcome == PROBE_ROUTE_MISSING:
        _remember_v3_capability(server, False)
    elif outcome in (PROBE_AUTH_INVALID, PROBE_SCOPE_INSUFFICIENT):
        # An auth/scope problem is an operator-actionable state, not a version
        # fact. Attach it to whatever we already know about this panel so the
        # doctor can say why management is degraded - and never let it rewrite
        # the detected version.
        compat = xui_compat.cached_compatibility(sid)
        if compat is not None:
            warning = (xui_compat.WARN_AUTH_INVALID
                       if outcome == PROBE_AUTH_INVALID
                       else xui_compat.WARN_SCOPE_INSUFFICIENT)
            xui_compat.remember_compatibility(
                xui_compat.compat_with_warning(compat, warning))
    # AUTH_INVALID / SCOPE_INSUFFICIENT / INVALID_RESPONSE / TRANSPORT_ERROR
    # deliberately do NOT touch the cache (spec FR-012, FR-013, guarantee N4).
    return outcome


def _probe_legacy_inbound_api(server, session_obj, *, force=False) -> str:
    """Typed probe for the LEGACY inbound client API. Returns a PROBE_* outcome.

    Needed because a 404 is not proof of anything on its own: 3x-ui answers 404 both
    for "this route does not exist" and for "authentication aborted" (upstream
    answers 404 to a bare unauthenticated request on every version, and v2.8.11/v3.0
    answer 404 on a failed credential too). Choosing the legacy write because the
    first-class probe 404'd would therefore be a guess - and the guess lands a
    mutation on a panel that may well be modern.

    The probe is a READ: ``POST /panel/api/inbounds/onlines`` lists online clients on
    every version that has the legacy family (<= v3.0.x) and is absent from v3.1.0
    onwards, where client listing moved to /clients/onlines. So:
        200  -> the legacy family exists (v2.x / v3.0.x), proven
        404  -> neither family answered: unclassifiable, not "legacy"
        401/403 -> credential or scope problem, not a version fact
    """
    try:
        sid = int(getattr(server, "id"))
    except (TypeError, ValueError):
        sid = None
    if not force and sid is not None:
        cached = XUI_LEGACY_PROBE_CACHE.get(sid)
        if cached and time.time() < float(cached.get("expiry") or 0):
            return str(cached.get("state"))
    base, webpath = extract_base_and_webpath(server.host)
    url = "%s%s/panel/api/inbounds/onlines" % (base, webpath)
    try:
        resp = session_obj.post(url, json={}, timeout=(3, 8),
                                verify=session_tls_verify(session_obj),
                                headers=_probe_headers_for(server))
        outcome = _classify_probe_response(resp)
    except Exception:
        return PROBE_TRANSPORT_ERROR
    if sid is not None and outcome in (PROBE_SUPPORTED, PROBE_ROUTE_MISSING,
                                       PROBE_AUTH_INVALID, PROBE_SCOPE_INSUFFICIENT):
        XUI_LEGACY_PROBE_CACHE[sid] = {"state": outcome,
                                       "expiry": time.time() + XUI_CAPABILITY_TTL}
    return outcome


def probe_legacy_inbound_api(server, session_obj, *, force=False) -> str:
    """Public alias: the legacy-family verdict, for the capability planner."""
    return _probe_legacy_inbound_api(server, session_obj, force=force)


def _probe_v3_client_api(server, session_obj, *, force=False) -> bool:
    """Boolean view of probe_v3_client_api for existing callers.

    Kept because the capability question (does the first-class v3 client API
    exist?) is still legitimate on its own. It is NOT a version check and must
    never be used as one - use panel.services.xui_compat for that.
    """
    try:
        sid = int(getattr(server, 'id'))
    except (TypeError, ValueError):
        sid = None
    if not force and sid is not None:
        cached = XUI_CAPABILITY_CACHE.get(sid)
        if cached and time.time() < float(cached.get('expiry') or 0):
            return bool(cached.get('v3_clients'))

    base, webpath = extract_base_and_webpath(server.host)
    url = f"{base}{webpath}/panel/api/clients/get/__eve_capability_probe__"
    supported = False
    try:
        resp = session_obj.get(url, verify=session_tls_verify(session_obj), timeout=(3, 8),
                               headers={'Accept': 'application/json'})
        payload, parse_error = _safe_response_json(resp)
        supported = (
            resp.status_code == 200
            and not parse_error
            and isinstance(payload, dict)
            and ('success' in payload or 'obj' in payload or 'msg' in payload)
        )
    except Exception:
        # A transient probe failure must not overwrite a previously known result.
        if sid is not None and sid in XUI_CAPABILITY_CACHE:
            return bool(XUI_CAPABILITY_CACHE[sid].get('v3_clients'))
        return False
    _remember_v3_capability(server, supported)
    return supported


def server_is_v3(server, session_obj=None, *, force_probe=False) -> bool:
    """Return whether the panel supports the first-class v3 client API.

    Authentication mode and API generation are intentionally independent:
    Bearer-token and cookie+CSRF sessions can both be v3.
    """
    try:
        cached = XUI_CAPABILITY_CACHE.get(int(getattr(server, 'id')))
        if cached and time.time() < float(cached.get('expiry') or 0):
            return bool(cached.get('v3_clients'))
    except (TypeError, ValueError):
        pass
    if session_obj is not None:
        return _probe_v3_client_api(server, session_obj, force=force_probe)
    # Before the first authenticated probe, a configured token is a useful UI
    # hint. Network mutations always pass a session and therefore verify it.
    return bool(get_server_api_token(server))


# ── 3x-ui v3+ client API (/panel/api/clients/*) ──────────────────────────────
# In v3 the per-client inbound endpoints (updateClient/delClient/resetClientTraffic)
# were removed; clients are first-class and managed by email here. Verified live:
#   - update : POST /clients/update/{email}  body = bare client dict, id = uuid
#   - delete : POST /clients/del/{email}     (?keepTraffic=1 to keep stats)
#   - reset  : POST /clients/resetTraffic/{email}
#   - add    : POST /clients/add             body = {client, inboundIds}

def _v3_post(server, session_obj, path, json_body=None, *, timeout=(3, 20)):
    """POST to a v3 /panel/api/* path. Returns (ok: bool, json|None, error|None)."""
    base, webpath = extract_base_and_webpath(server.host)
    url = f"{base}{webpath}{path}"
    try:
        resp = session_obj.post(
            url,
            json=(json_body if json_body is not None else {}),
            verify=session_tls_verify(session_obj),
            timeout=timeout,
        )
    except Exception as e:
        return False, None, str(e)
    j, err = _safe_response_json(resp)
    if err:
        return False, None, err
    if resp.status_code == 200 and isinstance(j, dict) and j.get('success'):
        return True, j, None
    msg = (j.get('msg') or j.get('message')) if isinstance(j, dict) else None
    return False, j, (msg or f"HTTP {resp.status_code}")


#: Distinguishes "caller did not ask to change the device limit" (preserve the
#: panel value) from an explicit request, including an explicit 0.
_UNSET = object()


def _v3_client_payload(client: dict, limit_hwid=None) -> dict:
    """Shape a client dict for v3 /clients/update|add. v3 unmarshals Client.id as a
    string, so `id` must carry the UUID (not the numeric DB row id). Numeric fields
    must be numbers, not empty strings.

    limit_hwid is the panel-side device limit to PRESERVE, read from an
    authoritative client read. It is a sibling of the client object upstream
    (model.Client has no such field) and the server writes it unconditionally,
    defaulting an absent key to 0. So: pass the real value when the panel
    exposes one, and pass None - never 0 - when it does not. A stored 0 is a
    real operator choice and round-trips as 0."""
    c = dict(client or {})
    uid = c.get('uuid') or c.get('id') or ''
    if uid:
        c['id'] = uid
    if limit_hwid is not None:
        c['limitHwid'] = int(limit_hwid)
    for k in ('tgId', 'limitIp', 'reset'):
        if c.get(k) in ('', None):
            c[k] = 0
    # 3x-ui v3.4+ made model.Client.Security non-omitempty: a client object with
    # no `security` deserializes to "" and the node-add path panics → the API
    # returns an empty 200 and the client is silently NOT added. Default it to
    # xray's standard "auto" (ignored by VLESS/Trojan, valid for VMess); harmless
    # on older panels. Only set when missing so an explicit value is preserved.
    if not c.get('security'):
        c['security'] = 'auto'
    if isinstance(c.get('email'), str):
        c['email'] = _v3_sanitize_email(c['email'])
    return c


def _v3_sanitize_email(email: str) -> str:
    """v3 rejects emails containing spaces; strip them before every API call."""
    return (email or '').replace(' ', '')


def _v3_get(server, session_obj, path, *, timeout=(3, 20)):
    """GET a v3 /panel/api/* path. Returns (ok: bool, json|None, error|None)."""
    base, webpath = extract_base_and_webpath(server.host)
    url = f"{base}{webpath}{path}"
    try:
        resp = session_obj.get(
            url,
            headers={
                'Cache-Control': 'no-store, no-cache, max-age=0',
                'Pragma': 'no-cache',
            },
            verify=session_tls_verify(session_obj),
            timeout=timeout,
        )
    except Exception as e:
        return False, None, str(e)
    j, err = _safe_response_json(resp)
    if err:
        return False, None, err
    if resp.status_code == 200 and isinstance(j, dict) and j.get('success'):
        return True, j, None
    msg = (j.get('msg') or j.get('message')) if isinstance(j, dict) else None
    return False, j, (msg or f"HTTP {resp.status_code}")


def _v3_get_client(server, session_obj, email):
    """Fetch one client via GET /clients/get/{email}. Returns the client dict or None."""
    details = v3_get_client_details(server, session_obj, email)
    return details.get('client') if details.get('ok') else None


def v3_get_client_details(server, session_obj, email):
    """Authoritative first-class client read, keeping the membership metadata.

    ``/clients/get/{email}`` answers ``{"obj": {"client": {...}, "inboundIds": [...]}}``.
    The inbound ids are the panel's own statement about which inbounds this client
    belongs to, and every attached membership has to be verified separately: a
    client can be enabled globally and disabled inside one inbound, which is
    invisible if only the inner client object is read (that is the false-positive
    renewal verification this helper exists to close).

    Returns a dict:
        {'ok': bool, 'client': dict|None, 'inbound_ids': list[int],
         'raw': dict|None, 'error': str|None}
    Never raises; a failed read is ``ok=False`` with a reason.
    """
    empty = {'ok': False, 'client': None, 'inbound_ids': [], 'raw': None,
             'error': None}
    ok, j, err = _v3_get(
        server, session_obj,
        f"/panel/api/clients/get/{quote(str(email or ''), safe='')}")
    if not ok or not isinstance(j, dict):
        empty['error'] = err or 'client read failed'
        return empty
    obj = j.get('obj')
    if not isinstance(obj, dict):
        empty['error'] = 'client read returned no object'
        return empty
    inner = obj.get('client')
    client = inner if (isinstance(inner, dict) and inner.get('email')) else (
        obj if obj.get('email') else None)
    if client is None:
        empty['error'] = 'client not found'
        return empty
    inbound_ids = []
    for raw_id in (obj.get('inboundIds') or []):
        value = _as_panel_int(raw_id)
        if value is not None and value not in inbound_ids:
            inbound_ids.append(value)
    return {'ok': True, 'client': client, 'inbound_ids': inbound_ids,
            'raw': obj, 'error': None}


def _as_panel_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def v3_client_traffic(server, session_obj, email):
    """Read a client's traffic row: GET /clients/traffic/{email}.

    The traffic row is the panel's own view of consumption and of whether it has
    disabled the client for depletion. It is a separate layer from the client
    record, and EVE verifies it separately rather than assuming the two agree.
    Returns a dict shaped for ``renew_activation.analyze_activation``; an
    unsupported or unreadable endpoint reports ``available=False`` with a reason
    instead of inventing zeroes.
    """
    ok, j, err = _v3_get(
        server, session_obj,
        f"/panel/api/clients/traffic/{quote(str(email or ''), safe='')}")
    if not ok or not isinstance(j, dict):
        return {'available': False, 'reason': err or 'traffic read failed'}
    obj = j.get('obj')
    if isinstance(obj, list):
        row = None
        for item in obj:
            if isinstance(item, dict) and (
                    str(item.get('email') or '').lower()
                    == str(email or '').lower()):
                row = item
                break
        obj = row
    if not isinstance(obj, dict):
        return {'available': False, 'reason': 'traffic row not present'}
    return {
        'available': True,
        'enable': bool(obj.get('enable', True)),
        'up': _as_panel_int(obj.get('up')) or 0,
        'down': _as_panel_int(obj.get('down')) or 0,
        'total': _as_panel_int(obj.get('total')),
        'expiry': _as_panel_int(obj.get('expiryTime')),
    }


def _v3_node_pending(payload) -> bool:
    """Extract ``obj.nodePending`` from a mutation response, when it is exposed.

    From 3.3.1 a write can be committed in the panel's own database while the
    backing node has not synchronised yet. That is the difference between "the
    renewal was applied" and "the customer is online", so the flag is parsed
    rather than discarded. Returns False when the panel does not expose it (an
    older version is not "pending", it simply cannot say).
    """
    if not isinstance(payload, dict):
        return False
    obj = payload.get('obj')
    if not isinstance(obj, dict):
        return False
    return bool(obj.get('nodePending') is True)


@dataclass
class PanelMutationResult:
    """What one panel write actually did, as opposed to what it returned.

    ``transport_ok`` is "the panel answered"; ``panel_success`` is "the panel said
    it succeeded"; ``node_pending`` is "its node has not caught up yet";
    ``skipped`` carries the upstream per-email skip list; ``partially_applied`` is
    set when a failure may still have committed part of the change.
    """

    transport_ok: bool = False
    panel_success: bool = False
    node_pending: bool = False
    skipped: list = field(default_factory=list)
    partially_applied: bool = False
    need_restart: bool = False
    error: str | None = None
    response: dict | None = None

    @property
    def ok(self) -> bool:
        """The operation is applied on the panel (pending node sync is NOT a failure)."""
        return bool(self.transport_ok and self.panel_success and not self.skipped)

    def as_dict(self) -> dict:
        """Credential-free summary for logs, the trace and the doctor."""
        return {
            'transport_ok': self.transport_ok,
            'panel_success': self.panel_success,
            'node_pending': self.node_pending,
            'skipped': [str(item)[:120] for item in (self.skipped or [])],
            'partially_applied': self.partially_applied,
            'need_restart': self.need_restart,
            'error': self.error,
        }


def classify_mutation_result(ok, response=None, error=None, *,
                             may_be_partial=False) -> PanelMutationResult:
    """Turn one (ok, response, error) triple into a structured mutation result."""
    result = PanelMutationResult(transport_ok=bool(response is not None) or bool(ok),
                                 response=response if isinstance(response, dict) else None)
    if not ok:
        result.error = str(error or 'panel write failed')[:500]
        result.panel_success = False
        # A transport-level failure may still have been applied upstream (the
        # request can time out after the commit). The caller decides by reading the
        # panel back; this flag says "do not assume nothing happened".
        result.partially_applied = bool(may_be_partial)
        return result
    obj = response.get('obj') if isinstance(response, dict) else None
    skipped = obj.get('skipped') if isinstance(obj, dict) else None
    if isinstance(skipped, list):
        result.skipped = [item for item in skipped]
        if result.skipped:
            result.error = 'panel skipped the requested client'
    result.node_pending = _v3_node_pending(response)
    result.need_restart = bool(isinstance(obj, dict) and obj.get('needRestart') is True)
    # ``success`` is the panel's own verdict; a 200 with success=false is a failure.
    if isinstance(response, dict) and response.get('success') is False:
        result.panel_success = False
        result.error = result.error or str(response.get('msg') or 'panel reported failure')
    else:
        result.panel_success = True
    return result


def _v3_rename_email_via_inbounds(server, session_obj, old_email, new_email):
    """Fallback rename: rewrite the client's email inside every inbound that
    contains it and push the full inbounds back via the universal
    /inbounds/update/:id endpoint (works even when the per-client API refuses
    the spaced email entirely)."""
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import _json_field
    inbounds, fetch_err, _dt = fetch_inbounds(session_obj, server.host, server.panel_type)
    if fetch_err or not inbounds:
        return False
    old_found = False
    clean_taken = False
    for ib in inbounds:
        for c in _json_field(ib.get('settings'), {}).get('clients', []) or []:
            if c.get('email') == old_email:
                old_found = True
            elif c.get('email') == new_email:
                clean_taken = True
    if not old_found:
        # already renamed earlier (clean_taken) or genuinely missing
        return clean_taken
    if clean_taken:
        return False  # a different client already owns the space-free email
    renamed_any = False
    for ib in inbounds:
        settings = _json_field(ib.get('settings'), {})
        clients = settings.get('clients', []) or []
        if not any(c.get('email') == old_email for c in clients):
            continue
        for c in clients:
            if c.get('email') == old_email:
                c['email'] = new_email
        settings['clients'] = clients
        ok_push, _perr = _push_full_inbound(server, session_obj, ib, settings)
        renamed_any = renamed_any or ok_push
    return renamed_any


def _rename_client_email_local(server, old_email, new_email):
    """After a panel-side rename, move ownership rows and the live cache to the
    new email so reseller access checks and the dashboard keep matching."""
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import app, patch_cached_client
    try:
        rows = ClientOwnership.query.filter(
            ClientOwnership.server_id == server.id,
            func.lower(ClientOwnership.client_email) == (old_email or '').strip().lower(),
        ).all()
        for own in rows:
            own.client_email = new_email
        if rows:
            db.session.commit()
    except Exception as exc:
        app.logger.debug("ownership rename '%s' -> '%s' failed: %s", old_email, new_email, exc)
        try:
            db.session.rollback()
        except Exception:
            pass
    # Move transaction history (renewals, gifts) to the new email so the
    # "last renewal" / gift-once notices keep matching after the rename.
    # One-time per client; not on the hot renewal path.
    try:
        old_l = (old_email or '').strip().lower()
        tx_rows = Transaction.query.filter(
            func.lower(Transaction.client_email) == old_l,
        ).all()
        for tx in tx_rows:
            tx.client_email = new_email
        if tx_rows:
            db.session.commit()
    except Exception as exc:
        app.logger.debug("transaction email rename '%s' -> '%s' failed: %s", old_email, new_email, exc)
        try:
            db.session.rollback()
        except Exception:
            pass
    try:
        patch_cached_client(server.id, old_email, new_email=new_email)
    except Exception:
        pass


def _v3_fix_spaced_email(server, session_obj, email, client_obj=None):
    """v3 panels reject per-client API calls when the client's email contains
    spaces ("update failed"), so the client must FIRST be renamed on the panel
    to the space-free email, and only then can it be updated/deleted/reset.
    Returns the email all subsequent v3 calls should use."""
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import app
    original = str(email or '')
    clean = _v3_sanitize_email(original)
    if clean == original or not clean:
        return original

    # Rename via the first-class client update, looking the client up under its
    # current (spaced) email; the body carries the space-free email.
    payload = None
    if isinstance(client_obj, dict) and client_obj.get('email'):
        payload = dict(client_obj)
    else:
        payload = _v3_get_client(server, session_obj, original)
    renamed = False
    if isinstance(payload, dict):
        payload['email'] = clean
        renamed, _j, _err = _v3_post(
            server, session_obj,
            f"/panel/api/clients/update/{quote(original, safe='')}",
            _v3_client_payload(payload))

    if not renamed:
        renamed = _v3_rename_email_via_inbounds(server, session_obj, original, clean)

    if not renamed:
        app.logger.warning("v3: could not strip spaces from client email '%s'", original)
        return original
    _rename_client_email_local(server, original, clean)
    app.logger.info("v3: client email '%s' renamed to '%s' (v3 rejects spaces)", original, clean)
    return clean


def read_authoritative_client_settings(server, session_obj, email):
    """Read the client record the mutation read path cannot see.

    EVE builds update payloads from /inbounds/list, whose settings.clients[] does
    NOT carry limitHwid (verified on a live 3.8.0 panel). Updating a client with
    the key absent makes the panel write 0, silently removing the operator
    device cap. This reads the authoritative record so the value can be echoed.

    Returns (ok, client_dict). ok=False means the read FAILED and the caller must
    not send a payload that would clear the stored value. When ok=True but
    limitHwid is absent from the record, the panel simply does not expose it and
    the field must be omitted. Only fields on
    xui_compat.PRESERVED_CLIENT_FIELDS are ever consulted - secret and write-only
    fields are never read back or resubmitted (spec FR-023).
    """
    base, webpath = extract_base_and_webpath(server.host)
    url = "%s%s/panel/api/clients/get/%s" % (base, webpath, quote(str(email or ""), safe=""))
    try:
        resp = session_obj.get(
            url,
            headers={"Accept": "application/json",
                     "Cache-Control": "no-store, no-cache, max-age=0"},
            verify=session_tls_verify(session_obj),
            timeout=(3, 12),
        )
    except Exception:
        return False, None
    payload, parse_error = _safe_response_json(resp)
    if parse_error or not isinstance(payload, dict):
        return False, None
    # Guarded comparison: a stub or an unexpected object must read as "no
    # authoritative value", never raise. The caller fails closed either way.
    status = getattr(resp, "status_code", None)
    if not isinstance(status, int) or status != 200 or not payload.get("success"):
        # A missing client is a real answer; an auth/scope failure is not a
        # successful read either way, and the caller must fail closed.
        return False, None
    obj = payload.get("obj")
    # /clients/get answers {"obj": {"client": {...}, "inboundIds": [...]}}.
    client_record = obj.get("client") if isinstance(obj, dict) else None
    if not isinstance(client_record, dict):
        return False, None
    return True, client_record


def v3_update_client(server, session_obj, email, client: dict, *, limit_hwid=_UNSET,
                     preserved_client=None):
    """Update a v3 client, preserving panel-side state EVE does not model.

    By default this reads the authoritative client record and echoes the device
    limit, because the panel writes that sibling field unconditionally and would
    otherwise reset it to 0. Pass limit_hwid explicitly to SET the device limit
    (an operator request), or preserved_client to reuse a read the caller already
    made. Never sends a guessed or defaulted 0.

    Returns ``(ok, response, error)``. Callers that need the response's semantics
    (``nodePending``, a per-email skip list, a possible partial apply) must use
    :func:`v3_update_client_result` instead of throwing the response away.
    """
    result = v3_update_client_result(server, session_obj, email, client,
                                     limit_hwid=limit_hwid,
                                     preserved_client=preserved_client)
    return result.ok, result.response, result.error


def v3_update_client_result(server, session_obj, email, client: dict, *,
                            limit_hwid=_UNSET, preserved_client=None,
                            timeout=(3, 20)):
    """The same update, reported as a structured :class:`PanelMutationResult`.

    The response is parsed, not discarded: from 3.3.1 it can carry
    ``obj.nodePending``, which means the panel committed the configuration but its
    node has not synchronised - i.e. the renewal is applied and the customer may
    still be offline. Reporting that as plain success is the bug this exists for.
    """
    email = _v3_fix_spaced_email(server, session_obj, email, client_obj=client)
    if limit_hwid is not _UNSET:
        preserved = limit_hwid
    else:
        snapshot = preserved_client
        if not isinstance(snapshot, dict):
            ok, snapshot = read_authoritative_client_settings(server, session_obj, email)
            if not ok:
                return classify_mutation_result(
                    False, None,
                    "could not read the client's current device limit; refusing to update "
                    "because the panel would reset it to 0")
        preserved = xui_compat.preserved_limit_hwid(snapshot)
    ok, response, error = _v3_post(
        server, session_obj,
        f"/panel/api/clients/update/{quote(email, safe='')}",
        _v3_client_payload(client, limit_hwid=preserved),
        timeout=timeout)
    return classify_mutation_result(ok, response, error, may_be_partial=not ok)


def v3_enable_client(server, session_obj, email, client: dict, *,
                     capabilities=None, preserved_client=None):
    """Force a v3 client active, using only the primitives this panel proves.

    ``bulkEnable`` exists from 3.5 and also synchronises the running node; the
    full client update with ``enable=true`` exists across the whole first-class
    family. Which one is used is a capability decision, not a guess:

    * capabilities say ``bulk_enable`` -> ``/clients/bulkEnable`` (whose response
      is parsed, because upstream answers 200/success with the requested email in
      ``obj.skipped`` when it refuses);
    * capabilities do not prove it -> the full update, which cannot 404 on any
      v3.1+ panel.

    The unproven case keeps a narrow runtime fallback: if an endpoint EVE believed
    in turns out to be absent (404/405), the update is used instead. That fallback
    is a repair for a wrong capability claim, never a substitute for the claim.
    """
    enabled_client = dict(client or {})
    enabled_client['enable'] = True
    email = _v3_fix_spaced_email(
        server, session_obj, email, client_obj=enabled_client,
    )
    use_bulk = True
    if capabilities is not None:
        use_bulk = bool(getattr(capabilities, 'bulk_enable', False))
    if use_bulk:
        ok, result, error = _v3_post(
            server, session_obj, "/panel/api/clients/bulkEnable",
            {"emails": [email]},
        )
        if ok:
            # 3x-ui's bulk endpoint can return HTTP 200/success=true while putting
            # the requested account in obj.skipped.  Treat that as a failed enable;
            # the caller must never turn a transport acknowledgement into a fake
            # active state in EVE.
            obj = result.get('obj') if isinstance(result, dict) else None
            skipped = obj.get('skipped') if isinstance(obj, dict) else None
            if isinstance(skipped, list):
                requested = str(email or '').strip()
                for item in skipped:
                    if not isinstance(item, dict):
                        continue
                    skipped_email = str(item.get('email') or '').strip()
                    if not skipped_email or skipped_email == requested:
                        reason = item.get('reason') or 'client enable was skipped'
                        return False, result, str(reason)
            return ok, result, error
        if not _looks_like_missing_route(error):
            return ok, result, error
    # Either the capability was not proven, or the panel answered "no such route".
    # A full client update is the primitive that exists for every first-class panel.
    return _v3_enable_via_update(server, session_obj, email, enabled_client,
                                 preserved_client=preserved_client)


def _looks_like_missing_route(error) -> bool:
    """True only for the transport-level answers that mean "this route is absent"."""
    text = str(error or '').strip().lower()
    if not text:
        return False
    return (text in {'http 404', 'http 405'}
            or 'status 404' in text
            or 'status 405' in text
            or 'not found' in text
            or 'method not allowed' in text
            or 'unsupported' in text)


def _v3_enable_via_update(server, session_obj, email, enabled_client,
                          *, preserved_client=None):
    """Enable by re-sending the client with ``enable=true`` (capability-safe).

    The fallback re-issues a full client update, which would reset the device
    limit to 0 unless the authoritative value is echoed back, so the read is
    mandatory and a failure to read refuses the write (fail closed) instead of
    silently clearing an operator's device cap.
    """
    snapshot = preserved_client if isinstance(preserved_client, dict) else None
    if snapshot is None:
        _ok, snapshot = read_authoritative_client_settings(server, session_obj, email)
        if not _ok:
            return False, None, (
                "could not read the client's current device limit; refusing to re-enable "
                "because the fallback update would reset it to 0")
    return _v3_post(
        server, session_obj,
        f"/panel/api/clients/update/{quote(email, safe='')}",
        _v3_client_payload(enabled_client,
                            limit_hwid=xui_compat.preserved_limit_hwid(snapshot)))


def v3_delete_client(server, session_obj, email, keep_traffic=False):
    email = _v3_fix_spaced_email(server, session_obj, email)
    path = f"/panel/api/clients/del/{quote(email, safe='')}"
    if keep_traffic:
        path += "?keepTraffic=1"
    return _v3_post(server, session_obj, path, {})


def v3_reset_client(server, session_obj, email):
    email = _v3_fix_spaced_email(server, session_obj, email)
    return _v3_post(server, session_obj,
                    f"/panel/api/clients/resetTraffic/{quote(email, safe='')}", {})


def v3_add_client(server, session_obj, client: dict, inbound_ids: list):
    payload = _v3_client_payload(client)
    ok, result, error = _v3_post(
        server, session_obj, "/panel/api/clients/add",
        {"client": payload, "inboundIds": list(inbound_ids or [])},
    )
    if ok:
        return ok, result, error
    # Some 3x-ui v3 builds return HTTP 200 with no body after Add. That response
    # is ambiguous: older builds sometimes created the client and sometimes
    # aborted inside protocol attachment. Never retry blindly (which can create
    # duplicates); verify the durable client record by email first.
    if error and error.startswith("Empty response (status 200)"):
        email = str(payload.get('email') or '').strip()
        created = _v3_get_client(server, session_obj, email) if email else None
        if created:
            return True, {
                'success': True,
                'obj': {'client': created},
                'verified_after_empty_response': True,
            }, None
        error = f"{error}; client was not found after verification"
    return False, result, error


def v3_attach_client(server, session_obj, email, inbound_ids: list):
    """Use the panel's protocol-aware attach path (notably for WireGuard)."""
    email = _v3_fix_spaced_email(server, session_obj, email)
    return _v3_post(
        server, session_obj,
        f"/panel/api/clients/{quote(email, safe='')}/attach",
        {"inboundIds": list(inbound_ids or [])},
    )


def v3_detach_client(server, session_obj, email, inbound_ids: list):
    email = _v3_fix_spaced_email(server, session_obj, email)
    return _v3_post(
        server, session_obj,
        f"/panel/api/clients/{quote(email, safe='')}/detach",
        {"inboundIds": list(inbound_ids or [])},
    )


# ── Multi-inbound membership reconciliation (v3) ─────────────────────────────
# A v3 client's "inbound membership" is the set of inbounds whose
# settings.clients[] contain that email/uuid. We change membership by editing
# the individual inbounds' client lists and pushing the full inbound back via
# the universal /inbounds/update/:id endpoint — this works on every panel
# version (the per-inbound delClient shortcut was removed in v3, the full
# inbound update was not).

def _push_full_inbound(server, session_obj, inbound_obj, settings_dict):
    """POST a full inbound object back to the panel with updated settings.

    settings_dict replaces the inbound's clients list. JSON sub-fields that v3
    returns already-decoded (settings/streamSettings/sniffing/allocate) must be
    re-encoded to strings, which is what the update endpoint expects.
    """
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import collect_endpoint_templates, INBOUND_UPDATE_FALLBACKS, build_panel_url
    try:
        inbound_id = int(inbound_obj.get('id'))
    except (TypeError, ValueError):
        return False, 'Bad inbound id'
    update_data = dict(inbound_obj)
    update_data['settings'] = json.dumps(settings_dict)
    for k in ('streamSettings', 'sniffing', 'allocate'):
        v = update_data.get(k)
        if isinstance(v, (dict, list)):
            update_data[k] = json.dumps(v)

    errors = []
    for tpl in collect_endpoint_templates(server.panel_type, 'inbounds_update', INBOUND_UPDATE_FALLBACKS):
        up_url = build_panel_url(server.host, tpl, {'id': inbound_id})
        if not up_url:
            continue
        try:
            resp = session_obj.post(up_url, json=update_data, verify=session_tls_verify(session_obj), timeout=(3, 20))
        except Exception as exc:
            errors.append(str(exc))
            continue
        if resp.status_code != 200:
            errors.append(f"HTTP {resp.status_code}")
            continue
        j, err = _safe_response_json(resp)
        if err:
            errors.append(err)
            continue
        if isinstance(j, dict) and j.get('success'):
            return True, None
        errors.append((j.get('msg') or j.get('message')) if isinstance(j, dict) else 'update failed')
    return False, ('; '.join(str(e) for e in errors) or 'inbound update failed')


def _add_client_to_inbound(server, session_obj, inbound_obj, client_dict):
    """Append client_dict to an inbound's clients (no-op if email already there)."""
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import _json_field
    settings = _json_field(inbound_obj.get('settings'), {}) or {}
    settings.setdefault('clients', [])
    email_l = (client_dict.get('email') or '').strip().lower()
    for c in settings['clients']:
        if (c.get('email') or '').strip().lower() == email_l:
            return True, None  # already present
    settings['clients'].append(client_dict)
    return _push_full_inbound(server, session_obj, inbound_obj, settings)


def _remove_client_from_inbound(server, session_obj, inbound_obj, email, client_uuid):
    """Drop a client (by email or uuid) from one inbound's clients list."""
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import _json_field
    settings = _json_field(inbound_obj.get('settings'), {}) or {}
    clients = settings.get('clients') or []
    email_l = (email or '').strip().lower()
    uuid_s = str(client_uuid or '').strip()
    kept = [c for c in clients
            if not (((c.get('email') or '').strip().lower() == email_l and email_l)
                    or (uuid_s and str(c.get('id') or '').strip() == uuid_s))]
    if len(kept) == len(clients):
        return True, None  # nothing to remove
    settings['clients'] = kept
    return _push_full_inbound(server, session_obj, inbound_obj, settings)


def _sync_membership_ownership(user, server, email, client_uuid, added_ids, removed_ids):
    """Keep ClientOwnership rows in step with inbound-membership changes."""
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import ensure_reseller_allowed_for_assignment, invalidate_ownership_cache
    email_l = (email or '').strip().lower()
    uuid_s = str(client_uuid or '').strip()
    key_filter = []
    if uuid_s:
        key_filter.append(ClientOwnership.client_uuid == uuid_s)
    if email_l:
        key_filter.append(func.lower(ClientOwnership.client_email) == email_l)
    if not key_filter:
        return

    existing = ClientOwnership.query.filter(
        ClientOwnership.server_id == server.id, or_(*key_filter)
    ).all()
    owner_id = existing[0].reseller_id if existing else (user.id if user.role == 'reseller' else None)

    for iid in (added_ids or []):
        if owner_id is None:
            continue
        dup = ClientOwnership.query.filter(
            ClientOwnership.reseller_id == owner_id,
            ClientOwnership.server_id == server.id,
            ClientOwnership.inbound_id == iid,
            or_(*key_filter),
        ).first()
        if not dup:
            db.session.add(ClientOwnership(
                reseller_id=owner_id, server_id=server.id, inbound_id=iid,
                client_email=email, client_uuid=(uuid_s or None), price=0))
            try:
                owner = db.session.get(Admin, owner_id)
                if owner:
                    ensure_reseller_allowed_for_assignment(owner, server.id, iid)
            except Exception:
                pass

    for iid in (removed_ids or []):
        ClientOwnership.query.filter(
            ClientOwnership.server_id == server.id,
            ClientOwnership.inbound_id == iid,
            or_(*key_filter),
        ).delete(synchronize_session=False)

    db.session.commit()
    invalidate_ownership_cache()


def _reconcile_client_inbounds(user, server, email, client_uuid, target_inbound_ids, mode='set'):
    """Add/remove a client across a v3 server's inbounds.

    mode 'set'    → membership becomes exactly (target ∩ accessible)
         'add'    → add the target inbounds
         'remove' → remove the target inbounds
    Only inbounds the user can access are ever touched. Refuses to leave the
    client in zero inbounds. Returns (ok, err, status, info).
    """
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import _json_field, _ss_password, get_reseller_access_maps, _has_client_access, is_inbound_accessible, clone_cached_client_into_inbound, remove_cached_client, fetch_and_update_server_data, app
    mode = (mode or 'set').lower()
    if mode not in ('set', 'add', 'remove'):
        mode = 'set'

    if user.role == 'reseller':
        allowed_map, assignments = get_reseller_access_maps(user)
        if not _has_client_access(user, server.id, email, inbound_id=None, client_uuid=client_uuid):
            return False, 'Access denied', 403, None
    else:
        allowed_map, assignments = '*', {}

    def _accessible(iid):
        return user.role != 'reseller' or is_inbound_accessible(server.id, iid, allowed_map, assignments)

    session_obj, error = get_xui_session(server)
    if error:
        return False, error, 400, None

    inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
    if fetch_err:
        return False, 'Failed to fetch inbounds', 502, None
    persist_detected_panel_type(server, detected_type)

    email_l = (email or '').strip().lower()
    uuid_s = str(client_uuid or '').strip()
    membership = {}        # inbound_id -> raw client dict
    inbound_by_id = {}
    for ib in inbounds:
        try:
            iid = int(ib.get('id'))
        except (TypeError, ValueError):
            continue
        inbound_by_id[iid] = ib
        settings = _json_field(ib.get('settings'), {}) or {}
        for c in (settings.get('clients') or []):
            ce = (c.get('email') or '').strip().lower()
            cu = str(c.get('id') or '').strip()
            if (email_l and ce == email_l) or (uuid_s and cu == uuid_s):
                membership[iid] = c
                break

    if not membership:
        return False, 'Client not found on this server', 404, None
    current_ids = set(membership.keys())

    try:
        target_ids = {int(x) for x in (target_inbound_ids or []) if x is not None}
    except (TypeError, ValueError):
        target_ids = set()
    target_ids = {i for i in target_ids if i in inbound_by_id and _accessible(i)}

    if mode == 'add':
        to_add, to_remove = (target_ids - current_ids), set()
    elif mode == 'remove':
        to_add, to_remove = set(), (target_ids & current_ids)
    else:  # set
        to_add = target_ids - current_ids
        to_remove = {i for i in (current_ids - target_ids) if _accessible(i)}

    if not to_add and not to_remove:
        return True, None, 204, {'added': [], 'removed': []}

    final_ids = (current_ids - to_remove) | to_add
    if not final_ids:
        return False, 'Refusing to remove the client from all inbounds — delete the client instead', 400, None

    base_client = dict(next(iter(membership.values())))
    added, removed, errors = [], [], []
    native_membership_used = False

    # Modern v3 panels own the protocol-specific attach logic. This is essential
    # for 3.4.2+ WireGuard, where the panel generates a keypair and allocates an
    # address in the inbound's peer subnet. Older v3 builds that do not expose
    # attach/detach return 404 and fall through to the full-inbound compatibility
    # path below. Validation/runtime errors are not bypassed by that fallback.
    if server_is_v3(server, session_obj):
        if to_add:
            requested_add = sorted(to_add)
            ok_attach, _attach_response, attach_error = v3_attach_client(
                server, session_obj, email, requested_add)
            if ok_attach:
                added.extend(requested_add)
                native_membership_used = True
                to_add = set()
            elif '404' not in str(attach_error or ''):
                errors.append(f"attach: {attach_error or 'panel rejected attach'}")
                to_add = set()
        if to_remove:
            requested_remove = sorted(to_remove)
            ok_detach, _detach_response, detach_error = v3_detach_client(
                server, session_obj, email, requested_remove)
            if ok_detach:
                removed.extend(requested_remove)
                native_membership_used = True
                to_remove = set()
            elif '404' not in str(detach_error or ''):
                errors.append(f"detach: {detach_error or 'panel rejected detach'}")
                to_remove = set()

    for iid in sorted(to_add):
        ib = inbound_by_id[iid]
        clone = dict(base_client)
        proto = (ib.get('protocol') or '').lower()
        ib_settings = _json_field(ib.get('settings'), {}) or {}
        if proto == 'shadowsocks':
            method = ib_settings.get('method') or clone.get('method') or 'chacha20-ietf-poly1305'
            clone['method'] = method
            clone['password'] = clone.get('password') or _ss_password(method)
        elif proto == 'trojan':
            clone['password'] = clone.get('password') or secrets.token_urlsafe(16)
        ok_add, aerr = _add_client_to_inbound(server, session_obj, ib, clone)
        (added.append(iid) if ok_add else errors.append(f"add#{iid}: {aerr}"))

    for iid in sorted(to_remove):
        ib = inbound_by_id[iid]
        ok_rm, rerr = _remove_client_from_inbound(server, session_obj, ib, email, base_client.get('id'))
        (removed.append(iid) if ok_rm else errors.append(f"remove#{iid}: {rerr}"))

    try:
        _sync_membership_ownership(user, server, email, base_client.get('id'), added, removed)
    except Exception:
        db.session.rollback()

    # Native attach may have generated protocol credentials (WireGuard key/IP),
    # so refresh authoritative data instead of cloning a stale VLESS-style row.
    try:
        if added or removed:
            # Panel state changed regardless of whether a local cached row exists.
            bump_server_revision(server.id)
            # Phase 10: this membership write happened outside the cached-client
            # helpers, so mark the panel hot here too.
            try:
                from panel.core import refresh_policy
                refresh_policy.note_server_activity(server.id)
            except Exception:
                pass
        if native_membership_used:
            fetch_and_update_server_data(server.id)
        else:
            with serialized_server_snapshot_write(server.id):
                for _iid in added:
                    clone_cached_client_into_inbound(server.id, _iid, email,
                                                     client_uuid=base_client.get('id'), publish=False)
                for _iid in removed:
                    remove_cached_client(server.id, email, client_uuid=base_client.get('id'),
                                         inbound_id=_iid, publish=False)
                if added or removed:
                    publish_snapshot_to_redis([server.id])
    except Exception:
        app.logger.warning(
            "Client inbound membership cache sync failed (server_id=%s, email=%s)",
            server.id, email, exc_info=True,
        )

    if errors and not added and not removed:
        return False, '; '.join(errors), 502, None
    return True, ('; '.join(errors) or None), 200, {'added': added, 'removed': removed}


def _autoupgrade_http_to_https(server):
    """Self-heal an http:// host that is really an HTTPS-only panel.

    3x-ui panels with SSL enabled (they send HSTS + Secure cookies) reject plaintext
    on the TLS port — Python's requests raises ConnectionError('UnknownProtocol'),
    which surfaces in the UI as a generic "Error testing connection". This commonly
    bites after a panel upgrade where the admin also turned SSL on at the same time.

    If the stored host is http://, probe the same host over https. Only when https
    answers AND http does not do we rewrite server.host to https and persist it.
    Panels that genuinely run plaintext are left untouched (the https probe fails, so
    no change is made). Returns True when the host was upgraded.
    """
    try:
        host = (getattr(server, 'host', '') or '').strip()
    except Exception:
        host = ''
    if not host.lower().startswith('http://'):
        return False
    base, webpath = extract_base_and_webpath(host)
    https_base = 'https://' + base[len('http://'):]
    probe_path = f"{webpath}/" if webpath else '/'

    def _reaches(b):
        try:
            r = requests.get(f"{b}{probe_path}", timeout=6,
                             verify=panel_tls_verify(server), allow_redirects=False)
            return r.status_code < 500
        except Exception:
            return False

    if not _reaches(https_base):
        return False          # panel is not reachable over https — leave http as-is
    if _reaches(base):
        return False          # http works too; don't second-guess the operator
    try:
        server.host = https_base + webpath
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return False
    try:
        XUI_SESSION_CACHE.pop(server.id, None)  # drop any session built on the old scheme
        XUI_CAPABILITY_CACHE.pop(server.id, None)
    except Exception:
        pass
    return True


def _fetch_csrf_token(session_obj, base, webpath):
    """Seed and pin a CSRF token for cookie-login panels (3x-ui v3.3.1+).

    Starting with 3x-ui v3.3.1 the refactor (#5167) guards POST /login — and every
    other state-changing browser route (logout, getTwoFactorEnable, and the
    cookie-session /panel/api/* POSTs) — with a CSRF middleware. Requests without a
    valid token are rejected with HTTP 403, which is exactly why EVE could no longer
    log in to upgraded panels.

    The token lives in the server-side session (cookie '3x-ui'). A public
    GET {basePath}/csrf-token both seeds that session cookie and returns the token as
    {"success": true, "obj": "<token>"}. The panel reads it back from the
    'X-CSRF-Token' header (or a '_csrf' form field). Login does NOT rotate the
    session in v3.3.1, so a token fetched here stays valid for the subsequent login
    and for every later API POST made through the same requests.Session.

    We pin it as a default header on the session so all later calls carry it
    automatically. Backward compatible by construction:
      • Older panels (<=3.3.0, v3, pre-v3) have no /csrf-token route — the GET 404s,
        we skip the header, and those panels harmlessly ignore the unknown header.
      • Bearer/API-token servers never reach this path (they short-circuit earlier and
        CSRF is bypassed for api_authed requests).

    Returns the token string, or None when unavailable. Failures are non-fatal —
    login is still attempted without the header for maximum compatibility.
    """
    try:
        url = f"{base}{webpath}/csrf-token"
        resp = session_obj.get(url, timeout=8, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return None
        j, err = _safe_response_json(resp)
        if err or not isinstance(j, dict) or not j.get('success'):
            return None
        token = j.get('obj')
        if isinstance(token, str) and token:
            session_obj.headers.update({'X-CSRF-Token': token})
            return token
    except Exception:
        pass
    return None


def get_xui_session(server):
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import app, get_server_password
    # Never build a credential-bearing session for a plaintext remote panel
    # unless this server opted in.
    allow_insecure = bool(getattr(server, 'allow_insecure', False))
    try:
        enforce_panel_transport(getattr(server, 'host', '') or '',
                                allow_insecure=allow_insecure)
    except InsecurePanelTransportError as exc:
        return None, str(exc)
    # Current auth identity: the token for v3, or '' for cookie-login panels.
    # Cached sessions are keyed to this so a server that just switched to v3
    # (token added) doesn't keep returning a stale, token-less cookie session
    # — which the v3 panel rejects with 403. This is per-worker, so the cache
    # self-heals on the next call in each gunicorn worker.
    _api_token = get_server_api_token(server)
    # The transport policy is part of the identity: flipping allow_insecure must
    # never reuse a session that was built (and probed) under the other policy.
    _auth_key = "%s|%s" % (_api_token or '', 'insecure' if allow_insecure else 'verified')

    # Try to reuse session from cache
    now = time.time()
    if server.id in XUI_SESSION_CACHE:
        cached = XUI_SESSION_CACHE[server.id]
        if now < cached['expiry'] and cached.get('auth_key', '') == _auth_key:
            return cached['session'], None
        else:
            XUI_SESSION_CACHE.pop(server.id, None)

    session_obj = requests.Session()
    session_obj.trust_env = False
    session_obj.proxies = {'http': None, 'https': None}
    # The per-server policy applies to redirects and every call that inherits
    # Session.verify; session_tls_verify() reads it back for explicit verify=.
    session_obj.verify = panel_tls_verify(server)

    # ── 3x-ui v3+ : authenticate with the API token (Bearer) ──
    # The token bypasses the v3 login CSRF guard and never expires, so we attach
    # it to the session and skip the cookie-login dance entirely.
    if _api_token:
        session_obj.headers.update({'Authorization': f'Bearer {_api_token}'})
        _probe_v3_client_api(server, session_obj, force=True)
        XUI_SESSION_CACHE[server.id] = {'session': session_obj, 'expiry': now + XUI_SESSION_TTL, 'auth_key': _auth_key}
        return session_obj, None

    try:
        base, webpath = extract_base_and_webpath(server.host)
        normalized_type = (getattr(server, 'panel_type', None) or 'auto').strip().lower()
        panel_api = get_panel_api(normalized_type)
        login_ep = (getattr(panel_api, 'login_endpoint', None) if panel_api else None) or '/login'
        login_url = login_ep if login_ep.startswith('http') else f"{base}{webpath}{login_ep}"
        panel_password = get_server_password(server)
        credentials = {"username": server.username, "password": panel_password}

        # 3x-ui v3.3.1+ guards POST /login with a CSRF middleware (403 without a
        # token). Seed + pin the token now so the login POST below — and every later
        # cookie-session API POST through this same session — carries X-CSRF-Token.
        # No-op on older panels (the /csrf-token route 404s).
        _fetch_csrf_token(session_obj, base, webpath)

        login_resp = None
        login_json = None
        last_err = None

        # Try JSON body first (3x-ui v3.0.0+), then form-encoded (older panels)
        for use_json in (True, False):
            try:
                if use_json:
                    resp = session_obj.post(
                        login_url,
                        json=credentials,
                        timeout=8,
                        headers={"Accept": "application/json"},
                    )
                else:
                    resp = session_obj.post(login_url, data=credentials, timeout=8)

                j, err = _safe_response_json(resp)
                if err:
                    last_err = err
                    continue
                login_resp = resp
                login_json = j
                last_err = None
                if isinstance(j, dict) and j.get('success'):
                    break
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_err = _format_panel_connection_error(server, exc)
                app.logger.warning(
                    "Panel login connection failed for server %s (%s): %s",
                    getattr(server, 'id', None),
                    getattr(server, 'host', None),
                    exc,
                )
                break
            except Exception as exc:
                last_err = str(exc)
                continue

        if login_resp is None:
            return None, last_err or _format_panel_connection_error(server)

        if login_resp.status_code == 200 and isinstance(login_json, dict) and login_json.get('success'):
            XUI_SESSION_CACHE[server.id] = {
                'session': session_obj,
                'expiry': now + XUI_SESSION_TTL,
                'auth_key': _auth_key,
            }
            # Cookie login is fully supported by v3. Detect the API generation
            # now so every later mutation chooses the correct endpoint family.
            _probe_v3_client_api(server, session_obj, force=True)
            return session_obj, None

        msg = None
        if isinstance(login_json, dict):
            msg = login_json.get('msg') or login_json.get('message')
        return None, f"Login failed: {login_resp.status_code}{(' - ' + str(msg)) if msg else ''}"
    except Exception as e:
        return None, f"Error: {str(e)}"

def persist_detected_panel_type(server, detected_type: str) -> bool:
    """Persist detected panel type for a Server.

    Only updates when current type is auto/unset to avoid overriding a deliberate manual choice.
    Returns True if updated.
    """
    try:
        if not server:
            return False
        detected = (detected_type or '').strip().lower()
        if not detected or detected == 'auto':
            return False
        current = (getattr(server, 'panel_type', None) or 'auto').strip().lower()
        if current not in ('', 'auto'):
            return False
        if current == detected:
            return False
        server.panel_type = detected
        db.session.commit()
        return True
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return False

def fetch_inbounds(session_obj, host, panel_type='auto', *, force_fresh=False):
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import app
    base, webpath = extract_base_and_webpath(host)
    timeout_sec = 3
    normalized_type = (panel_type or 'auto').strip().lower()

    # Build a prioritized endpoint map: [(endpoint, detected_panel_type)]
    endpoints_map = []

    # If panel_type is known, try only its configured endpoint first
    panel_api = get_panel_api(normalized_type)
    if normalized_type != 'auto' and panel_api and panel_api.inbounds_list:
        endpoints_map.append((panel_api.inbounds_list, normalized_type))
    else:
        # Auto-discovery: try known panel APIs first (prefer sanaei)
        try:
            all_apis = PanelAPI.query.all()
            # Release the read lock before starting network I/O
            db.session.commit()
        except Exception:
            all_apis = []

        def _api_sort_key(api: 'PanelAPI'):
            pt = (getattr(api, 'panel_type', '') or '').lower()
            if pt == 'sanaei':
                return (0, pt)
            if pt == 'alireza':
                return (1, pt)
            return (2, pt)

        for api in sorted(all_apis, key=_api_sort_key):
            ep = getattr(api, 'inbounds_list', None)
            pt = (getattr(api, 'panel_type', None) or '').strip().lower()
            if ep and pt:
                endpoints_map.append((ep, pt))

        # Hardcoded fallbacks (covers older panels / missing PanelAPI rows)
        endpoints_map.extend([
            ("/panel/api/inbounds/list", "sanaei"),
            ("/xui/API/inbounds/", "alireza"),
            ("/xui/inbound/list", "xui"),
        ])

    # De-duplicate while preserving order
    seen = set()
    deduped = []
    for ep, pt in endpoints_map:
        if not ep:
            continue
        key = (ep, pt)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((ep, pt))

    last_error = None
    for ep, detected_type in deduped:
        try:
            url = ep if ep.startswith('http') else f"{base}{webpath}{ep}"
            ep_l = ep.lower()
            request_headers = None
            request_params = None
            if force_fresh:
                # Several 3x-ui builds (and reverse proxies in front of them)
                # cache the inbound list briefly.  A renew read-after-write must
                # never verify against that stale representation.
                request_headers = {
                    'Cache-Control': 'no-store, no-cache, max-age=0',
                    'Pragma': 'no-cache',
                }
                request_params = {'_eve_ts': str(time.time_ns())}

            # Request strategy per panel flavor
            if '/xui/' in ep_l and 'api' in ep_l:
                resp = session_obj.get(url, headers=request_headers, params=request_params,
                                       verify=session_tls_verify(session_obj), timeout=timeout_sec)
                if resp.status_code == 405:
                    resp = session_obj.post(url, headers=request_headers, params=request_params,
                                            verify=session_tls_verify(session_obj), timeout=timeout_sec)
            elif '/xui/' in ep_l:
                resp = session_obj.post(url, json={"page": 1, "limit": 100},
                                        headers=request_headers, params=request_params,
                                        verify=session_tls_verify(session_obj), timeout=timeout_sec)
            else:
                resp = session_obj.get(url, headers=request_headers, params=request_params,
                                       verify=session_tls_verify(session_obj), timeout=timeout_sec)

            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code} from {ep}"
                continue

            data = resp.json()
            if not isinstance(data, dict) or not data.get('success'):
                last_error = f"Panel returned success=false from {ep}"
                continue

            if 'obj' in data:
                return data['obj'], None, detected_type
            if 'data' in data:
                d = data['data']
                return (d if isinstance(d, list) else d.get('list', [])), None, detected_type
        except Exception as e:
            last_error = str(e)
            app.logger.debug("Failed inbounds endpoint %s: %s", ep, last_error)
            continue

    return None, (last_error or "Failed to fetch inbounds from all known endpoints"), 'auto'


XUI_COOKIE_SESSION_CACHE = {}  # cache_key -> {'session': requests.Session, 'expiry': float}


def get_xui_cookie_session(host, username, password, panel_type='auto', cache_key=None,
                           allow_insecure=False):
    """Return a COOKIE-authenticated session (username/password login).

    v3 panels are normally accessed with a Bearer API token, but some panel
    routes — notably the web-UI `/panel/inbound/onlines` — are NOT exposed on
    the token-authenticated API router and return 404 unless you present a
    valid login cookie. This logs in and caches the cookie session.
    """
    if not username or not password:
        return None
    try:
        enforce_panel_transport(host, allow_insecure=bool(allow_insecure))
    except InsecurePanelTransportError:
        return None
    now = time.time()
    # The transport policy is part of the cache identity: a session built under
    # one policy must never be reused after the flag flips.
    policy_key = 'insecure' if allow_insecure else 'verified'
    ck = cache_key or f"{host}|{username}|{policy_key}"
    cached = XUI_COOKIE_SESSION_CACHE.get(ck)
    if cached and now < cached['expiry']:
        return cached['session']

    try:
        base, webpath = extract_base_and_webpath(host)
        normalized_type = (panel_type or 'auto').strip().lower()
        panel_api = get_panel_api(normalized_type)
        login_ep = (getattr(panel_api, 'login_endpoint', None) if panel_api else None) or '/login'
        login_url = login_ep if login_ep.startswith('http') else f"{base}{webpath}{login_ep}"

        s = requests.Session()
        s.trust_env = False
        s.proxies = {'http': None, 'https': None}
        # Host-only callers have no server row; express the same per-server policy
        # so the verify value still comes from the one central helper.
        s.verify = panel_tls_verify(SimpleNamespace(allow_insecure=allow_insecure))
        creds = {"username": username, "password": password}
        # v3.3.1+ CSRF guard: pin a token before the login POST so both /login and
        # the later /panel/inbound/onlines POST (made through this same session)
        # pass the middleware. No-op on older panels.
        _fetch_csrf_token(s, base, webpath)
        for use_json in (True, False):
            try:
                if use_json:
                    r = s.post(login_url, json=creds, timeout=8, headers={"Accept": "application/json"})
                else:
                    r = s.post(login_url, data=creds, timeout=8)
                j, err = _safe_response_json(r)
                if r.status_code == 200 and isinstance(j, dict) and j.get('success'):
                    XUI_COOKIE_SESSION_CACHE[ck] = {'session': s, 'expiry': now + XUI_SESSION_TTL}
                    return s
            except Exception:
                continue
    except Exception:
        pass
    return None


def fetch_onlines(session_obj, host, panel_type='auto'):
    """Fetch online clients from panel (best-effort).

    Returns (index, error) where index is:
      {"pairs": set[(inbound_id_norm, email_lower)], "emails": set[email_lower]}
    """
    # Deferred import: lives in app.py (module-level import would be circular)
    from app import app
    index = {"pairs": set(), "emails": set()}

    try:
        base, webpath = extract_base_and_webpath(host)
        timeout_sec = 3
        normalized_type = (panel_type or 'auto').strip().lower()

        # Online endpoints:
        # - 3x-ui (Sanaei): base /panel/api/inbounds, method POST /onlines
        # - x-ui (alireza0): base /xui/API/inbounds, method POST /onlines
        # Some installs may also allow GET; keep as fallback.
        candidates = []
        if normalized_type in ('sanaei', 'auto', ''):
            candidates.extend([
                # Official 3x-ui v3 API path (Bearer token works on /panel/api/*)
                ('POST', '/panel/api/clients/onlines'),
                ('GET',  '/panel/api/clients/onlines'),
                # Fallbacks for older builds
                ('POST', '/panel/inbound/onlines'),
                ('POST', '/panel/api/inbounds/onlines'),
            ])
        if normalized_type in ('alireza', 'alireza0', 'xui', 'x-ui', 'auto', ''):
            candidates.extend([
                ('POST', '/xui/API/inbounds/onlines'),
                ('POST', '/xui/inbound/onlines'),
                ('POST', '/xui/api/inbounds/onlines'),
                ('GET', '/xui/API/inbounds/onlines'),
                ('GET', '/xui/api/inbounds/onlines'),
            ])

        last_error = None
        last_status = None

        for method, ep in candidates:
            try:
                url = ep if ep.startswith('http') else f"{base}{webpath}{ep}"
                if method == 'POST':
                    resp = session_obj.post(url, json={}, verify=session_tls_verify(session_obj), timeout=timeout_sec)
                else:
                    resp = session_obj.get(url, verify=session_tls_verify(session_obj), timeout=timeout_sec)

                last_status = resp.status_code
                try:
                    _body_snippet = re.sub(r'\s+', ' ', (resp.text or ''))[:160]
                    _srv_hdr = resp.headers.get('Server', '?')
                    _ct = resp.headers.get('Content-Type', '?')
                    app.logger.info("[onlines] %s %s -> HTTP %s [Server=%s, CT=%s]: %s", method, url, resp.status_code, _srv_hdr, _ct, _body_snippet)
                except Exception:
                    pass
                if resp.status_code != 200:
                    continue

                data = resp.json()

                # Response shapes vary:
                # - {success: true, obj: [...]} or {success: true, data: {...}}
                # - plain list of emails
                # - dict with a nested list
                obj = None
                if isinstance(data, dict):
                    # Many panels use 'success' flag; if present and false, skip.
                    if 'success' in data and not data.get('success'):
                        continue
                    obj = data.get('obj')
                    if obj is None:
                        obj = data.get('data')
                elif isinstance(data, list):
                    obj = data
                else:
                    continue

                items = []
                if isinstance(obj, list):
                    items = obj
                elif isinstance(obj, dict):
                    for k in ('onlines', 'list', 'data', 'clients'):
                        v = obj.get(k)
                        if isinstance(v, list):
                            items = v
                            break

                for item in items or []:
                    email = None
                    inbound_id = None
                    if isinstance(item, str):
                        email = item
                    elif isinstance(item, dict):
                        email = item.get('email') or item.get('user') or item.get('username')
                        inbound_id = item.get('inboundId')
                        if inbound_id is None:
                            inbound_id = item.get('inbound_id')

                    email_l = (str(email or '').strip().lower())
                    if not email_l:
                        continue

                    if inbound_id is not None:
                        try:
                            inbound_id_norm = int(inbound_id)
                        except Exception:
                            inbound_id_norm = str(inbound_id)
                        index['pairs'].add((inbound_id_norm, email_l))
                    else:
                        index['emails'].add(email_l)

                try:
                    app.logger.info(
                        f"[onlines] {normalized_type} {method} {ep} -> "
                        f"{len(index['pairs'])} pairs, {len(index['emails'])} emails"
                    )
                except Exception:
                    pass
                return index, None
            except Exception as e:
                last_error = str(e)
                continue

        # If we tried endpoints but none worked, return a hint (caller still treats it best-effort).
        if candidates:
            hint = last_error or (f"HTTP {last_status}" if last_status is not None else "No response")
            try:
                app.logger.warning("[onlines] all endpoints failed (%s): %s", normalized_type, hint)
            except Exception:
                pass
            return index, f"Failed to fetch onlines ({normalized_type}): {hint}"

        return index, None
    except Exception as e:
        return index, str(e)


def _pick_first_value(payload: dict, keys: list[str]):
    for key in keys:
        if key in payload and payload.get(key) not in (None, ''):
            return payload.get(key)
    return None


def _normalize_server_status_payload(payload: dict) -> dict:
    """Extract useful info from the panel /status API response.

    Note: The /status endpoint returns system stats (CPU, mem, disk, xray info).
    It does NOT return xui_version or online_count - those come from elsewhere.
    """
    if not isinstance(payload, dict):
        return {}

    xray_info = payload.get('xray') if isinstance(payload.get('xray'), dict) else {}

    # 'panelVersion' is the 3x-ui v3+ field for the panel version (e.g. "3.2.8").
    xui_version = _pick_first_value(payload, ['xui_version', 'xuiVersion', 'xui', 'panelVersion'])
    if not xui_version and isinstance(payload.get('version'), str):
        xui_version = payload.get('version')

    xray_version = _pick_first_value(payload, ['xray_version', 'xrayVersion'])
    if not xray_version and isinstance(xray_info, dict):
        xray_version = _pick_first_value(xray_info, ['version', 'xray_version', 'xrayVersion'])

    # Xray state: running / stop / error (Sanaei uses lowercase, Alireza uses capitalized)
    xray_state = None
    if isinstance(xray_info, dict):
        raw_state = _pick_first_value(xray_info, ['state', 'State'])
        if raw_state:
            xray_state = str(raw_state).lower()  # normalize to lowercase

    xray_core = _pick_first_value(payload, ['core', 'xray_core', 'xrayCore', 'arch', 'architecture'])
    if not xray_core and isinstance(xray_info, dict):
        xray_core = _pick_first_value(xray_info, ['core', 'arch', 'architecture'])

    online = _pick_first_value(payload, ['online', 'onlineCount', 'online_count'])
    try:
        online_count = int(online) if online is not None else None
    except Exception:
        online_count = None

    return {
        'xui_version': xui_version,
        'xray_version': xray_version,
        'xray_state': xray_state,
        'xray_core': xray_core,
        'online_count': online_count
    }


def fetch_server_status(session_obj, host, panel_type='auto'):
    base, webpath = extract_base_and_webpath(host)
    timeout_sec = 5
    normalized_type = (panel_type or 'auto').strip().lower()

    endpoints = []
    panel_api = get_panel_api(normalized_type)
    if normalized_type != 'auto' and panel_api and panel_api.server_status:
        endpoints.append((panel_api.server_status, normalized_type))
    else:
        try:
            all_apis = PanelAPI.query.all()
            # Release the read lock before starting network I/O
            db.session.commit()
        except Exception:
            all_apis = []

        def _api_sort_key(api: 'PanelAPI'):
            pt = (getattr(api, 'panel_type', '') or '').lower()
            if pt == 'sanaei':
                return (0, pt)
            if pt == 'alireza':
                return (1, pt)
            return (2, pt)

        for api in sorted(all_apis, key=_api_sort_key):
            ep = getattr(api, 'server_status', None)
            pt = (getattr(api, 'panel_type', None) or '').strip().lower()
            if ep and pt:
                endpoints.append((ep, pt))

        endpoints.extend([
            ('/panel/api/server/status', 'sanaei'),
            ('/xui/API/server/status', 'alireza'),
        ])

    # Add non-API fallback paths (some older panel versions only expose these)
    if normalized_type in ('alireza', 'alireza0', 'xui', 'x-ui', 'auto', ''):
        endpoints.append(('/server/status', 'alireza'))

    seen = set()
    deduped = []
    for ep, pt in endpoints:
        if not ep:
            continue
        key = (ep, pt)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((ep, pt))

    last_error = None
    for ep, detected_type in deduped:
        try:
            url = ep if ep.startswith('http') else f"{base}{webpath}{ep}"
            resp = session_obj.get(url, verify=session_tls_verify(session_obj), timeout=timeout_sec, allow_redirects=False)

            # Redirect usually means session expired -> redirected to login page
            if resp.status_code in (301, 302, 303, 307, 308):
                last_error = f"Redirect {resp.status_code} (session may have expired)"
                continue

            if resp.status_code == 404:
                # Sanaei returns 404 for unauthenticated API calls, or endpoint doesn't exist
                last_error = f"HTTP 404 (endpoint may not exist in this panel version)"
                continue

            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                continue

            data, err = _safe_response_json(resp)
            if err:
                last_error = err
                continue
            if isinstance(data, dict) and data.get('success') is False:
                last_error = data.get('msg') or data.get('message') or 'Status failed'
                continue

            payload = None
            if isinstance(data, dict):
                obj_val = data.get('obj')
                # Handle null/None obj (e.g. Alireza panel lazy-load: status not ready yet)
                if obj_val is not None and isinstance(obj_val, dict):
                    payload = obj_val
                elif obj_val is None:
                    # obj is null, status not ready yet - return empty but successful
                    return {}, None, detected_type
                else:
                    payload = data.get('data') or data

            normalized = _normalize_server_status_payload(payload if isinstance(payload, dict) else {})
            return normalized, None, detected_type
        except requests.exceptions.Timeout:
            last_error = f"Connection timeout ({timeout_sec}s)"
            continue
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {str(e)[:100]}"
            continue
        except Exception as e:
            last_error = str(e)[:150]
            continue

    return None, last_error or 'Failed to fetch status', 'auto'


def resolve_server_compatibility(server, session_obj=None, status_payload=None, *,
                                 force=False, server_id=None):
    """Resolve and cache which compatibility profile this panel gets.

    Local-first by design: the panel's own build identity (panelVersion on
    /server/status) is authoritative and needs no outbound internet from the
    panel. The GitHub-backed update endpoint is corroboration only, because it
    returns no version at all when the panel cannot reach GitHub - which is
    exactly the situation on the panels this feature exists for.

    Never guesses: an unusable version resolves to the baseline profile with an
    explicit unknown warning, and no version-specific behaviour is attempted.
    """
    sid = server_id
    if sid is None:
        try:
            sid = int(getattr(server, "id"))
        except (TypeError, ValueError):
            sid = None
    if not force and sid is not None:
        cached = xui_compat.cached_compatibility(sid)
        if cached is not None:
            return cached

    version_raw = None
    if isinstance(status_payload, dict):
        version_raw = status_payload.get("xui_version")

    if version_raw not in (None, ""):
        return xui_compat.resolve_compatibility(
            sid, version_raw,
            source=xui_compat.SOURCE_SERVER_STATUS,
            confidence=xui_compat.CONF_AUTHORITATIVE,
        )

    # Corroborating source. Its failure must not degrade anything.
    if session_obj is not None:
        corroborated = _fetch_panel_update_version(server, session_obj)
        if corroborated not in (None, ""):
            return xui_compat.resolve_compatibility(
                sid, corroborated,
                source=xui_compat.SOURCE_PANEL_UPDATE_INFO,
                confidence=xui_compat.CONF_CORROBORATED,
            )

    return xui_compat.resolve_compatibility(sid, None)


def _fetch_panel_update_version(server, session_obj):
    """Best-effort panel version from the GitHub-backed update endpoint.

    Returns None on any failure. The panel answers success:false with no obj when
    it cannot reach GitHub, so absence here is normal and expected - it is never
    treated as evidence about the version.
    """
    base, webpath = extract_base_and_webpath(server.host)
    url = "%s%s/panel/api/server/getPanelUpdateInfo" % (base, webpath)
    try:
        resp = session_obj.get(
            url,
            headers={"Accept": "application/json"},
            verify=session_tls_verify(session_obj),
            timeout=(3, 10),
        )
    except Exception:
        return None
    payload, parse_error = _safe_response_json(resp)
    if parse_error or not isinstance(payload, dict) or not payload.get("success"):
        return None
    obj = payload.get("obj")
    if not isinstance(obj, dict):
        return None
    value = obj.get("currentVersion")
    return value if isinstance(value, str) and value.strip() else None


def fetch_direct_link_from_subscription(sub_url: str, fallback_func=None, fallback_args=None,
                                        server=None) -> str:
    """
    Fetch the direct config link from the upstream X-UI subscription endpoint.
    Returns the first config line, or falls back to manual generation if fetch fails.

    ``server`` is optional: when given, the subscription endpoint inherits that
    panel transport policy (an allow_insecure panel may expose it over plaintext or
    with a self-signed certificate).
    """
    direct_link = None
    try:
        resp = requests.get(
            sub_url, 
            headers={'User-Agent': 'v2rayng'}, 
            timeout=5, 
            verify=panel_tls_verify(server),
            allow_redirects=False
        )
        if resp.status_code == 200:
            raw_content = resp.content or b''
            try:
                decoded = base64.b64decode(raw_content).decode('utf-8')
            except Exception:
                decoded = raw_content.decode('utf-8', errors='ignore')
            configs = [line.strip() for line in decoded.splitlines() if line.strip()]
            if configs:
                direct_link = configs[0]
    except Exception:
        pass
    
    # Fallback to manual generation
    if not direct_link and fallback_func and fallback_args:
        try:
            direct_link = fallback_func(*fallback_args)
        except Exception:
            pass
    
    return direct_link
