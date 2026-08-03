"""Subscription rendering cluster (extracted from app.py).

Share-link generation per client/inbound, full subscription config
aggregation, and inbound client lookup.
"""
import base64
import json
import re
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlsplit

from panel.adapters.xui import (
    _v3_get,
    _v3_post,
    fetch_inbounds,
    get_xui_session,
    server_is_v3,
)
from panel.core.redis_client import GLOBAL_SERVER_DATA


SUBSCRIPTION_STATISTICS_ENABLED_KEY = 'subscription_statistics_enabled'
SUBSCRIPTION_STATISTICS_TEMPLATE_FA_KEY = 'subscription_statistics_template_fa'
SUBSCRIPTION_STATISTICS_TEMPLATE_EN_KEY = 'subscription_statistics_template_en'
DEFAULT_SUBSCRIPTION_STATISTICS_TEMPLATE_FA = (
    '{emoji} {status} | {renewal} | {expiry} | {volume} | انتخاب نکنید'
)
DEFAULT_SUBSCRIPTION_STATISTICS_TEMPLATE_EN = (
    '{emoji} {status} | {renewal} | {expiry} | {volume} | Do not select'
)
SUBSCRIPTION_STATISTICS_PLACEHOLDERS = {
    'emoji', 'status', 'renewal', 'days', 'volume', 'remaining_volume',
    'total_volume', 'used_volume', 'expiry', 'expiry_type', 'email',
}
_SUBSCRIPTION_STATISTICS_PLACEHOLDER_RE = re.compile(r'\{([a-z_]+)\}')
_CLONEABLE_SUBSCRIPTION_SCHEMES = {
    'vless', 'trojan', 'ss', 'socks', 'http', 'https', 'hysteria',
    'hysteria2', 'hy2', 'tuic', 'wireguard', 'wg', 'anytls', 'ssh',
}


def validate_subscription_statistics_template(value):
    """Normalize a status-name template and reject unsupported variables."""
    template = str(value or '').strip()
    if not template:
        raise ValueError('Statistics template cannot be empty')
    if len(template) > 500:
        raise ValueError('Statistics template cannot exceed 500 characters')
    if any(char in template for char in ('\r', '\n', '\x00')):
        raise ValueError('Statistics template must be a single line')
    unknown = sorted(
        set(_SUBSCRIPTION_STATISTICS_PLACEHOLDER_RE.findall(template))
        - SUBSCRIPTION_STATISTICS_PLACEHOLDERS
    )
    if unknown:
        raise ValueError(f"Unknown statistics variable: {{{unknown[0]}}}")
    return template


def render_subscription_statistics_name(template, values):
    """Render only the documented placeholders; literal text stays untouched."""
    normalized = validate_subscription_statistics_template(template)
    rendered = _SUBSCRIPTION_STATISTICS_PLACEHOLDER_RE.sub(
        lambda match: str((values or {}).get(match.group(1), '')),
        normalized,
    )
    return re.sub(r'\s+', ' ', rendered).strip()[:500]


def clone_subscription_config_with_name(configs, display_name):
    """Clone the first usable user config and change only its display remark.

    Credentials, host, transport and every connection parameter remain exactly
    the same. The resulting statistics entry is therefore still connectable.
    """
    name = re.sub(r'[\r\n\x00]+', ' ', str(display_name or '')).strip()[:500]
    if not name:
        return None

    for raw_link in configs or []:
        link = str(raw_link or '').strip()
        if not link:
            continue
        if link.startswith('vmess://'):
            try:
                encoded = link[len('vmess://'):]
                padded = encoded + ('=' * (-len(encoded) % 4))
                obj = json.loads(base64.b64decode(padded, altchars=b'-_').decode('utf-8'))
                if not isinstance(obj, dict):
                    continue
                obj['ps'] = name
                payload = base64.b64encode(
                    json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                ).decode('ascii')
                return f'vmess://{payload}'
            except Exception:
                continue

        try:
            scheme = (urlsplit(link).scheme or '').lower()
        except ValueError:
            continue
        if scheme not in _CLONEABLE_SUBSCRIPTION_SCHEMES:
            continue
        base = link.partition('#')[0]
        return f"{base}#{quote(name, safe='')}"
    return None


