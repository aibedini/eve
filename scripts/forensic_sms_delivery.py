"""Answer "why did this account not get its SMS?" from the database, read-only.

Written for one production case: an account that received the `low_volume` warning
and never received the terminal `volume_ended` message. Guessing which gate refused
it is not acceptable, because four different gates produce the same silence, so this
script reads the ledger and then RUNS the real delivery gates in their real order,
printing the one that fires.

Nothing here sends, writes, mutates or claims anything: every function it calls is a
read, and the delivery path itself is never invoked. It prints configuration VALUES
only, from an explicit whitelist, so no API key, template body or recipient phone
number can leak into a support ticket.

Usage (on the host, as the app user)::

    python scripts/forensic_sms_delivery.py --email h34-09195758193 --server-id 3
    python scripts/forensic_sms_delivery.py --email <acct> --server-id 3 --hours 72 --json

Exit codes: 0 = report produced, 2 = the account could not be found in the ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read-only forensics must not start workers: a fetcher running beside this script
# would change the very snapshot the verdict is computed from.
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')

#: Settings that may be printed. Everything else is withheld by default so a
#: credential cannot reach the output by accident.
SAFE_SETTINGS = (
    'enabled', 'provider',
    'trigger_created', 'trigger_renew', 'trigger_depletion',
    'trigger_near_expiry', 'trigger_low_volume', 'trigger_expired', 'trigger_ended',
    'cooldown_hours',
    'depletion_expiry_days', 'depletion_volume_gb', 'depletion_cooldown_days',
    'quiet_enabled', 'quiet_start', 'quiet_end',
    'daily_limit', 'hourly_limit', 'send_pace_seconds', 'min_interval_seconds',
    'skip_unlimited', 'expired_max_age_days', 'ended_max_age_days',
)

EVENT_FIELDS = (
    'event_id', 'state', 'previous_state', 'notification_kind', 'state_version',
    'lifecycle_generation', 'source', 'status', 'attempt_count', 'created_at',
    'next_attempt_at', 'last_attempt_at', 'sent_at', 'last_error',
    'superseded_reason', 'last_status_code', 'gateway_request_id',
)

STATE_FIELDS = (
    'service_key', 'client_uuid', 'client_email', 'last_state',
    'last_remaining_bytes', 'last_total_bytes', 'last_expiry_ms', 'state_version',
    'last_observed_at', 'updated_at',
)


def _iso(value):
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat() + 'Z'
    return value


def _dump(rows, fields):
    out = []
    for row in rows:
        item = {}
        for name in fields:
            item[name] = _iso(getattr(row, name, None))
        out.append(item)
    return out


def collect(email: str, server_id: int, hours: int) -> dict:
    import app as app_module  # deferred: models need the app context
    from sqlalchemy import func

    from panel.jobs import messaging
    from panel.models import (
        ServiceNotificationEvent,
        ServiceObservedState,
        SmsSendLog,
        WhatsappBotLog,
    )
    from panel.services import depletion_pipeline, lifecycle, telemetry_state

    email_l = email.strip().lower()
    report = {'account': {'email': email_l, 'server_id': server_id,
                          'window_hours': hours}}

    with app_module.app.app_context():
        # A client's service key is derived from its canonical identity; try the
        # ledger by email+server first, then fall back to the key form, because the
        # ledger is the authority and the key is only its spelling.
        states = (ServiceObservedState.query
                  .filter(ServiceObservedState.server_id == int(server_id),
                          func.lower(ServiceObservedState.client_email) == email_l)
                  .all())
        if not states:
            # The ledger is the authority; the service key is only its spelling, so
            # fall back to the uuid derived from the email.
            key = lifecycle.make_service_key(
                int(server_id), lifecycle.client_uuid_from_email(email_l))
            states = ServiceObservedState.query.filter_by(service_key=key).all()
        report['observed_state'] = _dump(states, STATE_FIELDS)
        if not states:
            report['found'] = False
            return report
        report['found'] = True

        keys = [row.service_key for row in states]
        events = (ServiceNotificationEvent.query
                  .filter(ServiceNotificationEvent.service_key.in_(keys))
                  .order_by(ServiceNotificationEvent.created_at.desc())
                  .limit(25).all())
        report['events'] = _dump(events, EVENT_FIELDS)
        current = states[0]
        generation = lifecycle.generation_state(current.service_key).get('generation')
        expected_kind = telemetry_state.SERVICE_STATE_TO_NOTIFICATION_KIND.get(
            current.last_state)
        matching = next((event for event in events
                         if event.notification_kind == expected_kind
                         and int(event.lifecycle_generation or 0) == int(generation or 0)),
                        None)
        if not expected_kind:
            classification = 'NOT_APPLICABLE'
        elif matching is None:
            classification = 'COVERAGE_GAP'
        elif matching.status == 'sent':
            classification = 'CONFIRMED_OR_LEGACY_ACCEPTED'
        elif matching.status == 'gateway_accepted':
            classification = 'GATEWAY_ACCEPTED_UNCONFIRMED'
        elif matching.status == 'superseded':
            classification = 'STALE_SUPERSEDED'
        elif matching.status == 'skipped':
            classification = 'POLICY_SUPPRESSED'
        elif matching.status in ('pending', 'retry', 'sending'):
            classification = 'RETRY_SCHEDULED'
        else:
            classification = 'NEEDS_ATTENTION'
        report['notification_coverage'] = {
            'current_state': current.last_state,
            'generation': generation,
            'expected_kind': expected_kind,
            'obligation_present': matching is not None,
            'classification': classification,
        }

        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(hours)))
        logs = (WhatsappBotLog.query
                .filter(WhatsappBotLog.email == email_l,
                        WhatsappBotLog.server_id == int(server_id),
                        WhatsappBotLog.sent_at >= cutoff)
                .order_by(WhatsappBotLog.sent_at.desc()).all())
        report['automation_log'] = [
            {'event': row.event, 'sent_at': _iso(row.sent_at)} for row in logs]
        report['automation_log_by_event'] = {}
        for row in logs:
            report['automation_log_by_event'].setdefault(row.event, []).append(
                _iso(row.sent_at))

        send_log = (SmsSendLog.query
                    .filter(func.lower(SmsSendLog.email) == email_l,
                            SmsSendLog.server_id == int(server_id),
                            SmsSendLog.created_at >= cutoff)
                    .order_by(SmsSendLog.created_at.desc()).limit(50).all())
        report['sms_send_log'] = [
            {'state': row.state, 'status': row.status, 'reason': row.reason,
             'job_id': row.job_id, 'created_at': _iso(row.created_at),
             'gateway_sent_at': row.gateway_sent_at,
             'gateway_outcome': row.gateway_outcome}
            for row in send_log]

        report['pipeline'] = {
            'mode': depletion_pipeline.mode(),
            'detection_enabled': depletion_pipeline.detection_enabled(),
            'delivery_enabled': depletion_pipeline.delivery_enabled(),
            'legacy_sender_active': depletion_pipeline.legacy_sender_active(),
        }

        cfg = messaging._get_sms_runtime_settings()
        report['settings'] = {name: cfg.get(name) for name in SAFE_SETTINGS}

        # ── the verdict: the real gates, in the real order ────────────────────
        report['verdict'] = _verdict(messaging, telemetry_state, states[0], events,
                                    logs, cfg, server_id, email_l)
    return report


def _verdict(messaging, telemetry_state, state_row, events, logs, cfg, server_id,
             email_l) -> dict:
    """Which gate refuses this account's newest event, evaluated with real code."""
    verdict = {'gates': []}

    def gate(name, result, detail=None):
        """Record one gate and return whether it PASSES.

        The gates are evaluated in the delivery path's order and this return value is
        what stops the walk at the first refusal: the operator wants the one reason,
        not a list they have to interpret.
        """
        verdict['gates'].append({'gate': name, 'result': result, 'detail': detail})
        return result == 'ok'

    if not events:
        gate('event_exists', 'FAIL', 'no ServiceNotificationEvent for this service: '
                                     'the transition was never detected')
        verdict['root_cause'] = 'no_event'
        return verdict
    event = events[0]
    state = telemetry_state.sms_state_for(event.state) or str(event.state or '')
    verdict['event'] = {'event_id': event.event_id, 'state': state,
                        'canonical_state': event.state, 'status': event.status,
                        'last_error': event.last_error,
                        'superseded_reason': event.superseded_reason,
                        'next_attempt_at': _iso(event.next_attempt_at),
                        'attempt_count': event.attempt_count}
    gate('event_exists', 'ok', 'newest event %s (%s) status=%s'
         % (event.event_id, state, event.status))

    if not gate('sms_automation_enabled', 'ok' if cfg.get('enabled') else 'BLOCKS',
                'enabled=%s' % cfg.get('enabled')):
        verdict['root_cause'] = 'sms_disabled'
        return verdict

    trigger_key = messaging._DEPLETION_TRIGGER_KEYS.get(state)
    trigger_value = cfg.get(trigger_key) if trigger_key else None
    if not gate('per_state_trigger', 'ok' if trigger_value else 'BLOCKS',
                '%s=%s' % (trigger_key, trigger_value)):
        verdict['root_cause'] = 'trigger_disabled'
        return verdict

    if not gate('not_reseller_owned',
                'BLOCKS' if messaging._account_has_reseller_owner(server_id, email_l)
                else 'ok', 'reseller ownership check'):
        verdict['root_cause'] = 'reseller_owned'
        return verdict

    current = {}
    try:
        current = messaging.lifecycle_service.generation_state(event.service_key) or {}
    except Exception as exc:  # pragma: no cover - host dependent
        current = {}
        verdict.setdefault('warnings', []).append('generation read failed: %s' % exc)
    live_generation = current.get('generation')
    generation = int(event.lifecycle_generation or 0)
    if live_generation is not None and generation and int(live_generation) != generation:
        gate('lifecycle_generation', 'BLOCKS',
             'event generation %s, live %s' % (generation, live_generation))
        verdict['root_cause'] = 'lifecycle_generation_advanced'
        return verdict
    gate('lifecycle_generation', 'ok',
         'event %s, live %s' % (generation, live_generation))

    rows = messaging._cached_snapshot_clients(server_id, email_l)
    if not rows:
        gate('live_state', 'UNPROVEN', 'the snapshot holds no row for this account; '
                                       'run this in a process that has one (or with '
                                       'Redis reachable) to prove the state')
    else:
        seen = sorted({messaging._classify_cached_client_state(row, cfg) for row in rows})
        gate('live_state', 'ok' if seen == [state] else 'BLOCKS',
             'snapshot says %s, event says %s' % (', '.join(seen), state))
        if seen != [state]:
            verdict['root_cause'] = 'state_changed_since_detection'
            return verdict

    cooldown_hours = (cfg.get('cooldown_hours') or {}).get(state, 24)
    kinds = messaging.cooldown_events_for_state(state)
    matching = sorted({row.event for row in logs
                       if str(row.event or '').lower() in [k.lower() for k in kinds]})
    remaining = messaging._cooldown_remaining_seconds(email_l, server_id, state,
                                                     cooldown_hours)
    verdict['cooldown'] = {
        'cooldown_hours': cooldown_hours,
        'events_that_count_for_this_state': list(kinds),
        'matching_log_events_in_window': matching,
        'remaining_seconds': remaining,
    }
    if remaining > 0:
        gate('cooldown', 'DEFERS', '%ss left on the same-kind cooldown (matched %s)'
             % (remaining, ', '.join(matching) or 'none'))
        verdict['root_cause'] = 'cooldown_active_deferred'
        return verdict
    gate('cooldown', 'ok', 'no same-kind message inside %sh' % cooldown_hours)

    other_kinds = sorted({row.event for row in logs}) if logs else []
    verdict['prior_automation_events_ignored'] = [
        name for name in other_kinds
        if str(name or '').lower() not in [k.lower() for k in kinds]]

    recipient = None
    if rows:
        recipient = messaging._extract_iran_mobile_from_text(
            email_l, rows[0].get('comment') or '')
    if not gate('recipient', 'ok' if recipient else 'BLOCKS',
                'a phone number is derivable from the account' if recipient
                else 'no recipient in the email or comment'):
        verdict['root_cause'] = 'no_recipient'
        return verdict

    if not gate('quiet_hours', 'DEFERS' if messaging._sms_in_quiet_hours(cfg) else 'ok',
                'quiet %s-%s enabled=%s' % (cfg.get('quiet_start'), cfg.get('quiet_end'),
                                            cfg.get('quiet_enabled'))):
        verdict['root_cause'] = 'quiet_hours'
        return verdict

    verdict['root_cause'] = 'no_gate_blocks_now'
    verdict['note'] = ('No gate refuses this event right now: it is either in flight, '
                       'already delivered, or its blocking condition has since passed')
    return verdict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', required=True, help='the account email')
    parser.add_argument('--server-id', required=True, type=int)
    parser.add_argument('--hours', type=int, default=72,
                        help='how far back to read the automation/send logs')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    report = collect(args.email, args.server_id, args.hours)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get('found') else 2

    acct = report['account']
    print('=' * 78)
    print('SMS delivery forensics: %s on server %s (log window %sh)'
          % (acct['email'], acct['server_id'], acct['window_hours']))
    print('=' * 78)
    if not report.get('found'):
        print('NOT FOUND in ServiceObservedState for this server.')
        print('Check the email spelling, the server id, and whether the account was '
              'ever observed by the ledger.')
        return 2
    print('\n-- observed state --')
    for row in report['observed_state']:
        for name, value in row.items():
            print('  %-20s %s' % (name, value))
    print('\n-- notification events (newest first) --')
    for event in report['events']:
        print('  %s  state=%s kind=%s status=%s attempts=%s'
              % (event['event_id'], event['state'], event['notification_kind'],
                 event['status'], event['attempt_count']))
        print('      created=%s next_attempt=%s sent=%s'
              % (event['created_at'], event['next_attempt_at'], event['sent_at']))
        print('      last_error=%s superseded_reason=%s'
              % (event['last_error'], event['superseded_reason']))
    print('\n-- pipeline --')
    for name, value in report['pipeline'].items():
        print('  %-22s %s' % (name, value))
    print('\n-- settings (values only, no credentials) --')
    for name, value in report['settings'].items():
        print('  %-24s %s' % (name, value))
    print('\n-- automation log in window (cooldown source) --')
    if not report['automation_log']:
        print('  (none)')
    for row in report['automation_log']:
        print('  %-20s %s' % (row['event'], row['sent_at']))
    print('\n-- SMS send log in window --')
    if not report['sms_send_log']:
        print('  (none)')
    for row in report['sms_send_log']:
        print('  %s %-10s %-10s %s' % (row['created_at'], row['state'], row['status'],
                                       row['reason'] or ''))
    print('\n-- verdict: the gates, in delivery order --')
    for step in report['verdict']['gates']:
        print('  %-24s %-9s %s' % (step['gate'], step['result'], step['detail'] or ''))
    if report['verdict'].get('cooldown'):
        print('\n  cooldown detail: %s' % json.dumps(report['verdict']['cooldown']))
    if report['verdict'].get('prior_automation_events_ignored'):
        print('  prior automation ignored by this state\'s cooldown: %s'
              % ', '.join(report['verdict']['prior_automation_events_ignored']))
    print('\n  ROOT CAUSE: %s' % report['verdict'].get('root_cause'))
    if report['verdict'].get('note'):
        print('  note: %s' % report['verdict']['note'])
    print('=' * 78)
    print('Read-only: nothing was sent, written or claimed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
