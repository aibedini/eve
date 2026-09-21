"""One place that decides WHICH panel API a client mutation may use, and WHY.

The recurring renewal bug (config applied, client left inactive) came from routing
mutations with a single boolean: ``server_is_v3()``. A boolean cannot express the
facts a mutation actually depends on, and it conflates four very different
answers:

* the first-class client API exists (a ROUTE fact);
* the credential is rejected (an AUTH fact);
* the token's scope is too narrow (an AUTHZ fact);
* the request never reached a verdict (a TRANSPORT fact).

Only the first may select the legacy inbound API. Collapsing the other three into
"legacy" is how a scoped token silently turns a modern panel into a v2 one and
leaves a renewed customer disabled.

This module therefore produces a structured capability record plus one
``RenewStrategy``, and it is the only place a version comparison for client
lifecycle behaviour is allowed to live. Routes ask it for a strategy; they never
compare versions themselves.

Evidence discipline: the version -> capability table below is a claim about the
upstream contract. Each entry records the family that introduced the endpoint and
the audit it came from; a family that is not proven keeps the capability OFF, and
an unknown/future version inherits nothing (``UNKNOWN_FUTURE`` chooses operations
only from capabilities that were actually proven).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Optional

# ── client API families ──────────────────────────────────────────────────────
CLIENT_API_LEGACY = 'legacy_inbound'
CLIENT_API_FIRST_CLASS = 'first_class'

# ── probe verdicts (mirrors panel.adapters.xui.PROBE_*) ──────────────────────
PROBE_SUPPORTED = 'SUPPORTED'
PROBE_ROUTE_MISSING = 'ROUTE_MISSING'
PROBE_AUTH_INVALID = 'AUTH_INVALID'
PROBE_SCOPE_INSUFFICIENT = 'SCOPE_INSUFFICIENT'
PROBE_TRANSPORT_ERROR = 'TRANSPORT_ERROR'
PROBE_INVALID_RESPONSE = 'INVALID_RESPONSE'
PROBE_UNKNOWN = 'UNKNOWN'

#: Verdicts that prove NOTHING about the route. They must never fall back to the
#: legacy API, because the panel may well be modern and the credential may simply
#: be wrong: the honest answer is "blocked, fix the credential".
UNPROVEN_PROBE_STATES = (
    PROBE_AUTH_INVALID,
    PROBE_SCOPE_INSUFFICIENT,
    PROBE_TRANSPORT_ERROR,
    PROBE_INVALID_RESPONSE,
    PROBE_UNKNOWN,
)

#: The version family (major, minor) that introduced the first-class client API
#: (/panel/api/clients/*). v3.0.x still used the inbound-based client endpoints;
#: classifying it as first-class because major == 3 is exactly the mistake this
#: module exists to prevent.
FIRST_CLASS_CLIENT_API_FAMILY = (3, 1)
#: nodePending appears in the update response from v3.3.1 (patch matters here).
NODE_PENDING_FAMILY = (3, 3)
NODE_PENDING_MIN_PATCH = 1
#: bulkEnable / bulkDisable appear in v3.5.
BULK_ENABLE_FAMILY = (3, 5)
#: Scoped/expiring API tokens appear in v3.7 (tracked by the compatibility profile
#: too; the profile is the authority when a version is unparsed but certified).
SCOPED_TOKENS_FAMILY = (3, 7)
#: The newest family whose behaviour EVE has audited. A version above this ceiling
#: inherits NOTHING from it: only the primitives the route probe actually proved are
#: offered, because "3.9 looks like 3.8" is a guess and this module exists to stop
#: guesses from reaching a customer's renewal.
KNOWN_BEHAVIOUR_CEILING = (3, 8)


class RenewStrategy(str, Enum):
    """How one renewal must be performed on this panel."""

    #: v2.x and v3.0.x: the client lives inside an inbound.
    LEGACY_INBOUND = 'LEGACY_INBOUND'
    #: v3.1-v3.2: first-class clients, no bulkEnable and no nodePending signal.
    V3_EARLY = 'V3_EARLY'
    #: v3.3-v3.4: first-class clients, update response carries nodePending.
    V3_NODE_PENDING = 'V3_NODE_PENDING'
    #: v3.5-v3.6: bulkEnable exists, and bulkAdjust can revive depleted clients.
    V3_BULK_ENABLE = 'V3_BULK_ENABLE'
    #: v3.7: as above plus scoped/expiring tokens and limitHwid preservation.
    V3_SCOPED = 'V3_SCOPED'
    #: v3.8.x: current auth/error semantics; a failed write may be partial.
    V3_CURRENT = 'V3_CURRENT'
    #: A parsed version newer than anything audited: inherit nothing, use only
    #: proven capabilities.
    UNKNOWN_FUTURE = 'UNKNOWN_FUTURE'
    #: The capability question could not be answered (auth/scope/transport). No
    #: mutation may be attempted; the caller must surface the diagnostic.
    BLOCKED = 'BLOCKED'


@dataclass(frozen=True)
class PanelClientCapabilities:
    """What this panel can actually do, and which evidence says so."""

    client_api_family: str = CLIENT_API_LEGACY
    client_get: bool = False
    client_update: bool = False
    client_traffic: bool = False
    client_reset_traffic: bool = False
    bulk_adjust: bool = False
    bulk_enable: bool = False
    node_pending_response: bool = False
    scoped_tokens: bool = False
    limit_hwid: bool = False
    legacy_inbound_update: bool = True
    legacy_inbound_reset_traffic: bool = True
    version: Optional[str] = None
    version_family: Optional[tuple] = None
    profile: Optional[str] = None
    probe_state: str = PROBE_UNKNOWN
    evidence: dict = field(default_factory=dict)

    @property
    def first_class(self) -> bool:
        return self.client_api_family == CLIENT_API_FIRST_CLASS

    @property
    def probe_proven(self) -> bool:
        return self.probe_state in (PROBE_SUPPORTED, PROBE_ROUTE_MISSING)

    def as_dict(self) -> dict:
        """Credential-free view for the doctor surface and the renew trace."""
        return {
            'client_api_family': self.client_api_family,
            'client_get': self.client_get,
            'client_update': self.client_update,
            'client_traffic': self.client_traffic,
            'client_reset_traffic': self.client_reset_traffic,
            'bulk_adjust': self.bulk_adjust,
            'bulk_enable': self.bulk_enable,
            'node_pending_response': self.node_pending_response,
            'scoped_tokens': self.scoped_tokens,
            'limit_hwid': self.limit_hwid,
            'legacy_inbound_update': self.legacy_inbound_update,
            'legacy_inbound_reset_traffic': self.legacy_inbound_reset_traffic,
            'version': self.version,
            'profile': self.profile,
            'probe_state': self.probe_state,
            'evidence': dict(self.evidence or {}),
        }


def _family_at_least(version, family, *, min_patch=None) -> bool:
    """True when ``version`` is at or above ``family``.

    Patch is only consulted for the family it is specified for, because a patch
    threshold is a one-off fact (nodePending arrived in 3.3.1, not 3.3.0) and
    applying it to every later family would be wrong.
    """
    if version is None or not getattr(version, 'is_parsed', False):
        return False
    current = (int(version.major), int(version.minor))
    if current != tuple(family):
        return current > tuple(family)
    if min_patch is None:
        return True
    return int(version.patch or 0) >= int(min_patch)


def capabilities_from_version(version, *, profile=None, client_route_proven=None,
                              probe_state: str = PROBE_UNKNOWN) -> PanelClientCapabilities:
    """Build the capability record from a version, a profile and the route verdict.

    ``client_route_proven`` is the authoritative word on the family:
      * True  -> first-class client API exists;
      * False -> the route is absent (legacy);
      * None  -> unproven, so the version is the only hint available.

    A version hint may never *downgrade* a proven route, and an unproven route may
    never be guessed into first-class on the strength of a version number alone:
    that guess is what puts a v3.0 panel on /clients/* and fails the renewal.
    """
    version_display = getattr(version, 'display', None) if version else None
    family = getattr(version, 'family', None) if version else None
    version_says_first_class = _family_at_least(version, FIRST_CLASS_CLIENT_API_FAMILY)

    if client_route_proven is True:
        first_class = True
    elif client_route_proven is False:
        first_class = False
    else:
        first_class = bool(version_says_first_class)

    evidence = {
        'version': version_display,
        'profile': getattr(profile, 'name', None),
        'route_probe': probe_state,
        'client_api_family': ('probe' if client_route_proven is not None
                              else 'version_hint'),
    }
    if not first_class:
        return PanelClientCapabilities(
            client_api_family=CLIENT_API_LEGACY,
            version=version_display, version_family=family,
            profile=getattr(profile, 'name', None),
            probe_state=probe_state, evidence=evidence,
        )

    beyond_known = bool(family) and tuple(family) > KNOWN_BEHAVIOUR_CEILING
    if beyond_known:
        # Newer than anything audited: the proven primitives are the whole
        # first-class family's core (read, update, traffic, reset) because the route
        # probe answered on this panel; every later refinement stays OFF until it is
        # separately proven. This is what keeps a 3.9/4.x panel from silently
        # receiving 3.8 assumptions.
        evidence['ceiling'] = ('version above the audited ceiling %s: only proven '
                               'primitives' % (KNOWN_BEHAVIOUR_CEILING,))
        return PanelClientCapabilities(
            client_api_family=CLIENT_API_FIRST_CLASS,
            client_get=True, client_update=True, client_traffic=True,
            client_reset_traffic=True, bulk_adjust=True,
            bulk_enable=False, node_pending_response=False,
            scoped_tokens=False, limit_hwid=False,
            version=version_display, version_family=family,
            profile=getattr(profile, 'name', None),
            probe_state=probe_state, evidence=evidence,
        )

    # bulkEnable/nodePending are only claimed when the version (or, in future, a
    # per-route probe) proves them. An unknown or future version therefore gets the
    # primitive that has existed since v3.1 - the full client update - and never a
    # guessed bulkEnable.
    node_pending = _family_at_least(version, NODE_PENDING_FAMILY,
                                    min_patch=NODE_PENDING_MIN_PATCH)
    bulk_enable = _family_at_least(version, BULK_ENABLE_FAMILY)
    evidence['bulk_enable'] = ('version >= 3.5' if bulk_enable else 'not proven')
    evidence['node_pending'] = ('version >= 3.3.1' if node_pending else 'not proven')
    evidence['client_traffic'] = 'first-class family (>= 3.1)'
    return PanelClientCapabilities(
        client_api_family=CLIENT_API_FIRST_CLASS,
        client_get=True, client_update=True, client_traffic=True,
        client_reset_traffic=True, bulk_adjust=True,
        bulk_enable=bulk_enable,
        node_pending_response=node_pending,
        scoped_tokens=bool(getattr(profile, 'scoped_tokens', False))
        or _family_at_least(version, SCOPED_TOKENS_FAMILY),
        limit_hwid=bool(getattr(profile, 'client_limit_hwid', False)),
        version=version_display, version_family=family,
        profile=getattr(profile, 'name', None),
        probe_state=probe_state, evidence=evidence,
    )


def blocked_capabilities(*, probe_state: str, reason: str, version=None,
                         profile=None) -> PanelClientCapabilities:
    """The fail-closed record: no first-class claim, no legacy claim either.

    Both families are marked unusable so a caller cannot accidentally fall back to
    the legacy write. The reason travels in ``evidence`` for the operator.
    """
    return PanelClientCapabilities(
        client_api_family=CLIENT_API_LEGACY,
        legacy_inbound_update=False,
        legacy_inbound_reset_traffic=False,
        version=getattr(version, 'display', None) if version else None,
        version_family=getattr(version, 'family', None) if version else None,
        profile=getattr(profile, 'name', None),
        probe_state=probe_state,
        evidence={'blocked_reason': reason, 'route_probe': probe_state},
    )


def capabilities_for(server, session_obj=None, *, force: bool = False):
    """Resolve the capabilities for one server, fail-closed on an unproven probe.

    Returns ``(capabilities, reason)`` where ``reason`` is a short diagnostic when
    the result is blocked, else None. Never raises: a panel that cannot be
    classified is a BLOCKED strategy, not an exception in a request path.
    """
    from panel.adapters import xui            # deferred: peer layer, avoids a cycle
    from panel.services import xui_compat

    compat = xui_compat.cached_compatibility(getattr(server, 'id', None))
    profile = getattr(compat, 'profile', None) if compat is not None else None
    version = None
    if compat is not None:
        version = xui_compat.normalize_version(getattr(compat, 'detected_version', None))
        if not version.is_parsed:
            # A panel that reported no version still yields a usable profile; the
            # version hint is simply absent, so capabilities come from the probe.
            version = None

    probe_state = PROBE_UNKNOWN
    route_proven = None
    if session_obj is not None:
        try:
            probe_state = xui.probe_v3_client_api(server, session_obj, force=force)
        except Exception as exc:                       # pragma: no cover - defensive
            probe_state = PROBE_TRANSPORT_ERROR
            route_proven = None
            return blocked_capabilities(
                probe_state=probe_state, reason='capability probe raised: %s' % exc,
                version=version, profile=profile), 'capability probe failed'
        if probe_state == PROBE_SUPPORTED:
            route_proven = True
        elif probe_state == PROBE_ROUTE_MISSING:
            # A 404 is NOT proof of a legacy panel: upstream answers 404 both for an
            # absent route and for an aborted authentication (v2.8.11/v3.0 answer 404
            # on a failed credential, and every version answers 404 to a bare
            # unauthenticated request). Require POSITIVE evidence that the legacy
            # client family exists before a mutation is allowed to use it - the probe
            # is a read (POST /inbounds/onlines) that only the legacy family answers.
            legacy_state = None
            try:
                legacy_state = xui.probe_legacy_inbound_api(server, session_obj,
                                                            force=force)
            except Exception as exc:                       # pragma: no cover - defensive
                legacy_state = PROBE_TRANSPORT_ERROR
            if legacy_state == PROBE_SUPPORTED:
                route_proven = False
            else:
                reason = {
                    PROBE_ROUTE_MISSING: (
                        'neither the first-class client API nor the legacy inbound '
                        'client API answered: this panel build is not classifiable'),
                    PROBE_AUTH_INVALID: (
                        'the panel rejected EVE\'s credential while confirming the '
                        'legacy client API (404 on /clients/get is what a failed auth '
                        'looks like on older builds)'),
                    PROBE_SCOPE_INSUFFICIENT: (
                        'the API token scope is insufficient to confirm either client API'),
                    PROBE_TRANSPORT_ERROR: (
                        'the panel could not be reached to confirm the legacy client API'),
                    PROBE_INVALID_RESPONSE: (
                        'the legacy client probe answered something that is not a client '
                        'API response'),
                }.get(legacy_state, 'the client API family could not be proven')
                return blocked_capabilities(
                    probe_state=probe_state, reason=reason,
                    version=version, profile=profile), reason
        else:
            # AUTH_INVALID / SCOPE_INSUFFICIENT / TRANSPORT_ERROR / INVALID_RESPONSE:
            # ask the cache whether this panel was ever proven. A previously proven
            # fact stays valid (we do not downgrade it), but a first-time failure is
            # NOT evidence of legacy.
            cached_known = False
            try:
                cached = xui.XUI_CAPABILITY_CACHE.get(int(getattr(server, 'id')))
                cached_known = bool(cached) and float(cached.get('expiry') or 0) > time.time()
                if cached_known:
                    route_proven = bool(cached.get('v3_clients'))
            except (TypeError, ValueError, AttributeError):
                cached_known = False
            if not cached_known:
                reason = {
                    PROBE_AUTH_INVALID: 'the panel rejected EVE\'s credential (401)',
                    PROBE_SCOPE_INSUFFICIENT: 'the API token scope is insufficient (403)',
                    PROBE_TRANSPORT_ERROR: 'the panel could not be reached to prove its API',
                    PROBE_INVALID_RESPONSE: 'the panel answered something that is not a client API response',
                }.get(probe_state, 'the panel API could not be classified')
                return blocked_capabilities(
                    probe_state=probe_state, reason=reason,
                    version=version, profile=profile), reason

    caps = capabilities_from_version(version, profile=profile,
                                     client_route_proven=route_proven,
                                     probe_state=probe_state)
    return caps, None


def select_renew_strategy(caps: PanelClientCapabilities) -> RenewStrategy:
    """The one mapping from capabilities to a renewal procedure.

    A version may inform strategy selection only through the capabilities it
    produced; this function never re-reads a version itself.
    """
    if not caps.legacy_inbound_update and not caps.first_class:
        return RenewStrategy.BLOCKED
    if not caps.first_class:
        return RenewStrategy.LEGACY_INBOUND
    if caps.client_api_family != CLIENT_API_FIRST_CLASS:
        return RenewStrategy.BLOCKED
    family = caps.version_family
    if family is None:
        # No parsed version: only the proven primitives may be used, which for the
        # first-class family means the full client update (available since v3.1).
        return RenewStrategy.UNKNOWN_FUTURE
    # Exact family matching, deliberately: a >= comparison would let 3.9 inherit
    # 3.8's audited behaviour, which is the guess this enum exists to refuse.
    if tuple(family) > KNOWN_BEHAVIOUR_CEILING:
        return RenewStrategy.UNKNOWN_FUTURE
    if tuple(family) >= (3, 8):
        return RenewStrategy.V3_CURRENT
    if tuple(family) >= (3, 7):
        return RenewStrategy.V3_SCOPED
    if tuple(family) >= (3, 5):
        return RenewStrategy.V3_BULK_ENABLE
    if tuple(family) >= (3, 3):
        return RenewStrategy.V3_NODE_PENDING
    if tuple(family) >= (3, 1):
        return RenewStrategy.V3_EARLY
    # A first-class route on a pre-3.1 major is a contradiction; trust the route
    # (it was proven) but use nothing newer than the earliest strategy.
    return RenewStrategy.V3_EARLY


#: Field names a renewal intent may rely on. Used by the bulkAdjust planner so an
#: unmodelled field can never be silently ignored by a delta-based endpoint.
BULK_ADJUST_PROVABLE_FIELDS = (
    'add_days', 'add_bytes', 'carry_over', 'reset_traffic', 'unlimited_volume',
    'unlimited_expiry', 'start_after_first_use', 'gift_bytes', 'fractional_days',
)


def can_use_native_bulk_adjust(intent: dict, observed_state: dict,
                               capabilities: PanelClientCapabilities):
    """May this renewal be expressed as a native bulkAdjust? Returns (ok, reason).

    bulkAdjust is delta-based and has upstream semantics that are NOT EVE's for
    several intents: it cannot express an exact cap replacement, it treats
    unlimited differently, and its automatic re-enable deliberately skips clients
    that were disabled manually. Using it as a universal renewal would therefore
    change EVE behaviour silently.

    The gate is deliberately conservative: every intent shape whose equivalence has
    not been proven is refused, and the caller falls back to the exact full-client
    update (which is a replacement and can express all of them).
    """
    if not capabilities.bulk_adjust:
        return False, 'panel does not expose bulkAdjust'
    intent = dict(intent or {})
    observed = dict(observed_state or {})
    # Exact cap replacement / exact expiry setting cannot be expressed as a delta.
    for field_name in ('set_total_bytes', 'set_expiry_ms', 'exact_total_bytes',
                       'exact_expiry_ms'):
        if intent.get(field_name) is not None:
            return False, 'exact replacement (%s) is not a delta operation' % field_name
    if intent.get('carry_over'):
        return False, 'carry-over semantics differ from bulkAdjust'
    if intent.get('unlimited_volume') or intent.get('unlimited_expiry'):
        return False, 'unlimited handling differs from bulkAdjust'
    if intent.get('start_after_first_use'):
        return False, 'negative/start-after-first-use expiry is not a bulkAdjust delta'
    if intent.get('reset_traffic'):
        return False, 'traffic reset is a separate primitive with its own idempotency'
    if intent.get('gift_bytes'):
        return False, 'gift volume has its own accounting and must stay an explicit update'
    if intent.get('fractional_days'):
        return False, 'fractional custom renewal values are not a whole-day delta'
    if not any(intent.get(name) for name in ('add_days', 'add_bytes')):
        return False, 'nothing to add'
    # The panel's own auto-re-enable only revives clients it disabled for
    # depletion. A manually disabled client must keep requiring an explicit
    # activation step, so bulkAdjust may not be used to "also fix" activation.
    if observed.get('manually_disabled'):
        return False, 'client was disabled manually; bulkAdjust will not re-enable it'
    if observed.get('depleted'):
        return False, 'client is still depleted; activation must follow, not be assumed'
    return True, 'delta renewal with no carry-over, exact cap, gift or reset'