SUBSCRIPTION_PROFILE_CACHE = {}
SUBSCRIPTION_PROFILE_CACHE_TTL = 600
_SUBSCRIPTION_PROFILE_CACHE_LOCK = threading.Lock()


def fetch_subscription_profile_metadata(
    server,
    *,
    session_obj,
    timeout=(1.5, 2),
):
    """Return cacheable visual metadata from the panel, never subscription content."""
    server_id = int(getattr(server, 'id', 0) or 0)
    now = time.monotonic()
    with _SUBSCRIPTION_PROFILE_CACHE_LOCK:
        cached = SUBSCRIPTION_PROFILE_CACHE.get(server_id)
        if cached and now < float(cached.get('expiry') or 0):
            return dict(cached.get('value') or {})

    metadata = {}
    try:
        ok, payload, _error = _v3_post(
            server,
            session_obj,
            "/panel/api/setting/all",
            {},
            timeout=timeout,
        )
        obj = payload.get('obj') if ok and isinstance(payload, dict) else None
        if isinstance(obj, dict):
            metadata = {
                'sub_title': str(obj.get('subTitle') or '').strip(),
                'update_interval': str(obj.get('subUpdates') or '24').strip() or '24',
            }
    except Exception:
        metadata = {}

    if metadata:
        with _SUBSCRIPTION_PROFILE_CACHE_LOCK:
            SUBSCRIPTION_PROFILE_CACHE[server_id] = {
                'value': dict(metadata),
                'expiry': now + SUBSCRIPTION_PROFILE_CACHE_TTL,
            }
        return metadata

    # A temporary settings failure must not discard previously learned visual
    # metadata. Subscription credentials/configs are never stored in this cache.
    return dict((cached or {}).get('value') or {})


def build_subscription_profile_title(panel_title, server_name):
    """Put Eve's server name on a second line under the panel profile title."""
    panel_title = str(panel_title or '').strip()
    server_name = str(server_name or '').strip()
    if panel_title and server_name and panel_title.casefold() != server_name.casefold():
        return f"{panel_title}\n{server_name}"
    return panel_title or server_name


def find_subscription_client_email(server, sub_id, *, session_obj=None):
    """Resolve display identity without ever taking credentials from the snapshot."""
    normalized_sub_id = str(sub_id or '').strip()
    server_id = int(getattr(server, 'id', 0) or 0)
    for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
        try:
            if int(inbound.get('server_id') or 0) != server_id:
                continue
        except (TypeError, ValueError):
            continue
        clients = inbound.get('clients')
        if not isinstance(clients, list):
            settings = inbound.get('settings') or {}
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except (TypeError, ValueError):
                    settings = {}
            clients = settings.get('clients') if isinstance(settings, dict) else []
        for client in clients or []:
            if str(client.get('subId') or '').strip() == normalized_sub_id:
                email = str(client.get('email') or '').strip()
                if email:
                    return email

    # 3x-ui v3.6 exposes a small paged client record that can search by subId.
    # This is only identity metadata; links and credentials still come from the
    # authoritative live subLinks endpoint on every request.
    if session_obj and normalized_sub_id:
        path = "/panel/api/clients/list/paged?" + urlencode({
            'page': 1,
            'pageSize': 5,
            'search': normalized_sub_id,
        })
        try:
            ok, payload, _error = _v3_get(
                server,
                session_obj,
                path,
                timeout=(1.5, 2),
            )
            obj = payload.get('obj') if ok and isinstance(payload, dict) else None
            items = obj.get('items') if isinstance(obj, dict) else []
            for item in items or []:
                if str(item.get('subId') or '').strip() == normalized_sub_id:
                    email = str(item.get('email') or '').strip()
                    if email:
                        return email
        except Exception:
            pass
    return ''


