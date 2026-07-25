"""x-ui database merger / inbound-transform API routes (extracted from app.py)."""
import base64
import copy
import json
import os
import re
import secrets
import shutil
import sqlite3
import uuid
from collections import defaultdict

from flask import Blueprint, jsonify, request, send_file, url_for

from panel.routes.common import login_required

bp = Blueprint('merger', __name__)


MERGER_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MERGER_DIR_NAME = 'merger'


def _merger_base_dir():
    from app import app  # deferred: app-level helper, avoids circular import
    path = os.path.join(app.instance_path, MERGER_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _merger_job_dir(job_id):
    safe_job = re.sub(r'[^a-f0-9-]', '', str(job_id or '').lower())
    if not safe_job:
        raise ValueError('Invalid merger job')
    path = os.path.abspath(os.path.join(_merger_base_dir(), safe_job))
    base = os.path.abspath(_merger_base_dir())
    if not (path == base or path.startswith(base + os.sep)):
        raise ValueError('Invalid merger job path')
    return path


def _merger_json_load(raw, default=None):
    if default is None:
        default = {}
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None:
        return copy.deepcopy(default)
    try:
        parsed = json.loads(raw)
        return parsed if parsed is not None else copy.deepcopy(default)
    except Exception:
        return copy.deepcopy(default)


def _merger_json_dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _merger_table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _merger_table_columns(conn, table_name):
    try:
        return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
    except Exception:
        return []


def _merger_row_to_dict(row):
    return {key: row[key] for key in row.keys()}


def _merger_client_email(client):
    if not isinstance(client, dict):
        return ''
    return str(client.get('email') or client.get('remark') or client.get('id') or '').strip()


def _merger_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _merger_normalize_settings_for_export(settings_raw):
    settings = _merger_json_load(settings_raw, {})
    if not isinstance(settings, dict):
        return settings
    clients = settings.get('clients')
    if isinstance(clients, list):
        for client in clients:
            if isinstance(client, dict) and 'enable' in client:
                client['enable'] = _merger_bool(client.get('enable'))
    return settings


def _merger_traffic_rows(conn, inbound_ids):
    tables = _merger_table_names(conn)
    if 'client_traffics' not in tables:
        return {}
    cols = _merger_table_columns(conn, 'client_traffics')
    if 'inbound_id' not in cols:
        return {}

    traffic = defaultdict(dict)
    placeholders = ','.join(['?'] * len(inbound_ids))
    rows = conn.execute(
        f'SELECT * FROM client_traffics WHERE inbound_id IN ({placeholders})',
        [int(v) for v in inbound_ids],
    ).fetchall()
    for row in rows:
        item = _merger_row_to_dict(row)
        email = str(item.get('email') or '').strip()
        if email:
            traffic[int(item.get('inbound_id') or 0)][email] = item
    return traffic


def _merger_analyze_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = _merger_table_names(conn)
        if 'inbounds' not in tables:
            raise ValueError('This SQLite database does not contain an inbounds table.')

        inbound_cols = _merger_table_columns(conn, 'inbounds')
        rows = conn.execute('SELECT * FROM inbounds ORDER BY id').fetchall()
        traffic_counts = defaultdict(int)
        if 'client_traffics' in tables and 'inbound_id' in _merger_table_columns(conn, 'client_traffics'):
            for row in conn.execute('SELECT inbound_id, COUNT(*) AS c FROM client_traffics GROUP BY inbound_id').fetchall():
                traffic_counts[int(row['inbound_id'] or 0)] = int(row['c'] or 0)

        inbounds = []
        for row in rows:
            item = _merger_row_to_dict(row)
            settings = _merger_json_load(item.get('settings'), {})
            clients = settings.get('clients') if isinstance(settings, dict) else []
            if not isinstance(clients, list):
                clients = []
            inbounds.append({
                'id': int(item.get('id')),
                'remark': item.get('remark') or item.get('tag') or f"Inbound {item.get('id')}",
                'port': item.get('port'),
                'protocol': item.get('protocol') or '-',
                'enable': bool(item.get('enable')),
                'client_count': len(clients),
                'traffic_rows': traffic_counts[int(item.get('id') or 0)],
                'up': int(item.get('up') or 0) if 'up' in inbound_cols else 0,
                'down': int(item.get('down') or 0) if 'down' in inbound_cols else 0,
                'total': int(item.get('total') or 0) if 'total' in inbound_cols else 0,
            })
        return {
            'inbounds': inbounds,
            'tables': sorted(list(tables)),
            'has_client_traffics': 'client_traffics' in tables,
        }
    finally:
        conn.close()


def _merger_make_unique_email(email, used):
    base = (email or 'client').strip() or 'client'
    if base not in used:
        used.add(base)
        return base, None
    counter = 2
    while True:
        candidate = f'{base}-m{counter}'
        if candidate not in used:
            used.add(candidate)
            return candidate, candidate
        counter += 1


def _merger_export_inbound(row, client_stats=None):
    raw = dict(row)
    export = {}
    camel = {
        'user_id': 'userId',
        'expiry_time': 'expiryTime',
        'stream_settings': 'streamSettings',
    }
    allowed = {
        'id', 'userId', 'up', 'down', 'total', 'remark', 'enable', 'expiryTime',
        'listen', 'port', 'protocol', 'settings', 'streamSettings', 'tag',
        'sniffing', 'allocate',
    }
    json_fields = {'settings', 'stream_settings', 'streamSettings', 'sniffing', 'allocate'}

    for key, value in raw.items():
        out_key = camel.get(key, key)
        if out_key not in allowed:
            continue
        if key == 'settings':
            export[out_key] = _merger_normalize_settings_for_export(value)
        elif key in json_fields or out_key in json_fields:
            export[out_key] = _merger_json_load(value, {})
        elif out_key == 'enable':
            export[out_key] = _merger_bool(value)
        else:
            export[out_key] = value

    if 'settings' in export and isinstance(export['settings'], dict):
        export['settings'] = _merger_json_dump(export['settings'])
    if 'streamSettings' in export and isinstance(export['streamSettings'], dict):
        export['streamSettings'] = _merger_json_dump(export['streamSettings'])
    if 'sniffing' in export and isinstance(export['sniffing'], dict):
        export['sniffing'] = _merger_json_dump(export['sniffing'])
    if 'allocate' in export and isinstance(export['allocate'], dict):
        export['allocate'] = _merger_json_dump(export['allocate'])
    export['clientStats'] = client_stats or []
    return export


def _merger_merge_db(job_id, selected_ids, base_id, final_port, final_remark=None):
    if len(selected_ids) < 2:
        raise ValueError('Select at least two inbounds to merge.')
    selected_ids = [int(v) for v in selected_ids]
    base_id = int(base_id or selected_ids[0])
    if base_id not in selected_ids:
        raise ValueError('Base inbound must be one of the selected inbounds.')
    final_port = int(final_port)
    if final_port < 1 or final_port > 65535:
        raise ValueError('Final port must be between 1 and 65535.')

    job_dir = _merger_job_dir(job_id)
    source_db = os.path.join(job_dir, 'source.db')
    output_db = os.path.join(job_dir, 'merged.db')
    export_path = os.path.join(job_dir, 'merged-inbound.json')
    if not os.path.exists(source_db):
        raise ValueError('Uploaded database was not found. Upload it again.')

    shutil.copy2(source_db, output_db)
    conn = sqlite3.connect(output_db)
    conn.row_factory = sqlite3.Row
    try:
        tables = _merger_table_names(conn)
        if 'inbounds' not in tables:
            raise ValueError('This SQLite database does not contain an inbounds table.')
        inbound_cols = _merger_table_columns(conn, 'inbounds')
        placeholders = ','.join(['?'] * len(selected_ids))
        rows = conn.execute(
            f'SELECT * FROM inbounds WHERE id IN ({placeholders}) ORDER BY id',
            selected_ids,
        ).fetchall()
        by_id = {int(row['id']): _merger_row_to_dict(row) for row in rows}
        missing = [v for v in selected_ids if v not in by_id]
        if missing:
            raise ValueError(f'Inbound not found: {missing[0]}')

        traffic_by_inbound = _merger_traffic_rows(conn, selected_ids)
        merged_clients = []
        duplicate_report = []
        traffic_to_insert = []
        used_emails = set()

        for inbound_id in selected_ids:
            inbound = by_id[inbound_id]
            settings = _merger_json_load(inbound.get('settings'), {})
            clients = settings.get('clients') if isinstance(settings, dict) else []
            if not isinstance(clients, list):
                clients = []
            for client in clients:
                if not isinstance(client, dict):
                    continue
                copied = copy.deepcopy(client)
                original_email = _merger_client_email(copied)
                final_email, renamed = _merger_make_unique_email(original_email, used_emails)
                if final_email != original_email:
                    copied['email'] = final_email
                    duplicate_report.append({
                        'inbound_id': inbound_id,
                        'original': original_email,
                        'renamed': final_email,
                    })
                merged_clients.append(copied)

                traffic_row = traffic_by_inbound.get(int(inbound_id), {}).get(original_email)
                if traffic_row:
                    traffic_copy = dict(traffic_row)
                    traffic_copy['inbound_id'] = base_id
                    traffic_copy['email'] = final_email
                    traffic_copy.pop('id', None)
                    traffic_to_insert.append(traffic_copy)

        base_row = by_id[base_id]
        base_settings = _merger_json_load(base_row.get('settings'), {})
        if not isinstance(base_settings, dict):
            base_settings = {}
        base_settings['clients'] = merged_clients

        updates = {'settings': _merger_json_dump(base_settings)}
        if 'port' in inbound_cols:
            updates['port'] = final_port
        if final_remark and 'remark' in inbound_cols:
            updates['remark'] = str(final_remark).strip()
        if 'up' in inbound_cols:
            updates['up'] = sum(int((by_id[i].get('up') or 0)) for i in selected_ids)
        if 'down' in inbound_cols:
            updates['down'] = sum(int((by_id[i].get('down') or 0)) for i in selected_ids)

        assignments = ', '.join([f'"{key}" = ?' for key in updates.keys()])
        conn.execute(
            f'UPDATE inbounds SET {assignments} WHERE id = ?',
            list(updates.values()) + [base_id],
        )

        delete_ids = [v for v in selected_ids if v != base_id]
        if delete_ids:
            delete_placeholders = ','.join(['?'] * len(delete_ids))
            conn.execute(f'DELETE FROM inbounds WHERE id IN ({delete_placeholders})', delete_ids)

        if 'client_traffics' in tables:
            traffic_cols = [c for c in _merger_table_columns(conn, 'client_traffics') if c != 'id']
            if 'inbound_id' in traffic_cols:
                conn.execute(
                    f'DELETE FROM client_traffics WHERE inbound_id IN ({placeholders})',
                    selected_ids,
                )
            if 'inbound_id' in traffic_cols and traffic_to_insert:
                insert_cols = [c for c in traffic_cols if any(c in row for row in traffic_to_insert)]
                quoted_cols = ', '.join([f'"{c}"' for c in insert_cols])
                insert_sql = (
                    f'INSERT INTO client_traffics ({quoted_cols}) '
                    f'VALUES ({", ".join(["?"] * len(insert_cols))})'
                )
                for row in traffic_to_insert:
                    conn.execute(insert_sql, [row.get(c) for c in insert_cols])

        conn.commit()

        final_row = conn.execute('SELECT * FROM inbounds WHERE id = ?', [base_id]).fetchone()
        client_stats = []
        if 'client_traffics' in tables and 'inbound_id' in _merger_table_columns(conn, 'client_traffics'):
            for stat_row in conn.execute('SELECT * FROM client_traffics WHERE inbound_id = ? ORDER BY id', [base_id]).fetchall():
                raw_stat = _merger_row_to_dict(stat_row)
                client_stats.append({
                    'id': raw_stat.get('id'),
                    'inboundId': raw_stat.get('inbound_id'),
                    'enable': _merger_bool(raw_stat.get('enable')),
                    'email': raw_stat.get('email') or '',
                    'up': raw_stat.get('up') or 0,
                    'down': raw_stat.get('down') or 0,
                    'expiryTime': raw_stat.get('expiry_time', raw_stat.get('expiryTime')) or 0,
                    'total': raw_stat.get('total') or 0,
                    'reset': raw_stat.get('reset') or 0,
                })
        export_payload = _merger_export_inbound(_merger_row_to_dict(final_row), client_stats)
        with open(export_path, 'w', encoding='utf-8') as fh:
            json.dump(export_payload, fh, ensure_ascii=False, indent=2)

        return {
            'base_inbound_id': base_id,
            'final_port': final_port,
            'client_count': len(merged_clients),
            'renamed_duplicates': duplicate_report,
        }
    finally:
        conn.close()


# ── Inbound Transform (protocol / transport / emails / port) ─────────────────
# Offline rebuild of a single inbound inside the uploaded x-ui DB. Lets you do
# what the x-ui panel forbids: change an inbound's protocol while keeping all of
# its clients (their fields are remapped to the target protocol's shape).

MERGER_TRANSFORM_PROTOCOLS = ('vless', 'vmess', 'trojan', 'shadowsocks')
MERGER_SS_METHODS = (
    'chacha20-ietf-poly1305', 'aes-256-gcm', 'aes-128-gcm',
    '2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm', '2022-blake3-chacha20-poly1305',
)


# Protocol families: 'client' protocols share x-ui's per-client quota model and
# transform losslessly. The others use a different user model entirely.
MERGER_CLIENT_FAMILY = ('vless', 'vmess', 'trojan', 'shadowsocks')
MERGER_ACCOUNT_FAMILY = ('socks', 'http')
MERGER_ALL_TARGETS = MERGER_CLIENT_FAMILY + MERGER_ACCOUNT_FAMILY + ('dokodemo-door', 'wireguard')


def _wg_keypair():
    """Return (private_b64, public_b64) Curve25519 keys in WireGuard format."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(priv_raw).decode('ascii'), base64.b64encode(pub_raw).decode('ascii')


def _transform_client(src: dict, target_protocol: str, ss_method: str,
                      email_prefix: str, email_suffix: str) -> dict:
    """Remap one client dict from its source shape to the target protocol's shape,
    keeping the cross-protocol fields (quota/expiry/sub/etc.)."""
    from app import _ss_key_len, _ss_password  # deferred: app-level helper, avoids circular import
    src = src if isinstance(src, dict) else {}
    old_email = _merger_client_email(src) or 'client'
    new_email = f'{email_prefix}{old_email}{email_suffix}'

    def _i(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    common = {
        'email': new_email,
        'enable': _merger_bool(src.get('enable', True)),
        'limitIp': _i(src.get('limitIp'), 0),
        'totalGB': _i(src.get('totalGB'), 0),
        'expiryTime': _i(src.get('expiryTime'), 0),
        'tgId': src.get('tgId', 0) if src.get('tgId') not in (None, '') else 0,
        'subId': src.get('subId') or '',
        'comment': src.get('comment') or '',
        'reset': _i(src.get('reset'), 0),
    }

    if target_protocol == 'vless':
        common['id'] = src.get('id') or str(uuid.uuid4())
        common['flow'] = src.get('flow') or ''
    elif target_protocol == 'vmess':
        common['id'] = src.get('id') or str(uuid.uuid4())
    elif target_protocol == 'trojan':
        common['password'] = src.get('password') or secrets.token_urlsafe(16)
    elif target_protocol == 'shadowsocks':
        common['method'] = ss_method
        # Reuse an existing SS password only if it already matches the method length.
        existing = src.get('password') or ''
        try:
            existing_len = len(base64.b64decode(existing)) if existing else 0
        except Exception:
            existing_len = 0
        common['password'] = existing if existing_len == _ss_key_len(ss_method) else _ss_password(ss_method)
    return common, old_email, new_email


def _build_inbound_settings(target_protocol: str, clients: list,
                            ss_method: str, existing_settings: dict) -> dict:
    from app import _ss_password  # deferred: app-level helper, avoids circular import
    if target_protocol == 'vless':
        return {'clients': clients, 'decryption': 'none', 'fallbacks': []}
    if target_protocol == 'vmess':
        return {'clients': clients}
    if target_protocol == 'trojan':
        return {'clients': clients, 'fallbacks': []}
    if target_protocol == 'shadowsocks':
        return {
            'clients': clients,
            'method': ss_method,
            'password': _ss_password(ss_method),
            'network': 'tcp,udp',
            'ivCheck': False,
        }
    # Fallback: keep existing structure, just swap clients.
    out = dict(existing_settings or {})
    out['clients'] = clients
    return out


def _build_stream_settings(existing: dict, transport: str, security: str,
                           target_protocol: str) -> dict:
    ss = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    ss.setdefault('externalProxy', [])

    # ── Security ──
    if security == 'none':
        ss['security'] = 'none'
        for k in ('tlsSettings', 'realitySettings', 'xtlsSettings'):
            ss.pop(k, None)
    # security == 'keep' → leave as-is

    # ── Transport ──
    if transport != 'keep':
        for k in ('tcpSettings', 'wsSettings', 'grpcSettings', 'httpSettings',
                  'kcpSettings', 'quicSettings', 'httpupgradeSettings'):
            ss.pop(k, None)
        if transport == 'tcp':
            ss['network'] = 'tcp'
            ss['tcpSettings'] = {'acceptProxyProtocol': False, 'header': {'type': 'none'}}
        elif transport == 'tcp_http':
            ss['network'] = 'tcp'
            ss['tcpSettings'] = {
                'acceptProxyProtocol': False,
                'header': {
                    'type': 'http',
                    'request': {'version': '1.1', 'method': 'GET', 'path': ['/'], 'headers': {}},
                    'response': {'version': '1.1', 'status': '200', 'reason': 'OK', 'headers': {}},
                },
            }
        elif transport == 'ws':
            ss['network'] = 'ws'
            ss['wsSettings'] = {'path': '/', 'headers': {}}
        elif transport == 'grpc':
            ss['network'] = 'grpc'
            ss['grpcSettings'] = {'serviceName': '', 'multiMode': False}
        elif transport == 'h2':
            ss['network'] = 'http'
            ss['httpSettings'] = {'path': '/', 'host': []}
    else:
        # Keeping the transport, but Shadowsocks doesn't use TCP http-header
        # obfuscation — normalize it to avoid an unusable config.
        if target_protocol == 'shadowsocks' and (ss.get('network') == 'tcp'):
            tcp = ss.get('tcpSettings') or {}
            if isinstance(tcp.get('header'), dict) and tcp['header'].get('type') == 'http':
                ss['tcpSettings'] = {'acceptProxyProtocol': False, 'header': {'type': 'none'}}
    return ss


def _merger_transform_db(job_id, inbound_id, opts):
    job_dir = _merger_job_dir(job_id)
    source_db = os.path.join(job_dir, 'source.db')
    output_db = os.path.join(job_dir, 'merged.db')
    export_path = os.path.join(job_dir, 'merged-inbound.json')
    if not os.path.exists(source_db):
        raise ValueError('Uploaded database was not found. Upload it again.')

    inbound_id = int(inbound_id)
    target_protocol = (opts.get('protocol') or 'keep').strip().lower()
    ss_method = (opts.get('ss_method') or 'chacha20-ietf-poly1305').strip()
    transport = (opts.get('transport') or 'keep').strip().lower()
    security = (opts.get('security') or 'keep').strip().lower()
    email_prefix = str(opts.get('email_prefix') or '')
    email_suffix = str(opts.get('email_suffix') or '')
    new_port = opts.get('port')
    new_remark = opts.get('remark')

    if target_protocol not in ('keep',) + MERGER_ALL_TARGETS:
        raise ValueError('Unsupported target protocol.')
    if target_protocol == 'shadowsocks' and ss_method not in MERGER_SS_METHODS:
        raise ValueError('Unsupported Shadowsocks method.')
    if new_port is not None and str(new_port).strip() != '':
        new_port = int(new_port)
        if new_port < 1 or new_port > 65535:
            raise ValueError('Port must be between 1 and 65535.')
    else:
        new_port = None

    shutil.copy2(source_db, output_db)
    conn = sqlite3.connect(output_db)
    conn.row_factory = sqlite3.Row
    try:
        tables = _merger_table_names(conn)
        if 'inbounds' not in tables:
            raise ValueError('This SQLite database does not contain an inbounds table.')
        inbound_cols = _merger_table_columns(conn, 'inbounds')

        row = conn.execute('SELECT * FROM inbounds WHERE id = ?', [inbound_id]).fetchone()
        if not row:
            raise ValueError(f'Inbound {inbound_id} not found.')
        inbound = _merger_row_to_dict(row)

        source_protocol = (inbound.get('protocol') or 'vless').lower()
        final_protocol = source_protocol if target_protocol == 'keep' else target_protocol

        settings = _merger_json_load(inbound.get('settings'), {})
        src_clients = settings.get('clients') if isinstance(settings, dict) else []
        if not isinstance(src_clients, list):
            src_clients = []

        rename_map = {}     # old_email -> new_email  (only for client-family)
        client_count = 0
        drop_traffic = False  # for non-client families, the old per-client rows no longer apply

        def _renamed_email(c):
            old = _merger_client_email(c) or 'client'
            return old, f'{email_prefix}{old}{email_suffix}'

        if final_protocol in MERGER_CLIENT_FAMILY:
            new_clients = []
            for c in src_clients:
                tc, old_email, new_email = _transform_client(c, final_protocol, ss_method, email_prefix, email_suffix)
                new_clients.append(tc)
                if old_email:
                    rename_map[old_email] = new_email
            new_settings = _build_inbound_settings(final_protocol, new_clients, ss_method, settings if isinstance(settings, dict) else {})
            client_count = len(new_clients)

        elif final_protocol in MERGER_ACCOUNT_FAMILY:
            # socks / http → username/password accounts (no quota/expiry)
            accounts = []
            for c in src_clients:
                _, new_email = _renamed_email(c)
                accounts.append({'user': new_email, 'pass': secrets.token_urlsafe(12)})
            if final_protocol == 'socks':
                new_settings = {'auth': 'password', 'accounts': accounts, 'udp': True, 'ip': ''}
            else:  # http
                new_settings = {'accounts': accounts, 'allowTransparent': False}
            client_count = len(accounts)
            drop_traffic = True

        elif final_protocol == 'dokodemo-door':
            addr = str(opts.get('dokodemo_address') or '127.0.0.1').strip() or '127.0.0.1'
            try:
                dport = int(opts.get('dokodemo_port') or 0)
            except (TypeError, ValueError):
                dport = 0
            if dport <= 0:
                dport = int(new_port or inbound.get('port') or 0)
            new_settings = {
                'address': addr,
                'port': dport,
                'network': 'tcp,udp',
                'followRedirect': False,
                'portMap': {},
            }
            client_count = 0
            drop_traffic = True

        elif final_protocol == 'wireguard':
            server_priv, _server_pub = _wg_keypair()
            peers = []
            for i, c in enumerate(src_clients):
                p_priv, p_pub = _wg_keypair()
                peers.append({
                    'privateKey': p_priv,
                    'publicKey': p_pub,
                    'psk': '',
                    'allowedIPs': [f'10.0.0.{(i % 250) + 2}/32'],
                    'keepAlive': 0,
                })
            new_settings = {'mtu': 1420, 'secretKey': server_priv, 'peers': peers, 'noKernelTun': False}
            client_count = len(peers)
            drop_traffic = True
        else:
            # Fallback: keep clients shape unchanged
            new_settings = settings if isinstance(settings, dict) else {}

        existing_stream = _merger_json_load(inbound.get('streamSettings') or inbound.get('stream_settings'), {})
        new_stream = _build_stream_settings(existing_stream, transport, security, final_protocol)

        updates = {
            'protocol': final_protocol,
            'settings': _merger_json_dump(new_settings),
        }
        if 'streamSettings' in inbound_cols:
            updates['streamSettings'] = _merger_json_dump(new_stream)
        elif 'stream_settings' in inbound_cols:
            updates['stream_settings'] = _merger_json_dump(new_stream)
        if new_port is not None and 'port' in inbound_cols:
            updates['port'] = new_port
            if 'tag' in inbound_cols:
                updates['tag'] = f'inbound-{new_port}'
        if new_remark is not None and str(new_remark).strip() != '' and 'remark' in inbound_cols:
            updates['remark'] = str(new_remark).strip()

        assignments = ', '.join([f'"{k}" = ?' for k in updates.keys()])
        conn.execute(f'UPDATE inbounds SET {assignments} WHERE id = ?',
                     list(updates.values()) + [inbound_id])

        # Traffic rows: rename for the client family, drop for the others
        # (socks/http accounts, dokodemo forwarder and wireguard peers have no
        # per-client traffic rows, so leaving the old ones would show ghosts).
        renamed_traffic = 0
        if 'client_traffics' in tables:
            tcols = _merger_table_columns(conn, 'client_traffics')
            if 'inbound_id' in tcols:
                if drop_traffic:
                    conn.execute('DELETE FROM client_traffics WHERE inbound_id = ?', [inbound_id])
                elif 'email' in tcols:
                    for old_email, new_email in rename_map.items():
                        if old_email == new_email:
                            continue
                        cur = conn.execute(
                            'UPDATE client_traffics SET email = ? WHERE inbound_id = ? AND email = ?',
                            [new_email, inbound_id, old_email])
                        renamed_traffic += cur.rowcount or 0

        conn.commit()

        final_row = conn.execute('SELECT * FROM inbounds WHERE id = ?', [inbound_id]).fetchone()
        client_stats = []
        if 'client_traffics' in tables and 'inbound_id' in _merger_table_columns(conn, 'client_traffics'):
            for stat_row in conn.execute('SELECT * FROM client_traffics WHERE inbound_id = ? ORDER BY id', [inbound_id]).fetchall():
                raw_stat = _merger_row_to_dict(stat_row)
                client_stats.append({
                    'id': raw_stat.get('id'),
                    'inboundId': raw_stat.get('inbound_id'),
                    'enable': _merger_bool(raw_stat.get('enable')),
                    'email': raw_stat.get('email') or '',
                    'up': raw_stat.get('up') or 0,
                    'down': raw_stat.get('down') or 0,
                    'expiryTime': raw_stat.get('expiry_time', raw_stat.get('expiryTime')) or 0,
                    'total': raw_stat.get('total') or 0,
                    'reset': raw_stat.get('reset') or 0,
                })
        export_payload = _merger_export_inbound(_merger_row_to_dict(final_row), client_stats)
        with open(export_path, 'w', encoding='utf-8') as fh:
            json.dump(export_payload, fh, ensure_ascii=False, indent=2)

        return {
            'inbound_id': inbound_id,
            'source_protocol': source_protocol,
            'final_protocol': final_protocol,
            'client_count': client_count,
            'emails_renamed': sum(1 for o, n in rename_map.items() if o != n),
            'traffic_rows_updated': renamed_traffic,
            'quota_preserved': final_protocol in MERGER_CLIENT_FAMILY,
            'final_port': new_port if new_port is not None else inbound.get('port'),
        }
    finally:
        conn.close()


@bp.route('/api/merger/analyze', methods=['POST'])
@login_required
def merger_analyze():
    from app import _merger_user_is_allowed  # deferred: app-level helper, avoids circular import
    if not _merger_user_is_allowed():
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    upload = request.files.get('database')
    if not upload or not upload.filename:
        return jsonify({'success': False, 'error': 'Upload an x-ui SQLite database file.'}), 400
    if request.content_length and request.content_length > MERGER_MAX_UPLOAD_BYTES:
        return jsonify({'success': False, 'error': 'Database file is too large.'}), 413

    job_id = str(uuid.uuid4())
    job_dir = _merger_job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)
    source_db = os.path.join(job_dir, 'source.db')
    upload.save(source_db)

    try:
        analysis = _merger_analyze_db(source_db)
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({'success': False, 'error': str(exc)}), 400

    return jsonify({'success': True, 'job_id': job_id, **analysis})


@bp.route('/api/merger/merge', methods=['POST'])
@login_required
def merger_merge():
    from app import _merger_user_is_allowed  # deferred: app-level helper, avoids circular import
    if not _merger_user_is_allowed():
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    data = request.get_json(silent=True) or {}
    try:
        result = _merger_merge_db(
            data.get('job_id'),
            data.get('inbound_ids') or [],
            data.get('base_inbound_id'),
            data.get('final_port'),
            data.get('remark'),
        )
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    job_id = str(data.get('job_id') or '')
    return jsonify({
        'success': True,
        **result,
        'downloads': {
            'database': url_for('merger.merger_download', job_id=job_id, kind='db'),
            'inbound_export': url_for('merger.merger_download', job_id=job_id, kind='export'),
        }
    })


@bp.route('/api/merger/transform', methods=['POST'])
@login_required
def merger_transform():
    from app import _merger_user_is_allowed  # deferred: app-level helper, avoids circular import
    if not _merger_user_is_allowed():
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    data = request.get_json(silent=True) or {}
    job_id = str(data.get('job_id') or '')
    try:
        result = _merger_transform_db(
            job_id,
            data.get('inbound_id'),
            {
                'protocol': data.get('protocol'),
                'ss_method': data.get('ss_method'),
                'transport': data.get('transport'),
                'security': data.get('security'),
                'port': data.get('port'),
                'remark': data.get('remark'),
                'email_prefix': data.get('email_prefix'),
                'email_suffix': data.get('email_suffix'),
                'dokodemo_address': data.get('dokodemo_address'),
                'dokodemo_port': data.get('dokodemo_port'),
            },
        )
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    return jsonify({
        'success': True,
        **result,
        'downloads': {
            'database': url_for('merger.merger_download', job_id=job_id, kind='db'),
            'inbound_export': url_for('merger.merger_download', job_id=job_id, kind='export'),
        }
    })


@bp.route('/api/merger/download/<job_id>/<kind>')
@login_required
def merger_download(job_id, kind):
    from app import _merger_user_is_allowed  # deferred: app-level helper, avoids circular import
    if not _merger_user_is_allowed():
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    job_dir = _merger_job_dir(job_id)
    if kind == 'db':
        path = os.path.join(job_dir, 'merged.db')
        filename = 'x-ui-merged.db'
    elif kind == 'export':
        path = os.path.join(job_dir, 'merged-inbound.json')
        filename = 'x-ui-merged-inbound.json'
    else:
        return jsonify({'success': False, 'error': 'Invalid download type'}), 404
    if not os.path.exists(path):
        return jsonify({'success': False, 'error': 'Merged output not found'}), 404
    return send_file(path, as_attachment=True, download_name=filename)
