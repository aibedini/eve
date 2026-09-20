"""Normalized per-server snapshot blocks and bounded compatibility views.

Schema v2 stores one v3 account entity and lightweight inbound memberships.  The
compatibility graph returned by :func:`hydrate_server_block` shares the same entity dict
between memberships when no membership override exists, so legacy readers keep working
without retaining one full row per mirrored inbound.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid

SCHEMA_VERSION = 2
_MEMBERSHIP_FIELDS = frozenset({'inbound_id', 'inboundId'})


class UnknownSnapshotSchema(ValueError):
    pass


def _uuid(value):
    try:
        return str(uuid.UUID(str(value).strip())).lower()
    except (ValueError, TypeError, AttributeError):
        return None


def client_key(client):
    """Reliable account identity, scoped by the containing server block."""
    if not isinstance(client, dict):
        return None
    reliable = _uuid(client.get('id')) or _uuid(client.get('uuid'))
    if reliable:
        return 'uuid:' + reliable
    email = str(client.get('email') or '').strip().casefold()
    return ('email:' + email) if email else None


def _entity_data(client):
    return {key: copy.deepcopy(value) for key, value in client.items()
            if key not in _MEMBERSHIP_FIELDS}


def _revision(data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), default=str).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


def normalize_server_block(inbounds, server_id):
    """Return a JSON-safe schema-v2 block without materializing duplicate rows."""
    entities = {}
    revisions = {}
    normalized_inbounds = []
    membership_index = {}
    anonymous = 0
    for inbound in inbounds or []:
        if not isinstance(inbound, dict):
            continue
        inbound_id = inbound.get('id')
        normalized = {key: copy.deepcopy(value) for key, value in inbound.items()
                      if key != 'clients'}
        normalized['client_refs'] = []
        normalized['client_overrides'] = {}
        for client in inbound.get('clients') or []:
            if not isinstance(client, dict):
                continue
            key = client_key(client)
            if key is None:
                anonymous += 1
                key = 'anonymous:%d' % anonymous
            data = _entity_data(client)
            if key not in entities:
                entities[key] = data
                revisions[key] = _revision(data)
            else:
                baseline = entities[key]
                override = {name: copy.deepcopy(value) for name, value in data.items()
                            if baseline.get(name) != value}
                removed = [name for name in baseline if name not in data]
                if removed:
                    override['__remove__'] = removed
                if override:
                    normalized['client_overrides'][key] = override
            normalized['client_refs'].append(key)
            membership_index.setdefault(key, []).append(inbound_id)
        normalized_inbounds.append(normalized)
    return {
        'schema_version': SCHEMA_VERSION,
        'server_id': int(server_id),
        'clients': entities,
        'entity_revisions': revisions,
        'inbounds': normalized_inbounds,
        'membership_index': membership_index,
    }


def validate_server_block(block):
    if not isinstance(block, dict) or block.get('schema_version') != SCHEMA_VERSION:
        version = block.get('schema_version') if isinstance(block, dict) else None
        raise UnknownSnapshotSchema('unsupported snapshot schema: %r' % version)
    if not isinstance(block.get('clients'), dict) or not isinstance(block.get('inbounds'), list):
        raise ValueError('invalid schema-v2 snapshot block')
    return block


def _membership_client(entity, override, inbound_id):
    if override:
        row = copy.deepcopy(entity)
        for name in override.get('__remove__') or []:
            row.pop(name, None)
        row.update({name: copy.deepcopy(value) for name, value in override.items()
                    if name != '__remove__'})
    else:
        row = entity
    # inbound_id cannot live on a shared entity. Existing internal readers use the
    # enclosing inbound; external materialization adds it on the ephemeral copy.
    return row


def hydrate_server_block(block):
    """Build the retained compatibility graph with shared canonical entity objects."""
    validate_server_block(block)
    entities = {key: copy.deepcopy(value) for key, value in block['clients'].items()}
    hydrated = []
    for source in block['inbounds']:
        inbound = {key: copy.deepcopy(value) for key, value in source.items()
                   if key not in {'client_refs', 'client_overrides'}}
        overrides = source.get('client_overrides') or {}
        inbound['clients'] = [
            _membership_client(entities[key], overrides.get(key), inbound.get('id'))
            for key in source.get('client_refs') or [] if key in entities
        ]
        hydrated.append(inbound)
    return hydrated


def normalize_retained_block(inbounds, server_id):
    block = normalize_server_block(inbounds, server_id)
    return hydrate_server_block(block), block


def build_retained_index(inbounds):
    """O(1) entity lookup plus O(memberships) mutation fan-out, retaining refs only."""
    entities = {}
    memberships = {}
    for inbound in inbounds or []:
        if not isinstance(inbound, dict):
            continue
        for client in inbound.get('clients') or []:
            key = client_key(client)
            if key is None:
                continue
            entities.setdefault(key, client)
            memberships.setdefault(key, []).append((inbound, client))
    return {'entities': entities, 'memberships': memberships}


def materialize_server_block(block):
    """Expand one requested block; returned rows are never retained by this module."""
    validate_server_block(block)
    entities = block['clients']
    result = []
    for source in block['inbounds']:
        inbound = {key: copy.deepcopy(value) for key, value in source.items()
                   if key not in {'client_refs', 'client_overrides'}}
        overrides = source.get('client_overrides') or {}
        rows = []
        for key in source.get('client_refs') or []:
            entity = entities.get(key)
            if entity is None:
                continue
            row = copy.deepcopy(entity)
            override = overrides.get(key) or {}
            for name in override.get('__remove__') or []:
                row.pop(name, None)
            row.update({name: copy.deepcopy(value) for name, value in override.items()
                        if name != '__remove__'})
            row['inbound_id'] = inbound.get('id')
            rows.append(row)
        inbound['clients'] = rows
        result.append(inbound)
    return result


def materialize_retained_inbounds(inbounds, normalized_server_ids, keys=None):
    """Expand only requested normalized compatibility rows for an external response."""
    normalized = {int(value) for value in (normalized_server_ids or set())}
    wanted = None if keys is None else {tuple(value) for value in keys}
    result = []
    for inbound in inbounds or []:
        if not isinstance(inbound, dict):
            continue
        key = (inbound.get('server_id'), inbound.get('id'))
        if wanted is not None and key not in wanted:
            continue
        try:
            is_normalized = int(inbound.get('server_id')) in normalized
        except (TypeError, ValueError):
            is_normalized = False
        if not is_normalized:
            result.append(inbound)
            continue
        view = {name: value for name, value in inbound.items() if name != 'clients'}
        view['clients'] = []
        for client in inbound.get('clients') or []:
            if not isinstance(client, dict):
                continue
            row = copy.deepcopy(client)
            row['inbound_id'] = inbound.get('id')
            view['clients'].append(row)
        result.append(view)
    return result


def affected_inbound_ids(block, identity):
    validate_server_block(block)
    return list((block.get('membership_index') or {}).get(identity) or [])


def remove_membership(block, identity, inbound_id):
    """Remove one membership; delete the entity only after its last membership."""
    validate_server_block(block)
    changed = False
    for inbound in block['inbounds']:
        if inbound.get('id') != inbound_id:
            continue
        refs = inbound.get('client_refs') or []
        if identity in refs:
            inbound['client_refs'] = [key for key in refs if key != identity]
            (inbound.get('client_overrides') or {}).pop(identity, None)
            changed = True
    if changed:
        memberships = [inbound.get('id') for inbound in block['inbounds']
                       if identity in (inbound.get('client_refs') or [])]
        if memberships:
            block.setdefault('membership_index', {})[identity] = memberships
        else:
            block.get('membership_index', {}).pop(identity, None)
            block['clients'].pop(identity, None)
            block.get('entity_revisions', {}).pop(identity, None)
    return changed