def ensure_subscription_identity(configs, client_email):
    """Ensure the client's email is visible in every config remark."""
    email = str(client_email or '').strip()
    if not email:
        return [str(item) for item in configs or [] if item]

    decorated = []
    for raw_link in configs or []:
        link = str(raw_link or '').strip()
        if not link:
            continue
        if link.startswith('vmess://'):
            try:
                encoded = link[len('vmess://'):]
                padded = encoded + ('=' * (-len(encoded) % 4))
                obj = json.loads(base64.b64decode(padded).decode('utf-8'))
                remark = str(obj.get('ps') or '').strip()
                if email.casefold() not in remark.casefold():
                    obj['ps'] = f"{remark}-{email}" if remark else email
                    encoded = base64.b64encode(
                        json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                    ).decode('ascii')
                    link = f"vmess://{encoded}"
            except Exception:
                pass
        elif '://' in link:
            base, separator, fragment = link.partition('#')
            remark = unquote(fragment).strip() if separator else ''
            if email.casefold() not in remark.casefold():
                remark = f"{remark}-{email}" if remark else email
                link = f"{base}#{quote(remark, safe='')}"
        decorated.append(link)
    return decorated


def get_subscription_inbound_order(server):
    """Parse the persisted inbound-id priority list, ignoring invalid entries."""
    raw = getattr(server, 'subscription_inbound_order', None) or '[]'
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    if not isinstance(raw, list):
        return []
    result = []
    seen = set()
    for value in raw:
        try:
            inbound_id = int(value)
        except (TypeError, ValueError):
            continue
        if inbound_id > 0 and inbound_id not in seen:
            seen.add(inbound_id)
            result.append(inbound_id)
    return result


def ordered_subscription_inbounds(server, inbounds):
    """Order inbound rows while keeping unconfigured/new rows stable at the end."""
    order = get_subscription_inbound_order(server)
    if not order:
        return list(inbounds or [])
    rank = {inbound_id: index for index, inbound_id in enumerate(order)}

    def _key(item_with_index):
        index, inbound = item_with_index
        try:
            inbound_id = int(inbound.get('id') or 0)
        except (TypeError, ValueError):
            inbound_id = 0
        return rank.get(inbound_id, len(rank) + index), index

    return [item for _, item in sorted(enumerate(inbounds or []), key=_key)]


def _subscription_link_signature(link):
    """Return protocol, port and display remark without exposing credentials."""
    raw = str(link or '').strip()
    if raw.startswith('vmess://'):
        try:
            encoded = raw[len('vmess://'):]
            padded = encoded + ('=' * (-len(encoded) % 4))
            obj = json.loads(base64.b64decode(padded).decode('utf-8'))
            return 'vmess', str(obj.get('port') or ''), str(obj.get('ps') or '')
        except Exception:
            return 'vmess', '', ''

    try:
        parsed = urlsplit(raw)
        protocol = (parsed.scheme or '').lower()
        protocol = {
            'ss': 'shadowsocks',
            'hy2': 'hysteria2',
            'hysteria': 'hysteria2',
            'tg': 'mtproto',
        }.get(protocol, protocol)
        if protocol == 'mtproto':
            port = (parse_qs(parsed.query).get('port') or [''])[0]
        else:
            try:
                port = str(parsed.port or '')
            except ValueError:
                port = ''
        return protocol, port, unquote(parsed.fragment or '').strip()
    except Exception:
        return '', '', ''


