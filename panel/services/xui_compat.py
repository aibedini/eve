"""Version-gated 3x-ui panel compatibility.

One authority for "what does this panel version mean for us". Splitting it out of
the adapter is deliberate: version comparisons scattered across route and job
modules are how a rule like "3.7 and newer sends the new shape" silently becomes
"every version we have never tested sends the new shape".

Design rules (see specs/001-3xui-37-38-compat):

* A version is normalised to numbers and reduced to a family (major, minor).
  Versions are never compared as strings.
* Only families on an explicit whitelist select a non-baseline profile. 3.9.x,
  4.x and anything newer fall back to the baseline profile and are flagged as
  uncertified - they must not inherit 3.8 semantics that were never verified.
* A version we cannot parse is unknown, not a guess. Unknown keeps today’s
  behaviour and says so.
* Every capability flag must be traceable to the exact upstream tag it was read
  from. Keep the evidence next to the flag.

Upstream evidence used for this table (cloned tags, not release notes):

  v3.7.0 = f727d04f6522bb94a8fb52e8352fdcafb51c11e1
  v3.8.0 = 837addf66e945a80080273b5d2a315dea765d748
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Optional

# --------------------------------------------------------------------------- #
# Version parsing
# --------------------------------------------------------------------------- #

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass(frozen=True)
class PanelVersion:
    """A normalised, comparable panel version.

    raw keeps exactly what the panel reported, for diagnostics. family is
    (major, minor) and is what profile selection keys on: patch differences
    within a family never change behaviour.
    """

    raw: Optional[str] = None
    major: Optional[int] = None
    minor: Optional[int] = None
    patch: Optional[int] = None
    family: Optional[tuple] = None
    is_parsed: bool = False

    @property
    def display(self) -> str:
        if not self.is_parsed:
            return str(self.raw or "").strip() or "unknown"
        return ".".join([str(self.major), str(self.minor), str(self.patch or 0)])


def normalize_version(raw) -> PanelVersion:
    """Parse a panel version into PanelVersion.

    Accepts an optional leading v/V, a 2- or 3-component version, and a trailing
    build suffix (3.8.0+build.1). Anything else - dev+, a commit hash, an empty
    string, the literal unknown - comes back unparsed. Callers must treat that as
    "unknown", never as a default family.
    """
    if raw is None:
        return PanelVersion(raw=None)
    text = str(raw).strip()
    if not text:
        return PanelVersion(raw=raw)
    # Strip a build/metadata suffix first; "dev+" has no leading digits and so
    # still fails the match below.
    candidate = text.split("+", 1)[0].strip()
    match = _VERSION_RE.match(candidate)
    if not match:
        return PanelVersion(raw=raw)
    try:
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) is not None else 0
        patch = int(match.group(3)) if match.group(3) is not None else 0
    except (TypeError, ValueError):
        return PanelVersion(raw=raw)
    return PanelVersion(
        raw=raw, major=major, minor=minor, patch=patch,
        family=(major, minor), is_parsed=True,
    )


# --------------------------------------------------------------------------- #
# Compatibility profiles
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PanelCompatibilityProfile:
    """The capability set that governs behaviour for one version family.

    Each flag is a claim about the upstream contract. Only set a flag when the
    exact tagged source or its generated OpenAPI proves it - see the evidence
    comments attached to each field in PROFILES below.
    """

    name: str
    certified: bool = False
    # 3.7.0 added scoped + optionally expiring API tokens (ApiToken.Scope and
    # ApiToken.ExpiresAt; enforceTokenScope in internal/web/controller/api.go).
    scoped_tokens: bool = False
    expiring_tokens: bool = False
    # 3.8.0 answers a rejected Bearer with 401. 3.7.0 answers 404 unless the
    # request carries X-Requested-With: XMLHttpRequest (checkAPIAuth differs).
    bearer_rejection_is_401: bool = False
    bearer_hint_header_required: bool = False
    # ClientRecord carries limitHwid in both tags generated OpenAPI.
    client_limit_hwid: bool = False
    # model.Client carries ResetDay/ResetMax/TrafficReset/TrafficResetDay in both.
    panel_lifecycle_automation: bool = False
    # 3.8.0 seeds fresh installs with a randomised sub path
    # (internal/web/service/setting.go:356); 3.7.0 has a fixed /sub/.
    random_subscription_paths: bool = False
    # Protocol constants in internal/database/model/model.go.
    amneziawg: bool = False
    tuic: bool = False


#: Profile names the doctor surface advertises as certified.
CERTIFIED_FAMILIES_PROFILE_NAMES = ("baseline_v3", "xui_3_7", "xui_3_8")

PROFILE_BASELINE_V3 = PanelCompatibilityProfile(name="baseline_v3", certified=True)
PROFILE_XUI_3_7 = PanelCompatibilityProfile(
    name="xui_3_7",
    certified=True,
    scoped_tokens=True,
    expiring_tokens=True,
    bearer_rejection_is_401=False,
    bearer_hint_header_required=True,
    client_limit_hwid=True,
    panel_lifecycle_automation=True,
    random_subscription_paths=False,
    amneziawg=True,
    tuic=False,
)
PROFILE_XUI_3_8 = PanelCompatibilityProfile(
    name="xui_3_8",
    certified=True,
    scoped_tokens=True,
    expiring_tokens=True,
    bearer_rejection_is_401=True,
    bearer_hint_header_required=False,
    client_limit_hwid=True,
    panel_lifecycle_automation=True,
    random_subscription_paths=True,
    amneziawg=True,
    tuic=True,
)

#: Whitelist of certified families. Anything absent resolves to the baseline.
CERTIFIED_FAMILIES = {
    (3, 7): PROFILE_XUI_3_7,
    (3, 8): PROFILE_XUI_3_8,
}

#: The oldest family whose behaviour this feature changed. Families below it are
#: already-supported pre-existing behaviour and must stay exactly as they were.
FIRST_CERTIFIED_FAMILY = (3, 7)

CERT_SUPPORTED = "supported"
CERT_REQUIRED = "certification_required"
CERT_UNVERIFIED = "unverified"

WARN_VERSION_UNKNOWN = "panel_version_unknown"
WARN_FUTURE_UNCERTIFIED = "future_version_uncertified"
WARN_AUTH_INVALID = "api_auth_invalid"
WARN_AUTH_EXPIRED = "api_token_expired_or_rotated"
WARN_SCOPE_INSUFFICIENT = "api_token_scope_insufficient"
WARN_LIFECYCLE_AUTOMATION = "panel_lifecycle_automation_detected"
WARN_SUBSCRIPTION_FALLBACK = "subscription_path_fallback"


def select_profile(version: PanelVersion):
    """Total function: version -> (profile, certification, warnings).

    Never raises and never returns None. A version that cannot be parsed, or one
    outside the certified whitelist, gets the baseline profile - the same
    requests EVE issued before this feature existed.
    """
    if not isinstance(version, PanelVersion) or not version.is_parsed:
        return PROFILE_BASELINE_V3, CERT_UNVERIFIED, [WARN_VERSION_UNKNOWN]
    certified = CERTIFIED_FAMILIES.get(version.family)
    if certified is not None:
        return certified, CERT_SUPPORTED, []
    # Families older than the first certified change keep the behaviour they had
    # before this feature. They are already-supported, not suspect.
    if version.family < FIRST_CERTIFIED_FAMILY:
        return PROFILE_BASELINE_V3, CERT_SUPPORTED, []
    # Parsed, but newer than anything certified. Explicitly NOT the newest known
    # profile: a 3.9 or 4.x panel must never inherit 3.8 behaviour.
    return PROFILE_BASELINE_V3, CERT_REQUIRED, [WARN_FUTURE_UNCERTIFIED]


# --------------------------------------------------------------------------- #
# Per-server resolution result + cache
# --------------------------------------------------------------------------- #

SOURCE_SERVER_STATUS = "server_status"
SOURCE_PANEL_UPDATE_INFO = "panel_update_info"
SOURCE_NONE = "none"

CONF_AUTHORITATIVE = "authoritative"
CONF_CORROBORATED = "corroborated"
CONF_UNKNOWN = "unknown"


@dataclass(frozen=True)
class PanelCompatibility:
    """Everything the rest of EVE needs to know about one panel version."""

    server_id: Optional[int] = None
    detected_version: Optional[str] = None
    version: PanelVersion = field(default_factory=PanelVersion)
    profile: PanelCompatibilityProfile = PROFILE_BASELINE_V3
    detection_source: str = SOURCE_NONE
    confidence: str = CONF_UNKNOWN
    certification: str = CERT_UNVERIFIED
    warnings: tuple = ()
    resolved_at: float = 0.0

    @property
    def is_certified_family(self) -> bool:
        return (self.certification == CERT_SUPPORTED
                and self.profile.name != PROFILE_BASELINE_V3.name)

    def as_public_dict(self) -> dict:
        """Credential-free view for the doctor surface."""
        return {
            "detected_version": self.detected_version,
            "normalized_version": self.version.display,
            "family": list(self.version.family) if self.version.family else None,
            "profile": self.profile.name,
            "detection_source": self.detection_source,
            "confidence": self.confidence,
            "certification": self.certification,
            "warnings": list(self.warnings),
        }


#: server_id -> {"value": PanelCompatibility, "expiry": float}
COMPAT_CACHE: dict = {}
COMPAT_TTL = 600  # seconds; same order as the existing capability cache


def invalidate_compatibility(server_id=None) -> None:
    """Drop cached compatibility. None clears everything."""
    if server_id is None:
        COMPAT_CACHE.clear()
        return
    try:
        COMPAT_CACHE.pop(int(server_id), None)
    except (TypeError, ValueError):
        pass


def cached_compatibility(server_id):
    """Return a live cache entry, or None. Never raises."""
    try:
        entry = COMPAT_CACHE.get(int(server_id))
    except (TypeError, ValueError):
        return None
    if not entry:
        return None
    if time.time() >= float(entry.get("expiry") or 0):
        return None
    return entry.get("value")


def remember_compatibility(compat: PanelCompatibility, ttl: int = None) -> PanelCompatibility:
    """Cache a resolution result. A cache is a hint, never a durable fact."""
    if compat is None or compat.server_id is None:
        return compat
    try:
        COMPAT_CACHE[int(compat.server_id)] = {
            "value": compat,
            "expiry": time.time() + float(ttl if ttl is not None else COMPAT_TTL),
        }
    except (TypeError, ValueError):
        pass
    return compat


def resolve_compatibility(server_id, version_raw, *, source=SOURCE_NONE,
                          confidence=CONF_UNKNOWN) -> PanelCompatibility:
    """Build and cache a compatibility result from a raw version string.

    Callers pass what the panel actually reported; this decides what it means -
    including refusing to guess when the value is unusable.
    """
    version = normalize_version(version_raw)
    profile, certification, warnings = select_profile(version)
    if not version.is_parsed:
        confidence = CONF_UNKNOWN
    compat = PanelCompatibility(
        server_id=server_id,
        detected_version=(str(version_raw).strip() if version_raw not in (None, "") else None),
        version=version,
        profile=profile,
        detection_source=source,
        confidence=confidence,
        certification=certification,
        warnings=tuple(warnings),
        resolved_at=time.time(),
    )
    return remember_compatibility(compat)


def compat_with_warning(compat: PanelCompatibility, warning: str) -> PanelCompatibility:
    """Return a copy carrying one extra named degraded condition."""
    if compat is None or warning in compat.warnings:
        return compat
    return replace(compat, warnings=tuple(compat.warnings) + (warning,))


# --------------------------------------------------------------------------- #
# Client-field preservation
# --------------------------------------------------------------------------- #

#: The ONLY client-record fields the preservation read may consult and the
#: mutation payload may echo. Everything else is either outside this contract or
#: is write-only / secret-bearing upstream and must never be read back and
#: resubmitted (FR-023).
PRESERVED_CLIENT_FIELDS = (
    "limitHwid",
)


def preserved_limit_hwid(client_snapshot) -> Optional[int]:
    """Extract the device limit from an authoritative client read.

    Returns None when the panel does not expose the field, which means the
    caller must OMIT it - never default it to zero. A stored 0 is a real
    operator choice (no device limit) and must round-trip as 0.
    """
    if not isinstance(client_snapshot, dict):
        return None
    if "limitHwid" not in client_snapshot:
        return None
    value = client_snapshot.get("limitHwid")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Panel-side lifecycle automation
# --------------------------------------------------------------------------- #

LIFECYCLE_AUTOMATION_FIELDS = ("resetDay", "resetMax", "trafficReset", "trafficResetDay")

#: trafficReset is always present upstream and defaults to never; only a value
#: that actually schedules work counts as automation.
_TRAFFIC_RESET_INACTIVE = ("", "never", "none")


def detect_lifecycle_automation(client) -> Optional[dict]:
    """Report panel-side automatic lifecycle settings on one client.

    EVE owns renewal, generation and notification supersession. A panel that
    renews or resets on its own can therefore act outside EVE journal. This
    detects that condition so it can be surfaced - it never writes, zeroes or
    adopts the fields.
    """
    if not isinstance(client, dict):
        return None
    observed = {}
    for key in LIFECYCLE_AUTOMATION_FIELDS:
        if key in client:
            observed[key] = client.get(key)
    try:
        reset_day = int(client.get("resetDay") or 0)
    except (TypeError, ValueError):
        reset_day = 0
    try:
        reset_max = int(client.get("resetMax") or 0)
    except (TypeError, ValueError):
        reset_max = 0
    traffic_reset = str(client.get("trafficReset") or "").strip().lower()
    has_cycle = traffic_reset not in _TRAFFIC_RESET_INACTIVE
    if reset_day <= 0 and reset_max <= 0 and not has_cycle:
        return None
    return {
        "fields": observed,
        "severity": "warning",
        "managed_state": "partially_managed",
        "operator_action": (
            "This client can be renewed or reset by the panel itself, outside "
            "EVE lifecycle control. Disable the panel-side automatic renewal / "
            "traffic reset for this client, or accept that EVE is not the only "
            "lifecycle authority for it."
        ),
        "warning": WARN_LIFECYCLE_AUTOMATION,
    }
