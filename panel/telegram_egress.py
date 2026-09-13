"""Explicit Telegram egress policy: which routes a bot may ever use.

Why this module exists
----------------------
`TelegramBotApi` already fails over between routes, and that failover used to be
the *only* thing deciding whether a message could leave the host directly. A
proxy that was cooling down simply dropped out of the ordering and the direct
route — if it was in the list — was tried next. Nothing recorded that decision
and nothing could forbid it, so `proxy_only` was one list-construction mistake
away from a silent direct connection, and `proxy_first` could not distinguish
"fallback is fine" from "fallback would be a policy breach".

This module splits the two concerns the directive calls out:

    EgressPolicy -> allowed route set -> route health/cooldown -> transport

* the policy decides the **maximum** route set, once, before any I/O;
* the transport may only *order* what the policy allowed, never widen it;
* a cooldown is an availability fact, not permission, so an exhausted allowed
  set stays within policy and reports the dependency as unavailable.

Vocabulary (the admin setting historically lived in the `connection_mode`
column, so the new names are accepted by the same field and the old ones keep
working):

    NEVER_DIRECT            zero direct attempts under every failure mode
    DIRECT_ONLY             only the direct route (legacy `direct_only`)
    PROXY_REQUIRED          managed routes only; a failure never falls back
    PANEL_ACCOUNT_REQUIRED  same, for a required panel account route
    PROXY_PREFERRED         managed first, direct is explicitly permitted after
    DIRECT_PREFERRED        direct first, managed still permitted

Failure behaviour is chosen by the caller from `decide().reason`: an empty
allowed set means the delivery is RETRY_WAIT / FAILED_DEPENDENCY, never a
quiet downgrade.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- policy vocabulary -----------------------------------------------------
NEVER_DIRECT = "NEVER_DIRECT"
DIRECT_ONLY = "DIRECT_ONLY"
PROXY_REQUIRED = "PROXY_REQUIRED"
PANEL_ACCOUNT_REQUIRED = "PANEL_ACCOUNT_REQUIRED"
PROXY_PREFERRED = "PROXY_PREFERRED"
DIRECT_PREFERRED = "DIRECT_PREFERRED"

EGRESS_POLICIES = (
    NEVER_DIRECT,
    DIRECT_ONLY,
    PROXY_REQUIRED,
    PANEL_ACCOUNT_REQUIRED,
    PROXY_PREFERRED,
    DIRECT_PREFERRED,
)

DIRECT_ROUTE = "direct"

# Policies that forbid the direct route outright.
_NEVER_DIRECT_POLICIES = frozenset({NEVER_DIRECT, PROXY_REQUIRED, PANEL_ACCOUNT_REQUIRED})
# Policies that forbid the managed routes outright.
_NO_MANAGED_POLICIES = frozenset({DIRECT_ONLY})

# Legacy values accepted from the database and the admin API. They map onto the
# policy that matches what they ACTUALLY did, so an upgrade cannot tighten a
# live deployment by surprise:
#
#   auto          -> DIRECT_PREFERRED   (direct was inserted first)
#   direct_only   -> DIRECT_ONLY
#   proxy_first   -> PROXY_PREFERRED    (direct was appended as a real fallback)
#   proxy_only    -> PROXY_REQUIRED     (direct was never in the list)
_LEGACY_MAP = {
    "auto": DIRECT_PREFERRED,
    "direct_only": DIRECT_ONLY,
    "proxy_first": PROXY_PREFERRED,
    "proxy_only": PROXY_REQUIRED,
}

# Display order for the admin dropdown (managed-first to legacy-fallback to
# never-direct), which is also the order of increasing egress strictness.
POLICY_LABELS = {
    DIRECT_ONLY: "Direct only",
    PROXY_PREFERRED: "Proxy preferred (direct fallback allowed)",
    PROXY_REQUIRED: "Proxy required (no direct fallback)",
    PANEL_ACCOUNT_REQUIRED: "Panel account required (no direct fallback)",
    NEVER_DIRECT: "Never direct",
    DIRECT_PREFERRED: "Direct preferred (proxy as backup)",
}


def normalize_policy(value) -> str:
    """Accept a policy name or a legacy connection mode; return a valid policy.

    An unknown value is a configuration error, not a reason to guess: the caller
    validates user input with :func:`is_valid_policy` and the runtime falls back to
    the strictest sane default only for rows that predate this vocabulary.
    """
    text = str(value or "").strip()
    if not text:
        return PROXY_PREFERRED
    upper = text.upper()
    if upper in EGRESS_POLICIES:
        return upper
    return _LEGACY_MAP.get(text.lower(), PROXY_PREFERRED)


def is_valid_policy(value) -> bool:
    """True when the value is one of the new names or a legacy mode."""
    text = str(value or "").strip()
    if not text:
        return False
    return text.upper() in EGRESS_POLICIES or text.lower() in _LEGACY_MAP


def is_strict(policy) -> bool:
    """True when this policy forbids at least one route class outright."""
    return normalize_policy(policy) in _NEVER_DIRECT_POLICIES | _NO_MANAGED_POLICIES


@dataclass(frozen=True)
class EgressDecision:
    """The allowed route set for one call, plus why it came out that way."""

    policy: str
    allowed: tuple
    allow_direct: bool
    direct_only: bool
    reason: str

    @property
    def usable(self) -> bool:
        """False when the policy leaves no route at all (a real dependency outage)."""
        return bool(self.allowed)


def decide(policy, *, has_managed: bool) -> EgressDecision:
    """Decide the allowed route set for one policy and one bot configuration.

    Pure: no database, no network, no clock. `has_managed` is whether the bot has
    at least one enabled proxy/egress endpoint configured.

    The returned `allowed` tuple is a CEILING. It never grows because a route
    failed; that is the whole point of the module.
    """
    name = normalize_policy(policy)
    if name in _NO_MANAGED_POLICIES:
        return EgressDecision(name, (DIRECT_ROUTE,), True, True, "policy_direct_only")
    if not has_managed:
        # No managed route is configured at all. Direct is the only route that
        # exists; a strict policy must fail closed rather than invent one.
        if name in _NEVER_DIRECT_POLICIES:
            return EgressDecision(name, (), False, False, "no_managed_route_configured")
        return EgressDecision(name, (DIRECT_ROUTE,), True, False, "no_managed_route_configured")
    if name in _NEVER_DIRECT_POLICIES:
        return EgressDecision(name, ("managed",), False, False, "policy_forbids_direct")
    if name == DIRECT_PREFERRED:
        return EgressDecision(name, (DIRECT_ROUTE, "managed"), True, False, "policy_prefers_direct")
    # PROXY_PREFERRED: direct is a documented, explicit part of the policy.
    return EgressDecision(name, ("managed", DIRECT_ROUTE), True, False, "policy_allows_direct_fallback")


def describe(policy) -> str:
    """One-line operator-facing description used by the admin UI and audit rows."""
    name = normalize_policy(policy)
    return POLICY_LABELS.get(name, name)