def sort_subscription_configs(configs, server, *, inbounds=None, sub_id=None):
    """Sort live configs by the server's inbound priority without caching content."""
    links = [str(item).strip() for item in configs or [] if item]
    order = get_subscription_inbound_order(server)
    if len(links) < 2 or not order:
        return links

    if inbounds is None:
        server_id = int(getattr(server, 'id', 0) or 0)
        inbounds = []
        for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
            try:
                if int(inbound.get('server_id') or 0) == server_id:
                    inbounds.append(inbound)
            except (TypeError, ValueError):
                continue

    inbound_by_id = {}
    for inbound in inbounds or []:
        try:
            inbound_by_id[int(inbound.get('id') or 0)] = inbound
        except (TypeError, ValueError):
            continue

    normalized_sub_id = str(sub_id or '').strip()
    if normalized_sub_id:
        eligible_ids = []
        for inbound in inbounds or []:
            clients = inbound.get('clients')
            if not isinstance(clients, list):
                settings = inbound.get('settings') or {}
                if isinstance(settings, str):
                    try:
                        settings = json.loads(settings)
                    except (TypeError, ValueError):
                        settings = {}
                clients = settings.get('clients') if isinstance(settings, dict) else []
            if any(
                str(client.get('subId') or '').strip() == normalized_sub_id
                for client in (clients or [])
            ):
                try:
                    eligible_ids.append(int(inbound.get('id') or 0))
                except (TypeError, ValueError):
                    eligible_ids.append(0)
        # 3x-ui emits subLinks in inbound traversal order. When the counts
        # match, this positional association is more reliable than URI ports
        # because public-host rules may replace the inbound's visible port.
        if len(eligible_ids) == len(links):
            rank = {inbound_id: index for index, inbound_id in enumerate(order)}
            paired = list(zip(links, eligible_ids, range(len(links))))
            paired.sort(key=lambda item: (
                rank.get(item[1], len(rank) + item[2]),
                item[2],
            ))
            return [item[0] for item in paired]

    signature_rank = {}
    remark_ranks = []
    for rank, inbound_id in enumerate(order):
        inbound = inbound_by_id.get(inbound_id)
        if not inbound:
            continue
        protocol = str(inbound.get('protocol') or '').strip().lower()
        port = str(inbound.get('port') or '').strip()
        if protocol and port:
            signature_rank.setdefault((protocol, port), rank)
        remark = str(inbound.get('remark') or '').strip().casefold()
        if remark:
            remark_ranks.append((len(remark), remark, rank))
    remark_ranks.sort(reverse=True)

    def _key(item_with_index):
        index, link = item_with_index
        protocol, port, remark = _subscription_link_signature(link)
        rank = signature_rank.get((protocol, port))
        if rank is None and remark:
            folded = remark.casefold()
            for _length, inbound_remark, candidate_rank in remark_ranks:
                if inbound_remark in folded:
                    rank = candidate_rank
                    break
        return (rank if rank is not None else len(order) + index), index

    return [link for _, link in sorted(enumerate(links), key=_key)]


