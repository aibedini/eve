"""Account statistics shared by fetches, cache mutations and authorized views.

The authenticated v3 API addresses accounts by email. Its inbound clientStats
replicate account traffic; legacy clientStats belong to an inbound client.
Conflicting replicated counters are excluded and reported as incomplete.
"""
from datetime import datetime, timezone


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def account_key(client, server_id, inbound_id, modern):
    email = str(client.get('email') or '').strip().casefold()
    identity = str(client.get('id') or client.get('uuid') or '').strip().lower()
    if modern:
        return (str(server_id), 'email', email) if email else (
            str(server_id), 'id', identity) if identity else None
    return (str(server_id), str(inbound_id), email, identity) if email or identity else None


def aggregate_accounts(inbounds, *, modern_server_ids=(), now_ms=None):
    """O(memberships + accounts), without network reads or snapshot mutation."""
    modern_ids = {str(sid) for sid in modern_server_ids}
    now_ms = (int(datetime.now(timezone.utc).timestamp() * 1000)
              if now_ms is None else now_ms)
    stats = dict.fromkeys((
        'total_inbounds', 'active_inbounds', 'total_clients', 'unique_clients',
        'inbound_memberships', 'active_clients', 'inactive_clients',
        'online_clients', 'unknown_clients', 'not_started_clients', 'unlimited_expiry_clients',
        'unlimited_volume_clients', 'limited_clients', 'upload_raw',
        'download_raw', 'remaining_raw', 'unused_allocated_raw',
        'allocated_quota_raw', 'identity_conflicts', 'traffic_incomplete_clients',
        'unknown_capability_servers',
    ), 0)
    accounts = {}
    unknown_servers = set()
    uuid_emails = {}
    conflicting_keys = set()
    for inbound in inbounds or []:
        sid, iid = inbound.get('server_id'), inbound.get('id')
        generation = inbound.get('client_api_generation')
        modern = generation == 'v3' or (generation is None and str(sid) in modern_ids)
        if generation == 'unknown':
            unknown_servers.add(str(sid))
        stats['total_inbounds'] += 1
        stats['active_inbounds'] += bool(inbound.get('enable', True))
        for index, client in enumerate(inbound.get('clients') or []):
            key = account_key(client, sid, iid, modern) or (str(sid), str(iid), 'anonymous', index)
            stats['inbound_memberships'] += 1
            identity = str(client.get('id') or client.get('uuid') or '').strip().lower()
            if modern and identity:
                previous = uuid_emails.setdefault((str(sid), identity), key)
                if previous != key:
                    conflicting_keys.update((previous, key))
            account = accounts.get(key)
            if account is None:
                account = accounts[key] = {
                    'client': client, 'ids': set(), 'usable_membership': False,
                    'online': False, 'traffic': None, 'traffic_conflict': False,
                    'membership_traffic': {}, 'modern': modern,
                }
            if identity:
                account['ids'].add(identity)
            account['usable_membership'] |= bool(inbound.get('enable', True)) and bool(client.get('enable', True))
            account['online'] |= bool(client.get('is_online'))
            if client.get('traffic_available', True):
                traffic = (_int(client.get('up')), _int(client.get('down')))
                if modern and client.get('traffic_scope') == 'membership':
                    account['membership_traffic'][str(iid)] = traffic
                elif account['traffic'] is None:
                    account['traffic'] = traffic
                elif account['traffic'] != traffic:
                    account['traffic_conflict'] = True
    for key, account in accounts.items():
        client = account['client']
        if len(account['ids']) > 1 or key in conflicting_keys:
            stats['identity_conflicts'] += 1
        stats['total_clients'] += 1
        stats['online_clients'] += account['online']
        expiry = _int(client.get('expiryTimestamp', (client.get('raw_client') or {}).get('expiryTime')))
        expiry_type = client.get('expiryType')
        stats['not_started_clients'] += expiry < 0 or expiry_type == 'start_after_use'
        stats['unlimited_expiry_clients'] += expiry == 0 and expiry_type != 'start_after_use'
        quota = max(0, _int(client.get('totalGB')))
        stats['unlimited_volume_clients'] += quota == 0
        stats['limited_clients'] += quota > 0
        stats['allocated_quota_raw'] += quota
        traffic = account['traffic']
        membership_traffic = account['membership_traffic']
        if membership_traffic and traffic is None:
            traffic = (sum(pair[0] for pair in membership_traffic.values()),
                       sum(pair[1] for pair in membership_traffic.values()))
        elif membership_traffic:
            account['traffic_conflict'] = True
        complete = traffic is not None and not account['traffic_conflict']
        stats['traffic_incomplete_clients'] += not complete
        if complete:
            stats['upload_raw'] += traffic[0]
            stats['download_raw'] += traffic[1]
        remaining = max(0, quota - sum(traffic)) if quota and complete else 0
        stats['unused_allocated_raw'] += remaining
        enabled = bool(client.get('global_enable', client.get('enable', True)))
        usable = (enabled and account['usable_membership']
                  and (expiry <= 0 or expiry > now_ms)
                  and (quota == 0 or (complete and remaining > 0)))
        uncertain = (enabled and account['usable_membership']
                     and (expiry <= 0 or expiry > now_ms) and quota > 0 and not complete)
        stats['unknown_clients' if uncertain else 'active_clients' if usable else 'inactive_clients'] += 1
        if usable:
            stats['remaining_raw'] += remaining
    stats['unique_clients'] = stats['total_clients']
    stats['unknown_capability_servers'] = len(unknown_servers)
    stats['data_freshness'] = 'incomplete' if stats['traffic_incomplete_clients'] else 'snapshot'
    return stats
