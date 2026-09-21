"""Why is a renewed client still inactive? Read-only, per server + account.

One panel, four layers, and a classification. The point is that "renew said
success and the customer is offline" has several very different causes - a global
client left disabled, ONE inbound membership disabled, a node that has not
synchronised, a stale EVE snapshot, a partially applied write - and they are
indistinguishable from the dashboard. This walks the layers in order and names the
one that is divergent.

It NEVER writes: every call is a read (client record, inbound list, traffic row),
the capability probe only asks whether a route exists, and nothing is claimed,
charged or notified. Credentials are never printed: the capability view is the
whitelisted ``as_dict()`` (version, profile, booleans) and the client fields
printed are limited to enable/expiry/quota.

Usage (on the host, as the app user)::

    python scripts/forensic_renew_activation.py --server-id 3 --email <account>
    python scripts/forensic_renew_activation.py --server-id 3 --email <acct> --json

Exit codes: 0 = report produced, 2 = the server or the account could not be read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A diagnostic must not start the fetchers: a concurrent read would change the very
# snapshot this reports on.
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')

# Imported at module level because the classifier below needs its state names. The
# layer module itself pulls in nothing heavy (no app, no DB, no network).
from panel.services import renew_activation  # noqa: E402

#: The one classification the report ends with. Kept as an explicit list so a new
#: cause cannot be smuggled in as a free-text string.
CLASSIFICATIONS = (
    'LEGACY_STATE_DIVERGENCE',       # the inbound row and the client record disagree
    'GLOBAL_DISABLED',               # the client record itself says enable=false
    'MEMBERSHIP_DIVERGENCE',         # at least one attached inbound has it disabled
    'TRAFFIC_STATE_DIVERGENCE',      # the traffic row still reports it disabled
    'NODE_PENDING',                  # the panel committed it, its node has not synced
    'EVE_SNAPSHOT_STALE',            # EVE's cached row disagrees with the panel
    'PARTIAL_RENEW',                 # expiry/quota did not reach the panel
    'AUTH_DEGRADED',                 # the API could not be proven; nothing may be sent
    'UNKNOWN',
)


def _client_view(client):
    """The only client fields this report ever prints."""
    if not isinstance(client, dict):
        return None
    return {
        'email': client.get('email'),
        'enable': client.get('enable'),
        'expiryTime': client.get('expiryTime'),
        'totalGB': client.get('totalGB'),
        'up': client.get('up'),
        'down': client.get('down'),
        'limitHwid': client.get('limitHwid'),
    }


def _cached_view(email):
    """EVE's own cached row for this account, or None."""
    try:
        from app import GLOBAL_SERVER_DATA
    except Exception:
        return None
    target = str(email or '').strip().lower()
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        for row in (inbound.get('clients') or []):
            if str(row.get('email') or '').strip().lower() == target:
                return {
                    'server_id': inbound.get('server_id'),
                    'inbound_id': inbound.get('id'),
                    'enable': row.get('enable'),
                    'expiryTimestamp': row.get('expiryTimestamp'),
                    'totalGB': row.get('totalGB'),
                    'remaining_bytes': row.get('remaining_bytes'),
                    'service_state': row.get('service_state'),
                    'telemetry_updated_at': row.get('telemetry_updated_at'),
                }
    return None


def collect(server_id: int, email: str) -> dict:
    import app as app_module                     # deferred: models need the app context
    from panel.adapters import xui as xui_adapter
    from panel.services import panel_capabilities
    from panel.models import Server

    report = {'account': {'server_id': int(server_id), 'email': email}}

    with app_module.app.app_context():
        server = Server.query.get(int(server_id))
        if server is None:
            report['found'] = False
            report['error'] = 'server %s not found' % server_id
            return report
        session_obj, login_error = xui_adapter.get_xui_session(server)
        if login_error or session_obj is None:
            report['found'] = False
            report['error'] = 'panel login failed: %s' % (login_error or 'no session')
            return report

        caps, caps_reason = panel_capabilities.capabilities_for(server, session_obj)
        strategy = panel_capabilities.select_renew_strategy(caps)
        report['panel'] = {
            'detected_version': caps.as_dict().get('version'),
            'compat_profile': caps.as_dict().get('profile'),
            'strategy': strategy.value,
            'capabilities': caps.as_dict(),
            'degraded_reason': caps_reason,
        }
        if strategy is panel_capabilities.RenewStrategy.BLOCKED:
            report['found'] = True
            report['classification'] = 'AUTH_DEGRADED'
            report['detail'] = caps_reason or 'the panel API could not be classified'
            report['eve_snapshot'] = _cached_view(email)
            return report

        details = xui_adapter.v3_get_client_details(server, session_obj, email)
        client = details.get('client') if details.get('ok') else None
        inbound_ids = list(details.get('inbound_ids') or [])

        inbounds, fetch_error, _detected = xui_adapter.fetch_inbounds(
            session_obj, server.host, server.panel_type, force_fresh=True)
        inbounds = inbounds or []
        memberships = renew_activation.membership_map(
            inbounds, email, inbound_ids=(inbound_ids or None))
        # The requested inbound is unknown here (the operator only gives server+email),
        # so membership divergence is judged from the panel's own inboundIds: that is
        # the list a renewal would have to converge.
        traffic = (xui_adapter.v3_client_traffic(server, session_obj, email)
                   if caps.client_traffic else {'available': False,
                                                'reason': 'capability not proven'})

        report['found'] = bool(client) or bool(memberships)
        report['client_record'] = _client_view(client)
        report['client_read_error'] = details.get('error')
        report['inbound_ids'] = inbound_ids
        report['inbound_read_error'] = fetch_error
        report['memberships'] = {
            str(inbound_id): _client_view(row)
            for inbound_id, row in sorted(memberships.items())
        }
        report['traffic'] = traffic
        report['eve_snapshot'] = _cached_view(email)

        layers = renew_activation.analyze_activation(
            expected={'expiryTime': (client or {}).get('expiryTime'),
                      'totalGB': (client or {}).get('totalGB')},
            global_client=client, inbound_ids=inbound_ids, memberships=memberships,
            traffic=traffic)
        report['layers'] = layers.as_dict()
        report['classification'], report['detail'] = _classify(
            layers, client, memberships, traffic, caps, report['eve_snapshot'])
    return report