def generate_client_link(client, inbound, server_host):
    """Generate share links for client-capable inbound protocols."""

    from app import app  # deferred: Flask instance lives in app.py (circular at module level)

    def _as_json(obj, default=None):
        if default is None:
            default = {}
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, str):
            try:
                return json.loads(obj)
            except Exception:
                return default
        return default

    def _parse_host(server_host, inbound_port):
        host_value = server_host or ''
        if host_value and not host_value.startswith(('http://', 'https://')):
            host_value = f"http://{host_value}"
        parsed = urlparse(host_value)
        host = parsed.hostname or parsed.path or ''
        port_val = inbound_port or parsed.port
        return host, port_val

    def _extract_stream_parts(stream_settings):
        network = (stream_settings.get('network') or 'tcp').lower()
        security = (stream_settings.get('security') or 'none').lower()

        ws = stream_settings.get('wsSettings') or {}
        grpc = stream_settings.get('grpcSettings') or {}
        tcp = stream_settings.get('tcpSettings') or {}
        h2 = stream_settings.get('httpSettings') or {}

        path = ws.get('path') or h2.get('path') or ''
        host_header = (ws.get('headers') or {}).get('Host') or (ws.get('headers') or {}).get('host') or ''
        if not host_header:
            host_header = h2.get('host') or ''

        service_name = grpc.get('serviceName') or grpc.get('service_name') or ''
        mode = grpc.get('multiMode') and 'multi' or 'gun'

        header = (tcp.get('header') or {})
        header_type = header.get('type') or ''
        if header_type == 'http':
            hosts = header.get('request', {}).get('headers', {}).get('Host') or []
            host_header = ','.join(hosts) if isinstance(hosts, list) else hosts

        tls_settings = stream_settings.get('tlsSettings') or {}
        reality_settings = stream_settings.get('realitySettings') or {}
        sni = tls_settings.get('serverName') or (reality_settings.get('serverNames') or [None])[0]
        alpn_list = tls_settings.get('alpn') or []
        alpn = ','.join(alpn_list) if isinstance(alpn_list, list) else alpn_list

        fp = reality_settings.get('fingerprint') or stream_settings.get('fingerprint')
        pbk = reality_settings.get('publicKey')
        sid = reality_settings.get('shortId') or reality_settings.get('shortIds') or ''

        return {
            "network": network,
            "security": security,
            "path": path,
            "host_header": host_header,
            "service_name": service_name,
            "grpc_mode": mode,
            "header_type": header_type,
            "sni": sni,
            "alpn": alpn,
            "fp": fp,
            "pbk": pbk,
            "sid": sid,
        }

    try:
        protocol = (inbound.get('protocol') or '').lower()
        settings = _as_json(inbound.get('settings'))
        stream_settings = _as_json(inbound.get('streamSettings'))
        if not stream_settings:
            stream_settings = {
                'network': inbound.get('network') or 'tcp',
                'security': inbound.get('security') or 'none',
            }
        stream = _extract_stream_parts(stream_settings)
        raw_client = _as_json(client.get('raw_client'))

        def _client_value(*keys):
            for key in keys:
                value = client.get(key)
                if value not in (None, ''):
                    return value
                value = raw_client.get(key)
                if value not in (None, ''):
                    return value
            return ''

        host, port = _parse_host(server_host, inbound.get('port'))
        remark = quote(_client_value('email') or inbound.get('remark') or 'client')
        uuid = _client_value('id', 'uuid', 'password')
        flow = _client_value('flow') or settings.get('flow') or ''

        if protocol == 'vless':
            query = {
                "encryption": "none",
                "type": stream["network"],
                "security": None if stream["security"] == 'none' else stream["security"],
                "sni": stream["sni"],
                "alpn": stream["alpn"],
                "fp": stream["fp"],
                "pbk": stream["pbk"],
                "sid": stream["sid"],
                "flow": flow or None,
            }
            if stream["network"] == 'ws':
                query.update({"path": stream["path"], "host": stream["host_header"]})
            elif stream["network"] == 'grpc':
                query.update({"type": "grpc", "serviceName": stream["service_name"], "mode": stream["grpc_mode"]})
            elif stream["network"] == 'tcp' and stream["header_type"] == 'http':
                query.update({"type": "http", "host": stream["host_header"]})

            q = {k: v for k, v in query.items() if v not in (None, '', [])}
            return f"vless://{uuid}@{host}:{port}?{urlencode(q)}#{remark}"

        if protocol == 'vmess':
            aid = _client_value('alterId', 'aid') or 0
            vmess_obj = {
                "v": "2",
                "ps": _client_value('email') or inbound.get('remark') or host,
                "add": host,
                "port": str(port),
                "id": uuid,
                "aid": str(aid),
                "scy": "auto",
                "net": stream["network"],
                "type": stream["header_type"] or "none",
                "host": stream["host_header"],
                "path": stream["path"] if stream["network"] == 'ws' else '',
                "tls": "" if stream["security"] == 'none' else stream["security"],
                "sni": stream["sni"] or "",
                "alpn": stream["alpn"] or "",
                "fp": stream["fp"] or "",
                "pbk": stream["pbk"] or "",
                "sid": stream["sid"] or "",
                "serviceName": stream["service_name"] if stream["network"] == 'grpc' else "",
            }
            payload = base64.b64encode(json.dumps(vmess_obj, ensure_ascii=False).encode()).decode()
            return f"vmess://{payload}"

        if protocol == 'trojan':
            password = _client_value('password') or uuid
            query = {
                "type": stream["network"],
                "security": None if stream["security"] == 'none' else stream["security"],
                "sni": stream["sni"],
                "alpn": stream["alpn"],
                "host": stream["host_header"],
            }
            if stream["network"] == 'ws':
                query.update({"path": stream["path"]})
            elif stream["network"] == 'grpc':
                query.update({"serviceName": stream["service_name"], "mode": stream["grpc_mode"]})
            q = {k: v for k, v in query.items() if v not in (None, '', [])}
            q_str = f"?{urlencode(q)}" if q else ''
            return f"trojan://{password}@{host}:{port}{q_str}#{remark}"

        if protocol == 'shadowsocks':
            method = settings.get('method') or _client_value('method')
            password = _client_value('password') or uuid
            if method and password:
                userinfo = base64.b64encode(f"{method}:{password}".encode()).decode()
                query = {}
                if stream["network"] == 'ws':
                    plugin = f"v2ray-plugin;path={stream['path'] or '/'};host={stream['host_header'] or host}"
                    if stream["security"] != 'none':
                        plugin += ";tls"
                    query["plugin"] = plugin
                elif stream["network"] == 'grpc':
                    plugin = f"grpc;serviceName={stream['service_name']}"
                    query["plugin"] = plugin
                q = f"?{urlencode(query)}" if query else ''
                return f"ss://{userinfo}@{host}:{port}{q}#{remark}"
            return None

        if protocol == 'wireguard':
            private_key = _client_value('privateKey', 'password')
            if not private_key:
                return None
            server_public_key = settings.get('publicKey') or settings.get('pubKey') or ''
            if not server_public_key and settings.get('secretKey'):
                try:
                    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
                    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
                    raw_secret = str(settings['secretKey']).strip()
                    raw_secret += '=' * (-len(raw_secret) % 4)
                    secret_bytes = base64.b64decode(raw_secret)
                    public_bytes = X25519PrivateKey.from_private_bytes(secret_bytes).public_key().public_bytes(
                        Encoding.Raw, PublicFormat.Raw)
                    server_public_key = base64.b64encode(public_bytes).decode()
                except Exception:
                    server_public_key = ''

            allowed_ips = client.get('allowedIPs') or []
            if isinstance(allowed_ips, str):
                allowed_ips = [part.strip() for part in allowed_ips.split(',') if part.strip()]
            params = {
                'publickey': server_public_key,
                'address': allowed_ips[0] if allowed_ips else '',
                'mtu': settings.get('mtu') or '',
                'dns': settings.get('dns') or '',
                'presharedkey': _client_value('preSharedKey') or '',
                'keepalive': _client_value('keepAlive') or '',
            }
            query = urlencode({k: str(v) for k, v in params.items() if v not in ('', None, 0)})
            suffix = f"?{query}" if query else ''
            return f"wireguard://{quote(str(private_key), safe='')}@{host}:{port}{suffix}#{remark}"

        if protocol == 'mtproto':
            secret = _client_value('secret', 'Secret')
            if not secret:
                return None
            params = {
                'server': host,
                'port': port,
                'secret': secret,
                'adtag': _client_value('adTag', 'AdTag') or None,
            }
            query = urlencode({k: str(v) for k, v in params.items() if v not in ('', None)})
            return f"tg://proxy?{query}"

        return None
    except Exception as exc:
        app.logger.debug(f"Link gen failed: {exc}")
        return None


