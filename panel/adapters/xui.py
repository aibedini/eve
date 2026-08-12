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
from urllib.parse import quote

import requests
from sqlalchemy import func, or_

from panel.core.redis_client import publish_snapshot_to_redis
from panel.extensions import db
from panel.models import (
    Admin,
    ClientOwnership,
    PanelAPI,
    Transaction,
    get_panel_api,
)

# Session cache for X-UI panels to speed up API calls
XUI_SESSION_CACHE = {}  # server_id -> {'session': requests.Session, 'expiry': float}
XUI_SESSION_TTL = 600  # 10 minutes cache
XUI_CAPABILITY_CACHE = {}  # server_id -> {'v3_clients': bool, 'expiry': float}
XUI_CAPABILITY_TTL = 600

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


def _probe_v3_client_api(server, session_obj, *, force=False) -> bool:
    """Detect the first-class v3 client API by capability, not credentials.

    API tokens are a strong v3 signal, but cookie-authenticated v3 panels expose
    the same API and must not be sent to the removed legacy updateClient routes.
    The deliberately missing email keeps the probe side-effect free and small.
    A JSON response from the route (including "client not found") proves the
    controller exists; a 404/HTML login page means it does not.
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
        resp = session_obj.get(url, verify=False, timeout=(3, 8),
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
            verify=False,
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


def _v3_client_payload(client: dict) -> dict:
    """Shape a client dict for v3 /clients/update|add. v3 unmarshals Client.id as a
    string, so `id` must carry the UUID (not the numeric DB row id). Numeric fields
    must be numbers, not empty strings."""
    c = dict(client or {})
    uid = c.get('uuid') or c.get('id') or ''
    if uid:
        c['id'] = uid
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
            verify=False,
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
    ok, j, _err = _v3_get(server, session_obj,
                          f"/panel/api/clients/get/{quote(str(email or ''), safe='')}")
    if not ok or not isinstance(j, dict):
        return None
    obj = j.get('obj')
    if not isinstance(obj, dict):
        return None
    inner = obj.get('client')
    if isinstance(inner, dict) and inner.get('email'):
        return inner
    return obj if obj.get('email') else None


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
        app.logger.debug(f"ownership rename '{old_email}' -> '{new_email}' failed: {exc}")
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
        app.logger.debug(f"transaction email rename '{old_email}' -> '{new_email}' failed: {exc}")
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
        app.logger.warning(f"v3: could not strip spaces from client email '{original}'")
        return original
    _rename_client_email_local(server, original, clean)
    app.logger.info(f"v3: client email '{original}' renamed to '{clean}' (v3 rejects spaces)")
    return clean


def v3_update_client(server, session_obj, email, client: dict):
    email = _v3_fix_spaced_email(server, session_obj, email, client_obj=client)
    return _v3_post(server, session_obj,
                    f"/panel/api/clients/update/{quote(email, safe='')}",
                    _v3_client_payload(client))


def v3_enable_client(server, session_obj, email, client: dict):
    """Force a v3 client active across panel versions.

    Newer panels expose ``bulkEnable``, which also synchronizes the running
    Xray state. Older v3 panels do not have that endpoint, so fall back to the
    traditional full-client update with ``enable=True``.
    """
    enabled_client = dict(client or {})
    enabled_client['enable'] = True
    email = _v3_fix_spaced_email(
        server, session_obj, email, client_obj=enabled_client,
    )
    ok, result, error = _v3_post(
        server, session_obj, "/panel/api/clients/bulkEnable",
        {"emails": [email]},
    )
    if ok:
        return ok, result, error

    unavailable = str(error or '').strip().lower()
    if (
        unavailable in {'http 404', 'http 405'}
        or 'status 404' in unavailable
        or 'status 405' in unavailable
        or 'not found' in unavailable
        or 'method not allowed' in unavailable
        or 'unsupported' in unavailable
    ):
        return _v3_post(
            server, session_obj,
            f"/panel/api/clients/update/{quote(email, safe='')}",
            _v3_client_payload(enabled_client),
        )
    return ok, result, error


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
            resp = session_obj.post(up_url, json=update_data, verify=False, timeout=(3, 20))
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
    from app import _json_field, _ss_password, get_reseller_access_maps, _has_client_access, is_inbound_accessible, clone_cached_client_into_inbound, remove_cached_client, fetch_and_update_server_data
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
        if native_membership_used:
            fetch_and_update_server_data(server.id)
        else:
            for _iid in added:
                clone_cached_client_into_inbound(server.id, _iid, email,
                                                 client_uuid=base_client.get('id'), publish=False)
            for _iid in removed:
                remove_cached_client(server.id, email, client_uuid=base_client.get('id'),
                                     inbound_id=_iid, publish=False)
            if added or removed:
                publish_snapshot_to_redis([server.id])
    except Exception:
        pass

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
            r = requests.get(f"{b}{probe_path}", timeout=6, verify=False, allow_redirects=False)
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
    # Current auth identity: the token for v3, or '' for cookie-login panels.
    # Cached sessions are keyed to this so a server that just switched to v3
    # (token added) doesn't keep returning a stale, token-less cookie session
    # — which the v3 panel rejects with 403. This is per-worker, so the cache
    # self-heals on the next call in each gunicorn worker.
    _api_token = get_server_api_token(server)
    _auth_key = _api_token or ''

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
    # Disable SSL verification at session level so redirects also skip cert checks
    # (self-signed certs on remote panels are supported this way)
    session_obj.verify = False

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

def fetch_inbounds(session_obj, host, panel_type='auto'):
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

            # Request strategy per panel flavor
            if '/xui/' in ep_l and 'api' in ep_l:
                resp = session_obj.get(url, verify=False, timeout=timeout_sec)
                if resp.status_code == 405:
                    resp = session_obj.post(url, verify=False, timeout=timeout_sec)
            elif '/xui/' in ep_l:
                resp = session_obj.post(url, json={"page": 1, "limit": 100}, verify=False, timeout=timeout_sec)
            else:
                resp = session_obj.get(url, verify=False, timeout=timeout_sec)

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
            app.logger.debug(f"Failed inbounds endpoint {ep}: {last_error}")
            continue

    return None, (last_error or "Failed to fetch inbounds from all known endpoints"), 'auto'


XUI_COOKIE_SESSION_CACHE = {}  # cache_key -> {'session': requests.Session, 'expiry': float}


def get_xui_cookie_session(host, username, password, panel_type='auto', cache_key=None):
    """Return a COOKIE-authenticated session (username/password login).

    v3 panels are normally accessed with a Bearer API token, but some panel
    routes — notably the web-UI `/panel/inbound/onlines` — are NOT exposed on
    the token-authenticated API router and return 404 unless you present a
    valid login cookie. This logs in and caches the cookie session.
    """
    if not username or not password:
        return None
    now = time.time()
    ck = cache_key or f"{host}|{username}"
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
        s.verify = False
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
                    resp = session_obj.post(url, json={}, verify=False, timeout=timeout_sec)
                else:
                    resp = session_obj.get(url, verify=False, timeout=timeout_sec)

                last_status = resp.status_code
                try:
                    _body_snippet = re.sub(r'\s+', ' ', (resp.text or ''))[:160]
                    _srv_hdr = resp.headers.get('Server', '?')
                    _ct = resp.headers.get('Content-Type', '?')
                    app.logger.info(f"[onlines] {method} {url} -> HTTP {resp.status_code} [Server={_srv_hdr}, CT={_ct}]: {_body_snippet}")
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
                app.logger.warning(f"[onlines] all endpoints failed ({normalized_type}): {hint}")
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
            resp = session_obj.get(url, verify=False, timeout=timeout_sec, allow_redirects=False)

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


def fetch_direct_link_from_subscription(sub_url: str, fallback_func=None, fallback_args=None) -> str:
    """
    Fetch the direct config link from the upstream X-UI subscription endpoint.
    Returns the first config line, or falls back to manual generation if fetch fails.
    """
    direct_link = None
    try:
        resp = requests.get(
            sub_url, 
            headers={'User-Agent': 'v2rayng'}, 
            timeout=5, 
            verify=False,
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