def _classify(layers, client, memberships, traffic, caps, cached):
    """Name the divergent layer, most specific first."""
    if client is None and not any(memberships.values()):
        return 'UNKNOWN', 'the account was found in neither the client record nor any inbound'
    if client is not None and client.get('enable') is False:
        return 'GLOBAL_DISABLED', 'the client record itself reports enable=false'
    if layers.disabled_inbound_ids:
        return ('MEMBERSHIP_DIVERGENCE',
                'disabled in inbound(s) %s'
                % ', '.join(str(i) for i in layers.disabled_inbound_ids))
    if layers.missing_inbound_ids:
        return ('MEMBERSHIP_DIVERGENCE',
                'listed by the panel in inbound(s) %s but absent from them'
                % ', '.join(str(i) for i in layers.missing_inbound_ids))
    if traffic.get('available') and traffic.get('enable') is False:
        return ('TRAFFIC_STATE_DIVERGENCE',
                'the traffic row still reports the client as disabled')
    if client is not None and not layers.config_applied:
        return ('PARTIAL_RENEW',
                'expiry/quota on the panel do not match what the renewal intended')
    if cached is not None and client is not None:
        if (bool(cached.get('enable')) != bool(client.get('enable'))
                or cached.get('totalGB') != client.get('totalGB')
                or cached.get('expiryTimestamp') != client.get('expiryTime')):
            return ('EVE_SNAPSHOT_STALE',
                    'EVE\'s cached row disagrees with the panel for this account')
    if layers.runtime_sync_state == renew_activation.RUNTIME_PENDING:
        return 'NODE_PENDING', 'the panel reports its node has not synchronised yet'
    if caps.client_api_family == 'legacy_inbound':
        return ('LEGACY_STATE_DIVERGENCE',
                'legacy inbound panel: the client lives inside an inbound, so a '
                'client-level record and an inbound row can legitimately differ')
    return 'UNKNOWN', 'no divergent layer: the account is active across every layer read'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-id', required=True, type=int)
    parser.add_argument('--email', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    report = collect(args.server_id, args.email)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get('found') else 2

    print('=' * 78)
    print('Renew activation forensics: server %s, account %s'
          % (report['account']['server_id'], report['account']['email']))
    print('=' * 78)
    if not report.get('found'):
        print('COULD NOT READ: %s' % report.get('error'))
        return 2
    panel = report['panel']
    print('\n-- panel --')
    print('  detected version : %s' % panel['detected_version'])
    print('  compat profile   : %s' % panel['compat_profile'])
    print('  renew strategy   : %s' % panel['strategy'])
    caps = panel['capabilities']
    print('  capabilities     : family=%s update=%s traffic=%s bulkEnable=%s '
          'nodePending=%s probe=%s'
          % (caps['client_api_family'], caps['client_update'], caps['client_traffic'],
             caps['bulk_enable'], caps['node_pending_response'], caps['probe_state']))
    if panel.get('degraded_reason'):
        print('  DEGRADED         : %s' % panel['degraded_reason'])

    print('\n-- global client record --')
    print('  %s' % json.dumps(report['client_record']))
    print('  inboundIds: %s   (read error: %s)'
          % (report['inbound_ids'], report['client_read_error']))

    print('\n-- memberships (one row per attached inbound) --')
    if not report['memberships']:
        print('  (none read)')
    for inbound_id, row in report['memberships'].items():
        print('  inbound %-6s %s' % (inbound_id, json.dumps(row)))

    print('\n-- traffic row --')
    print('  %s' % json.dumps(report['traffic']))

    print('\n-- EVE cached snapshot --')
    print('  %s' % json.dumps(report['eve_snapshot']))

    print('\n-- layer verdict --')
    layers = report['layers']
    print('  config_applied               : %s' % layers['config_applied'])
    print('  activation_config_converged  : %s' % layers['activation_config_converged'])
    print('  runtime_sync_state           : %s' % layers['runtime_sync_state'])
    print('  final_state                  : %s' % layers['final_state'])
    for note in layers['notes']:
        print('  note: %s' % note)

    print('\n  CLASSIFICATION: %s' % report['classification'])
    print('  detail: %s' % report['detail'])
    print('=' * 78)
    print('Read-only: no write, no charge, no notification was performed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