def fetch_authoritative_subscription_configs(
    server,
    sub_id,
    *,
    session_obj=None,
    timeout=(3, 8),
):
    """Fetch exact live links from the v3 main-panel API.

    This endpoint preserves public hosts, remarks and protocol-specific query
    fields. It is intentionally independent of the separately hosted /sub
    endpoint, whose response can lag behind credential changes.
    """
    normalized_sub_id = str(sub_id or '').strip()
    if not normalized_sub_id:
        return []

    if session_obj is None:
        try:
            session_obj, login_error = get_xui_session(server)
        except Exception:
            return []
        if login_error or not session_obj:
            return []

    try:
        ok, payload, _error = _v3_get(
            server,
            session_obj,
            f"/panel/api/clients/subLinks/{quote(normalized_sub_id)}",
            timeout=timeout,
        )
    except Exception:
        return []
    if not ok or not isinstance(payload, dict):
        return []

    obj = payload.get('obj')
    links = []
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, str):
                link = item.strip()
            elif isinstance(item, dict):
                link = str(item.get('link') or item.get('url') or '').strip()
            else:
                link = ''
            if '://' in link:
                links.append(link)
    return links


def build_subscription_configs(
    server,
    sub_id,
    fallback_client=None,
    fallback_inbound=None,
    *,
    live_session=None,
    live_inbounds=None,
):
    """Return ALL protocol config links for a subscription id.

    On 3x-ui v3 a single subId is attached to MULTIPLE inbounds; the panel's
    own /sub server aggregates them into one subscription. When we can't proxy
    that separate sub server (wrong/unreachable sub_port, v3.3.1 changes), we
    must reproduce the full set ourselves instead of emitting only the first
    inbound — otherwise client apps load fewer inbounds than the panel does.

    Strategy (most authoritative first; each falls through on failure):
      1) v3 API GET /panel/api/clients/subLinks/{subId}.
      2) Inbounds already fetched live for this HTTP request.
      3) A fresh fetch_inbounds when this helper is used outside the route.
      4) Last resort: single (fallback_client, fallback_inbound) link.
    """
    from app import app, _json_field  # deferred: live in app.py (circular at module level)

    sub_id = str(sub_id or '').strip()

    def _links_from_inbounds(inbounds):
        links = []
        seen = set()
        for inb in ordered_subscription_inbounds(server, inbounds):
            settings = _json_field(inb.get('settings'), {})
            for cli in settings.get('clients', []):
                c_sub = str(cli.get('subId') or '').strip()
                c_uuid = str(cli.get('id') or '').strip()
                if sub_id and (sub_id == c_sub or (not c_sub and sub_id == c_uuid)):
                    link = generate_client_link(cli, inb, server.host)
                    if link and link not in seen:
                        seen.add(link)
                        links.append(link)
        return links

    session_obj = live_session
    if session_obj is None:
        try:
            session_obj, _login_err = get_xui_session(server)
        except Exception:
            session_obj = None

    # 1) The v3 main-panel API is live like fetch_inbounds, but unlike the
    # generic generator it preserves each inbound's public host and remark.
    if session_obj and sub_id and server_is_v3(server, session_obj):
        links = fetch_authoritative_subscription_configs(
            server,
            sub_id,
            session_obj=session_obj,
        )
        if links:
            return sort_subscription_configs(
                links,
                server,
                inbounds=(live_inbounds if live_inbounds is not None else None),
                sub_id=sub_id,
            )

    # 2) The route passes the exact inbound response it just fetched. This is
    # the live fallback for legacy panels and v3 installations without subLinks.
    if live_inbounds is not None:
        links = _links_from_inbounds(live_inbounds)
        if links:
            return links

    # 3) Aggregate generate_client_link across a fresh inbound response.
    if session_obj:
        try:
            if live_inbounds is None:
                inbounds, ferr, _t = fetch_inbounds(
                    session_obj, server.host, server.panel_type,
                )
            else:
                inbounds, ferr = live_inbounds, None
            if not ferr and inbounds:
                links = _links_from_inbounds(inbounds)
                if links:
                    return links
        except Exception as e:
            app.logger.debug(f"Multi-inbound link aggregation failed for sub {sub_id}: {e}")

    # 4) Last resort: the single inbound we already matched in the route.
    if fallback_client and fallback_inbound:
        link = generate_client_link(fallback_client, fallback_inbound, server.host)
        if link:
            return [link]
    return []

def find_client(inbounds, inbound_id, email):
    from app import _json_field  # deferred: lives in app.py (circular at module level)
    for inbound in inbounds:
        if inbound.get('id') != inbound_id:
            continue
        settings = _json_field(inbound.get('settings'), {})
        for client in settings.get('clients', []):
            if client.get('email') == email:
                return client, inbound
    return None, None
