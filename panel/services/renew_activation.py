"""Renewal verification in layers, and activation repair that is not a renewal.

Two production facts drive this module:

1. **One boolean cannot describe a renewal.** "Config applied" (expiry and quota
   are what we asked for), "activation converged" (the global client AND every
   attached inbound membership are enabled) and "runtime sync" (the node has
   picked the change up) are three independent facts. Reporting them as one
   ``ok`` is how a renewal shows "Successful" while a customer stays offline -
   and how a client that is enabled globally but disabled inside one inbound
   passes verification.

2. **Repairing activation is not renewing again.** Once the quota/expiry write
   has landed, repeating days/volume/gift/traffic-reset would double-charge the
   customer. The repair path here is activation-only, keyed to the same operation,
   bounded, and it refuses to run at all while the account is still depleted (a
   panel would simply disable it again).

The module is deliberately free of HTTP and DB access of its own: every reader
and writer is injected. That is what makes the layer semantics testable without a
panel, a network or a sleep.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── final states ─────────────────────────────────────────────────────────────
STATE_APPLIED_ACTIVE = 'APPLIED_ACTIVE'
STATE_ACTIVATION_PENDING = 'CONFIG_APPLIED_ACTIVATION_PENDING'
STATE_PARTIALLY_APPLIED = 'PARTIALLY_APPLIED'
STATE_NOT_APPLIED = 'NOT_APPLIED'
STATE_AUTH_DEGRADED = 'AUTH_DEGRADED'
STATE_UNKNOWN = 'UNKNOWN'

# ── runtime sync state ───────────────────────────────────────────────────────
RUNTIME_CONVERGED = 'converged'
RUNTIME_PENDING = 'pending'
RUNTIME_NOT_EXPOSED = 'not_exposed'
RUNTIME_FAILED = 'failed'


@dataclass
class ActivationLayers:
    """What each layer of the panel says, kept separate on purpose."""

    global_found: bool = False
    global_enable: bool = None
    global_expiry: int = None
    global_total: int = None

    expected_inbound_ids: list = field(default_factory=list)
    found_inbound_ids: list = field(default_factory=list)
    enabled_inbound_ids: list = field(default_factory=list)
    disabled_inbound_ids: list = field(default_factory=list)
    missing_inbound_ids: list = field(default_factory=list)
    unknown_inbound_ids: list = field(default_factory=list)

    traffic_available: bool = False
    traffic_enable: bool = None
    traffic_up: int = None
    traffic_down: int = None
    traffic_total: int = None
    traffic_expiry: int = None

    node_pending: bool = None
    config_applied: bool = False
    activation_converged: bool = False
    runtime_sync_state: str = RUNTIME_NOT_EXPOSED
    final_state: str = STATE_UNKNOWN
    notes: list = field(default_factory=list)
    reason_codes: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    read_error: str | None = None

    def as_dict(self) -> dict:
        return {
            'global': {
                'found': self.global_found,
                'enable': self.global_enable,
                'expiryTime': self.global_expiry,
                'totalGB': self.global_total,
            },
            'memberships': {
                'expected_ids': list(self.expected_inbound_ids),
                'found_ids': list(self.found_inbound_ids),
                'enabled_ids': list(self.enabled_inbound_ids),
                'disabled_ids': list(self.disabled_inbound_ids),
                'missing_ids': list(self.missing_inbound_ids),
                'unknown_ids': list(self.unknown_inbound_ids),
            },
            'traffic': {
                'available': self.traffic_available,
                'enable': self.traffic_enable,
                'up': self.traffic_up,
                'down': self.traffic_down,
                'total': self.traffic_total,
                'expiry': self.traffic_expiry,
            },
            'node_pending': self.node_pending,
            'config_applied': self.config_applied,
            'activation_converged': self.activation_converged,
            'runtime_sync_state': self.runtime_sync_state,
            'final_state': self.final_state,
            'notes': list(self.notes),
            'reason_codes': list(self.reason_codes),
            'evidence': dict(self.evidence),
        }


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── expiry normalisation ─────────────────────────────────────────────────────
#: Tolerance for comparing two absolute expiry timestamps (or two pending
#: durations). The panel writes what it was sent, so the slack is only for a
#: value it rounded or recomputed on its own clock.
EXPIRY_TOLERANCE_MS = 60_000
#: Tolerance for accepting a delayed-start expiry the panel converted into an
#: absolute deadline at write time. A slow v3.4 panel can take 10-20 s to apply a
#: write, and the anchor is recorded just before the request is sent.
EXPIRY_CONVERSION_TOLERANCE_MS = 120_000

MODE_ABSOLUTE = 'absolute'
MODE_PENDING = 'pending'
MODE_CONVERTED = 'converted'
MODE_MISMATCH = 'mismatch'
MODE_UNKNOWN = 'unknown'


def normalize_expiry(expected, observed, *, anchor_ms=None, now_ms=None,
                     tolerance_ms=None,
                     conversion_tolerance_ms=None) -> dict:
    """Compare the expiry a renewal asked for with the one the panel reports back.

    3x-ui stores a NEGATIVE ``expiryTime`` as "this many milliseconds after first
    use" (delayed start) and a positive one as an absolute deadline. A v3 panel
    that receives the negative form can convert it to an absolute deadline at
    write time; after that the two numbers can never be equal, and comparing them
    directly (the old behaviour) reported a correctly applied 31-day renewal as
    ``EXPIRY_MISMATCH`` with a difference of ~20807 days.

    Four cases are distinguished, and only two of them are "unknown":

    * both absolute (or both zero) -> must agree within ``tolerance_ms``;
    * both negative -> the pending duration must agree within ``tolerance_ms``;
    * expected negative, observed positive -> CONVERTED: the deadline must equal
      the conversion anchor plus the requested duration (``anchor_ms`` is the
      moment the write was sent, so a Re-check minutes or hours later still
      compares against the same instant instead of a drifting "now");
    * expected positive, observed negative -> the reverse conversion: the
      remaining pending duration must equal what was left of the deadline.

    Returns ``{'matched', 'mode', 'target_ms', 'anchor_ms', 'difference_ms',
    'tolerance_ms'}``; ``matched`` is None only when a comparison is impossible.
    """
    tolerance = _as_int(tolerance_ms, EXPIRY_TOLERANCE_MS)
    conversion_tolerance = _as_int(conversion_tolerance_ms,
                                   EXPIRY_CONVERSION_TOLERANCE_MS)
    want = _as_int(expected)
    got = _as_int(observed)
    anchor = _as_int(anchor_ms)
    now = _as_int(now_ms)
    result = {'matched': None, 'mode': MODE_UNKNOWN, 'target_ms': None,
              'anchor_ms': anchor, 'difference_ms': None,
              'tolerance_ms': tolerance}
    if want is None or got is None:
        return result
    if want == 0 and got == 0:
        result.update(matched=True, mode=MODE_ABSOLUTE, target_ms=0,
                      difference_ms=0)
        return result
    if (want >= 0) == (got >= 0):
        # Same representation: a plain numeric comparison is the whole rule.
        delta = got - want
        result.update(matched=abs(delta) <= tolerance,
                      mode=MODE_ABSOLUTE if want >= 0 else MODE_PENDING,
                      target_ms=want, difference_ms=delta)
        return result
    # Opposite signs: the panel converted one representation into the other.
    # Anchor the conversion on when the write was sent, never on "now", which
    # drifts by however long it has been since.
    base = anchor if anchor is not None else now
    if base is None:
        result['mode'] = MODE_UNKNOWN
        return result
    if want < 0:
        target = base + abs(want)
    else:
        # The panel kept a pending duration for an absolute request: what it
        # stored is the time that was left of the requested deadline.
        target = -(want - base)
    delta = got - target
    result.update(matched=abs(delta) <= conversion_tolerance,
                  mode=MODE_CONVERTED, target_ms=target, difference_ms=delta)
    if not result['matched']:
        result['mode'] = MODE_MISMATCH
    return result


def _clients_of(inbound) -> list:
    """The client rows inside one inbound, tolerating a JSON-string settings field."""
    import json
    settings = (inbound or {}).get('settings')
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except (TypeError, ValueError):
            return []
    if not isinstance(settings, dict):
        return []
    clients = settings.get('clients')
    return [row for row in clients if isinstance(row, dict)] if isinstance(clients, list) else []


def membership_map(inbounds, email, *, inbound_ids=None) -> dict:
    """Map inbound id -> the client row inside it, for the inbounds that matter.

    ``inbound_ids`` restricts which inbounds are consulted (the panel's own
    ``inboundIds`` for the client); with no restriction every inbound is scanned,
    which is what the legacy path needs because it has no membership list at all.
    A key present with value None means "this inbound does not contain the client"
    - deliberately distinct from an absent key, which means "not inspected".
    """
    wanted = None
    if inbound_ids is not None:
        wanted = set()
        for raw in inbound_ids:
            value = _as_int(raw)
            if value is not None:
                wanted.add(value)
    result = {}
    target = str(email or '').strip().lower()
    for inbound in (inbounds or []):
        if not isinstance(inbound, dict):
            continue
        inbound_id = _as_int(inbound.get('id'))
        if inbound_id is None:
            continue
        if wanted is not None and inbound_id not in wanted:
            continue
        row = None
        for client in _clients_of(inbound):
            if str(client.get('email') or '').strip().lower() == target:
                row = client
                break
        result[inbound_id] = row
    return result


def classify_layers(layers: ActivationLayers, *, expected: dict,
                    write_may_be_partial: bool = False,
                    auth_degraded: bool = False, before=None,
                    mutation=None, read=None) -> ActivationLayers:
    """Turn the measured layers into the three facts plus one final state.

    An unknown layer can only make the result *less* certain, never more: a
    missing membership list is reported as missing, and an unavailable traffic
    row cannot confirm activation.
    """
    expected = dict(expected or {})
    want_expiry = _as_int(expected.get('expiryTime'))
    want_total = _as_int(expected.get('totalGB'))
    before = dict(before or layers.evidence.get('before') or {})
    mutation = dict(mutation or layers.evidence.get('mutation') or {})
    read = dict(read or layers.evidence.get('read') or {})
    observed = {'expiryTime': layers.global_expiry, 'totalGB': layers.global_total,
                'enable': layers.global_enable,
                'memberships': layers.as_dict()['memberships'],
                'traffic_enable': layers.traffic_enable,
                'up': layers.traffic_up, 'down': layers.traffic_down,
                'nodePending': layers.node_pending}
    used = (layers.traffic_up + layers.traffic_down
            if layers.traffic_up is not None and layers.traffic_down is not None else None)
    observed['remaining_quota'] = (max(0, layers.global_total - used)
                                   if layers.global_total and used is not None else None)
    fields = {}
    field_expected = dict(expected)
    field_expected.update(enable=expected.get('enable', True), traffic_enable=True, nodePending=False,
                          memberships={'expected_ids': list(layers.expected_inbound_ids),
                                       'enabled_ids': list(layers.expected_inbound_ids)},
                          remaining_quota='unlimited' if want_total == 0 else '>0')
    if mutation.get('traffic_reset', {}).get('requested'):
        field_expected.update(up=0, down=0)
    # The expiry is compared through the representation-aware helper, never by
    # number equality: a delayed-start expiry the panel converted into a deadline
    # is a MATCH, and the raw numbers of the two forms are never equal.
    expiry_cmp = normalize_expiry(want_expiry, layers.global_expiry,
                                  anchor_ms=expected.get('anchor_ms'),
                                  now_ms=expected.get('now_ms'))
    for name, value in observed.items():
        known = value is not None and (name != 'memberships' or bool(layers.expected_inbound_ids))
        if name == 'expiryTime':
            matched = expiry_cmp['matched'] is True
            if expiry_cmp['mode'] == MODE_CONVERTED:
                # The comparison that decided this field, kept beside it: the raw
                # expected/observed pair no longer shows how they were related.
                field_expected[name] = expiry_cmp['target_ms']
        else:
            matched = known and name in field_expected and value == field_expected[name]
        changed = (known and before.get('status', 'ok') == 'ok'
                   and name in before and before[name] is not None and value != before[name])
        source_layer = ('traffic' if name in ('traffic_enable', 'up', 'down', 'remaining_quota')
                        else 'memberships' if name == 'memberships' else 'global')
        layer_read = read.get('layers', {}).get(source_layer, read)
        fields[name] = {'before': before.get(name), 'expected': field_expected.get(name),
                        'observed': value, 'value_matched': bool(matched),
                        'change_confirmed': bool(changed),
                        'source_endpoint': layer_read.get('source_endpoint'),
                        'read_timestamp_ms': read.get('timestamp_ms'),
                        'read_status': layer_read.get('status', 'unknown')}
    delta = (layers.global_expiry - want_expiry
             if want_expiry is not None and layers.global_expiry is not None else None)
    layers.evidence = {'before': before, 'expected': expected, 'observed': observed,
                       'fields': fields, 'read': read, 'mutation': mutation,
                       'expiry': {'expected_ms': want_expiry, 'observed_ms': layers.global_expiry,
                                  'difference_ms': delta,
                                  'difference_seconds': delta / 1000 if delta is not None else None,
                                  'difference_days': delta / 86400000 if delta is not None else None,
                                  'mode': expiry_cmp['mode'],
                                  'matched': expiry_cmp['matched'],
                                  'target_ms': expiry_cmp['target_ms'],
                                  'anchor_ms': expiry_cmp['anchor_ms'],
                                  'conversion_difference_ms': expiry_cmp['difference_ms'],
                                  'tolerance_ms': expiry_cmp['tolerance_ms']}}
    reasons = []
    if auth_degraded:
        reasons.append('AUTH_FAILED')
    if not layers.global_found or read.get('status') == 'failed':
        reasons.append('PANEL_READ_FAILED')
    if read.get('status') == 'stale':
        reasons.append('STALE_READ')
    if layers.global_found:
        for name, code in [('expiryTime', 'EXPIRY_MISMATCH'), ('totalGB', 'QUOTA_MISMATCH')]:
            if expected.get(name) is not None and observed.get(name) is not None and not fields[name]['value_matched']:
                reasons.append(code)
    if layers.global_enable is False:
        reasons.append('GLOBAL_DISABLED')
    if layers.disabled_inbound_ids:
        reasons.append('MEMBERSHIP_DISABLED')
    if layers.missing_inbound_ids:
        reasons.append('MEMBERSHIP_MISSING')
    if used is not None and layers.global_total and used >= layers.global_total:
        reasons.append('TRAFFIC_DEPLETED')
    if mutation.get('traffic_reset', {}).get('accepted') is False:
        reasons.append('TRAFFIC_RESET_FAILED')
    if layers.node_pending is True:
        reasons.append('NODE_SYNC_PENDING')
    if mutation.get('accepted') is False:
        reasons.append('MUTATION_REJECTED' if mutation.get('rejected') else 'MUTATION_OUTCOME_UNKNOWN')
    elif mutation.get('accepted') is None:
        reasons.append('MUTATION_OUTCOME_UNKNOWN')
    layers.reason_codes = reasons

    if auth_degraded:
        layers.final_state = STATE_AUTH_DEGRADED
        layers.notes.append('the panel API could not be proven: fix the credential '
                            'or token scope before renewing')
        return layers

    config_expiry_ok = (want_expiry is None
                        or (layers.global_found
                            and expiry_cmp['matched'] is True))
    config_total_ok = (want_total is None
                       or (layers.global_found and layers.global_total == want_total))
    layers.config_applied = bool(layers.global_found and config_expiry_ok and config_total_ok
                                 and (want_expiry is not None or want_total is not None))

    membership_ok = bool(layers.expected_inbound_ids) and not layers.missing_inbound_ids
    memberships_enabled = (membership_ok
                           and not layers.disabled_inbound_ids
                            and not layers.unknown_inbound_ids
                           and len(layers.found_inbound_ids) == len(layers.expected_inbound_ids))
    global_enabled = layers.global_enable is True
    traffic_ok = (layers.traffic_enable is True) if layers.traffic_available else True
    traffic_ok = traffic_ok and 'TRAFFIC_DEPLETED' not in reasons and 'TRAFFIC_RESET_FAILED' not in reasons
    layers.activation_converged = bool(layers.global_found and global_enabled
                                       and memberships_enabled and traffic_ok)
    if layers.traffic_available and layers.traffic_enable is False:
        layers.notes.append('the traffic row still reports the client as disabled')

    if layers.node_pending is True:
        layers.runtime_sync_state = RUNTIME_PENDING
        layers.notes.append('the panel accepted the change but its node has not '
                            'synchronised yet (nodePending)')
    elif layers.node_pending is False:
        layers.runtime_sync_state = RUNTIME_CONVERGED
    elif layers.traffic_available:
        # Nothing exposed a pending flag. The traffic read is authoritative about
        # the panel's own view; runtime convergence on the node is still not
        # something EVE can see, and saying so is better than inferring it from
        # activity endpoints (activeInbounds/onlines describe traffic, not whether
        # this credential is installed).
        layers.runtime_sync_state = RUNTIME_NOT_EXPOSED
    else:
        layers.runtime_sync_state = RUNTIME_NOT_EXPOSED

    if not layers.global_found or read.get('status') in ('failed', 'stale'):
        layers.final_state = STATE_UNKNOWN
        layers.config_applied = False
        layers.activation_converged = False
        layers.notes.append('authoritative panel verification is unavailable')
        return layers
    config_names = [name for name in ('expiryTime', 'totalGB', 'enable') if expected.get(name) is not None]
    unchanged = bool(config_names) and all(
        before.get(name) is not None and observed.get(name) is not None
        and before[name] == observed[name] for name in config_names)
    if layers.config_applied and unchanged and mutation.get('accepted') is not True:
        layers.final_state = STATE_UNKNOWN
        layers.activation_converged = False
        layers.notes.append('matching existing values does not confirm this mutation')
        return layers
    if not layers.config_applied:
        changed = any(item['change_confirmed'] for item in fields.values())
        layers.final_state = (STATE_PARTIALLY_APPLIED if changed else
                              STATE_NOT_APPLIED if unchanged and read.get('status') == 'ok'
                              and mutation.get('rejected') is True else STATE_UNKNOWN)
        return layers
    if layers.runtime_sync_state == RUNTIME_PENDING:
        # The configuration is committed but the panel's own node has not picked it
        # up: the customer may still be offline, so this is NOT "applied and active"
        # even when every enable flag reads true. Reporting success here is exactly
        # the "Renewal Successful while the account stays disabled" complaint.
        layers.final_state = STATE_ACTIVATION_PENDING
        return layers
    if layers.activation_converged:
        layers.final_state = (STATE_APPLIED_ACTIVE
                             if layers.runtime_sync_state != RUNTIME_FAILED
                             else STATE_ACTIVATION_PENDING)
    else:
        layers.final_state = STATE_ACTIVATION_PENDING
    return layers


def analyze_activation(*, expected: dict, global_client: dict | None,
                       inbound_ids=None, memberships: dict | None = None,
                       traffic: dict | None = None, node_pending=None,
                       write_may_be_partial: bool = False,
                       auth_degraded: bool = False,
                       aliases=None) -> ActivationLayers:
    """Build the layer record from raw panel reads.

    ``memberships`` maps inbound id -> the client row inside that inbound (or None
    when the inbound does not contain the client). ``aliases`` maps an inbound id
    to extra ids that denote the same inbound (a membership may be keyed by the
    inbound id the panel reports, which can differ from the requested one).
    """
    layers = ActivationLayers()
    global_client = global_client if isinstance(global_client, dict) else None
    if global_client:
        layers.global_found = True
        layers.global_enable = global_client.get('enable') if isinstance(global_client.get('enable'), bool) else None
        layers.global_expiry = _as_int(global_client.get('expiryTime'))
        layers.global_total = _as_int(global_client.get('totalGB'))

    expected_ids = []
    for raw in (inbound_ids or []):
        value = _as_int(raw)
        if value is not None and value not in expected_ids:
            expected_ids.append(value)
    # The inbound the renewal was requested for is a membership too, even if the
    # client read did not list it: that is exactly the divergence worth catching.
    requested = _as_int((expected or {}).get('inbound_id'))
    if requested is not None and requested not in expected_ids:
        expected_ids.append(requested)
    layers.expected_inbound_ids = expected_ids

    memberships = memberships or {}
    aliases = aliases or {}
    for inbound_id in expected_ids:
        keys = [inbound_id] + [k for k in aliases.get(inbound_id, [])]
        row = None
        for key in keys:
            candidate = memberships.get(key)
            if isinstance(candidate, dict):
                row = candidate
                break
        if row is None:
            layers.missing_inbound_ids.append(inbound_id)
            continue
        layers.found_inbound_ids.append(inbound_id)
        if row.get('enable') is True:
            layers.enabled_inbound_ids.append(inbound_id)
        elif row.get('enable') is False:
            layers.disabled_inbound_ids.append(inbound_id)
        else:
            layers.unknown_inbound_ids.append(inbound_id)

    if isinstance(traffic, dict) and traffic.get('available'):
        layers.traffic_available = True
        layers.traffic_enable = traffic.get('enable')
        layers.traffic_up = _as_int(traffic.get('up'))
        layers.traffic_down = _as_int(traffic.get('down'))
        layers.traffic_total = _as_int(traffic.get('total'))
        layers.traffic_expiry = _as_int(traffic.get('expiry'))
    layers.node_pending = node_pending
    return classify_layers(layers, expected=expected,
                           write_may_be_partial=write_may_be_partial,
                           auth_degraded=auth_degraded)


def account_is_still_depleted(layers: ActivationLayers, expected: dict) -> tuple:
    """Would the panel disable this client again? Returns (depleted, reason).

    Activation is only meaningful once the account is no longer depleted: 3x-ui
    disables a client whose quota or expiry is exhausted on its next traffic
    cycle, so enabling first and correcting later always loses the race.
    """
    want_expiry = _as_int((expected or {}).get('expiryTime'))
    want_total = _as_int((expected or {}).get('totalGB'))
    now_ms = _as_int((expected or {}).get('now_ms'))
    if layers.global_found:
        observed_expiry = layers.global_expiry
        observed_total = layers.global_total
        used = None
        if layers.traffic_available:
            up = layers.traffic_up or 0
            down = layers.traffic_down or 0
            used = up + down
        if want_total and want_total > 0:
            if observed_total is not None and int(observed_total) <= 0:
                return True, 'the panel still reports no quota'
            if used is not None and observed_total is not None:
                if int(observed_total) - int(used) <= 0:
                    return True, 'the remaining volume is still zero after the write'
        if want_expiry and want_expiry > 0 and now_ms:
            if observed_expiry is not None and int(observed_expiry) <= int(now_ms):
                return True, 'the expiry is still in the past after the write'
    return False, None


#: What an operator can DO about each failure, and whether an enable repair can
#: still fix it. Anything not listed here is a config/read problem: retrying an
#: enable would be noise on top of a renewal that needs a human.
FAILURE_SCOPES = {
    'GLOBAL_DISABLED': ('enable', True),
    'MEMBERSHIP_DISABLED': ('membership', True),
    'MEMBERSHIP_MISSING': ('membership', True),
    'MEMBERSHIP_UNKNOWN': ('membership', False),
    'TRAFFIC_DISABLED': ('traffic', True),
    'EXPIRY_MISMATCH': ('config', False),
    'QUOTA_MISMATCH': ('config', False),
    'TRAFFIC_DEPLETED': ('traffic', False),
    'TRAFFIC_RESET_FAILED': ('traffic', False),
    'NODE_SYNC_PENDING': ('runtime', False),
    'PANEL_READ_FAILED': ('read', False),
    'AUTH_FAILED': ('auth', False),
    'MUTATION_REJECTED': ('write', False),
    'MUTATION_OUTCOME_UNKNOWN': ('write', False),
}


def activation_failures(layers) -> list:
    """The specific steps that kept this renewal from being active.

    The dashboard used to show one vague word ("Partial") for every one of these,
    so an operator could not tell a disabled membership in inbound 17 from an
    expiry the panel never applied. Each entry names the layer, the inbound ids
    when there are any, and whether an enable repair can still fix it.

    ``repairable`` is what "Force Enable" acts on: it re-runs the enable steps and
    nothing else - never days, volume, a gift or a traffic reset.
    """
    failures = []
    for code in (getattr(layers, 'reason_codes', None) or []):
        scope, repairable = FAILURE_SCOPES.get(code, (None, False))
        if scope is None:
            continue
        item = {'code': code, 'scope': scope, 'repairable': bool(repairable)}
        if code == 'MEMBERSHIP_DISABLED':
            item['inbound_ids'] = list(getattr(layers, 'disabled_inbound_ids', []) or [])
        elif code in ('MEMBERSHIP_MISSING', 'MEMBERSHIP_UNKNOWN'):
            item['inbound_ids'] = list(getattr(layers, 'missing_inbound_ids', []) or [])
        elif code == 'EXPIRY_MISMATCH':
            expiry = (getattr(layers, 'evidence', None) or {}).get('expiry') or {}
            item.update(expected_ms=expiry.get('expected_ms'),
                        observed_ms=expiry.get('observed_ms'),
                        difference_days=expiry.get('difference_days'),
                        mode=expiry.get('mode'))
        failures.append(item)
    return failures


def converge_activation(*, verify, repair, attempts: int = 3,                        sleep=None, should_repair=None, initial=None):
    """Repair activation only, bounded, and never by repeating the renewal.

    ``initial`` is the layer measurement the caller has ALREADY made; the loop starts
    from it and only calls ``verify()`` after a repair. Re-reading before the first
    repair would be worse than redundant: the fresh read can already show the panel
    converged (its reads are not instantaneous), which would silently skip a repair
    for a client that was measured inactive a moment earlier.

    ``repair`` performs the activation-only write (a capability-correct enable call)
    and must not touch amounts, expiry, traffic or billing. ``should_repair`` may veto
    a repair (e.g. the panel reports nodePending, or the account is still depleted).

    Returns ``(layers, history)``. ``sleep`` is injected so tests never wait.
    """
    history = []
    layers = initial if initial is not None else verify()
    if 'TRAFFIC_RESET_FAILED' in layers.reason_codes:
        return layers, [{'attempt': 0, 'repair': 'skipped', 'reason': 'traffic reset failed'}]
    history.append({'attempt': 0, 'final_state': layers.final_state,
                    'activation_converged': layers.activation_converged})
    attempt = 0
    while attempt < max(0, int(attempts)) and not layers.activation_converged:
        allowed, reason = (True, 'activation not converged')
        if should_repair is not None:
            allowed, reason = should_repair(layers)
        if not allowed:
            history.append({'attempt': attempt + 1, 'repair': 'skipped', 'reason': reason})
            break
        attempt += 1
        try:
            repair_result = repair()
        except Exception as exc:                       # pragma: no cover - defensive
            history.append({'attempt': attempt, 'repair': 'raised', 'error': str(exc)})
            break
        history.append({'attempt': attempt, 'repair': 'called',
                        'result': _summarise_mutation(repair_result)})
        if sleep is not None:
            sleep(attempt)
        layers = verify()
        history.append({'attempt': attempt, 'final_state': layers.final_state,
                        'activation_converged': layers.activation_converged})
    return layers, history


def _summarise_mutation(result) -> dict:
    """A non-secret summary of a mutation result object (or a plain value)."""
    if result is None:
        return {}
    for attr in ('as_dict',):
        fn = getattr(result, attr, None)
        if callable(fn):
            try:
                data = fn()
                if isinstance(data, dict):
                    return {k: data.get(k) for k in
                            ('transport_ok', 'panel_success', 'node_pending',
                             'skipped', 'partially_applied', 'need_restart', 'error')}
            except Exception:                          # pragma: no cover - defensive
                return {}
    if isinstance(result, dict):
        return {k: result.get(k) for k in
                ('transport_ok', 'panel_success', 'node_pending', 'skipped',
                 'partially_applied', 'need_restart', 'error')}
    return {'value': bool(result)}
