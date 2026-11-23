"""Machine credentials and network policy, independent of browser sessions."""
import base64
import binascii
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from flask import abort, g, request
from sqlalchemy import case, or_

from panel.core.phone import normalize_iran_mobile
from panel.extensions import db
from panel.models.machine import MachineAPIKey
from panel.security.network import FORWARDED_HEADERS, client_ip, trusted_proxy_policy

SCOPES = (
    'context.customer.read', 'context.service.read', 'context.renewal.read',
    'context.payment.read_masked', 'context.support.read', 'context.knowledge.read',
    'proposals.write',
)
KEY_RE = re.compile(r'Bearer eve_mk_([a-f0-9]{32})\.([A-Za-z0-9_-]{43})', re.IGNORECASE)
RATE_LIMIT = 120
LOOPBACK_NETWORKS = tuple(map(ipaddress.ip_network, ('127.0.0.0/8', '::1/128')))
PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7',
)))


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def timestamp(value):
    if value is None:
        return None
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def issue_key(name, scopes, days, actor):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError('Invalid key name')
    if not scopes or any(scope not in SCOPES for scope in scopes) or not 1 <= days <= 90:
        raise ValueError('Choose scopes and an expiration between 1 and 90 days')
    key_id = secrets.token_hex(16)
    secret = secrets.token_urlsafe(32)
    row = MachineAPIKey(id=key_id, name=name.strip(), scopes=sorted(set(scopes)),
                        key_hash=hashlib.sha256(secret.encode('ascii')).hexdigest(),
                        expires_at=utcnow() + timedelta(days=days), created_by=actor)
    db.session.add(row)
    return row, f'eve_mk_{key_id}.{secret}'


def check_network():
    host = (os.getenv('EVE_MACHINE_INTERNAL_HOST') or '').strip().lower()
    if not host or request.host.lower() != host or not request.is_secure:
        abort(404)
    try:
        environ = request.environ
        if any(header in environ for header in FORWARDED_HEADERS):
            peer = ipaddress.ip_address(environ.get('eve.peer_addr', ''))
            policy = trusted_proxy_policy()
            proxies = policy if isinstance(policy, tuple) else LOOPBACK_NETWORKS
            if not any(peer in network for network in PRIVATE_NETWORKS) or not any(peer in network for network in proxies):
                abort(404)
        address = ipaddress.ip_address(client_ip())
        networks = [ipaddress.ip_network(value.strip()) for value in
                    os.getenv('EVE_MACHINE_ALLOWED_CIDRS', '127.0.0.0/8,::1/128').split(',')]
        if not any(address in n for n in PRIVATE_NETWORKS) or not any(address in n for n in networks):
            abort(404)
    except ValueError:
        abort(404)


def authenticate():
    match = KEY_RE.fullmatch(request.headers.get('Authorization', ''))
    if not match:
        abort(401)
    key_id, secret = match.groups()
    row = db.session.get(MachineAPIKey, key_id)
    digest = hashlib.sha256(secret.encode('ascii')).hexdigest()
    valid = hmac.compare_digest(digest, row.key_hash if row else '0' * 64)
    now = utcnow()
    if not valid or row is None or row.revoked_at or row.expires_at <= now:
        abort(401)
    window = int(time.time()) // 60
    g.machine_key_id, g.machine_scopes = row.id, frozenset(row.scopes)
    updated = MachineAPIKey.query.filter(
        MachineAPIKey.id == key_id, MachineAPIKey.revoked_at.is_(None),
        MachineAPIKey.expires_at > now,
        or_(MachineAPIKey.rate_window != window, MachineAPIKey.rate_count < RATE_LIMIT),
    ).update({
        MachineAPIKey.rate_window: window,
        MachineAPIKey.rate_count: case((MachineAPIKey.rate_window == window, MachineAPIKey.rate_count + 1), else_=1),
        MachineAPIKey.last_used_at: now,
    }, synchronize_session=False)
    db.session.commit()
    if not updated:
        row = db.session.get(MachineAPIKey, key_id)
        if not row or row.revoked_at or row.expires_at <= utcnow():
            abort(401)
        g.machine_retry_after = max(1, 60 - int(time.time()) % 60)
        abort(429)


def configured_secret(name):
    value = (os.environ.get(name) or '').encode('utf-8')
    return value if len(value) >= 32 else None


def phone_lookup_hash(phone, pepper):
    normalized = normalize_iran_mobile(phone)
    return hmac.digest(pepper, normalized.encode('ascii'), 'sha256').hex() if normalized else None


def _service_cipher():
    secret = configured_secret('EVE_MACHINE_SERVICE_KEY_SECRET')
    if not secret:
        abort(503)
    return AESSIV(hmac.digest(secret, b'eve-machine-service-v1', 'sha256'))


def public_service_key(server_id, identity):
    prefix = f'eve:{server_id}:v1:'
    encrypted = _service_cipher().encrypt(identity.encode('utf-8'), [prefix.encode('ascii')])
    return prefix + base64.urlsafe_b64encode(encrypted).decode('ascii').rstrip('=')


def public_operation_id(identity):
    if not identity:
        return None
    secret = configured_secret('EVE_MACHINE_SERVICE_KEY_SECRET')
    if not secret:
        abort(503)
    return 'op_' + hmac.digest(secret, f'eve-machine-operation-v1\0{identity}'.encode('utf-8'), 'sha256').hex()


def private_service_identity(key):
    match = re.fullmatch(r'(eve:([1-9][0-9]{0,9}):v1:)([A-Za-z0-9_-]{23,555})', key)
    if not match or int(match[2]) > 2147483647:
        return None
    try:
        cipher = _service_cipher()
        data = base64.urlsafe_b64decode(match[3] + '=' * (-len(match[3]) % 4))
        identity = cipher.decrypt(data, [match[1].encode('ascii')]).decode('utf-8')
        if 0 < len(identity) <= 100:
            return int(match[2]), identity
    except (InvalidTag, UnicodeError, binascii.Error):
        return None
