"""The GMweb transport-health probe's pure parts (one definition, many callers).

Both the settings page (`GET /api/sms/transport-health`) and the doctor snapshot
need the same verdict about GMweb's transport-health contract. The vocabulary,
the status mapping and the field projection live here, once, so two callers
cannot name the same failure differently - and so the field list cannot drift
from the shared contract the way a hand-written list did.

No HTTP and no Flask here on purpose: this module is pure so it can be unit
tested without a network or an app context. Read-only and PII-free by
construction - the diagnostic strings never carry the API key, a phone number,
a recipient or message content.
"""
from panel.services import gmweb_contract

# Probe verdicts. The vocabulary is declared in shared/eve-gmweb-contract-v1.json
# (transportHealthResponse.probeStates) so provider and consumer agree.
PROBE_CONNECTED = 'connected'
PROBE_NOT_CONFIGURED = 'gmweb_not_configured'
PROBE_UNREACHABLE = 'gmweb_unreachable'
PROBE_CONTRACT_MISSING = 'contract_missing'
PROBE_VERSION_MISMATCH = 'contract_version_mismatch'
PROBE_AUTH_FAILED = 'auth_failed'
PROBE_SCOPE_DENIED = 'scope_denied'
PROBE_INVALID = 'invalid_response'

# What an operator should DO about each verdict. A generic "unknown" told them
# nothing: it hid a missing route, a rejected key and a dead host behind one dash.
PROBE_DIAGNOSTICS = {
    PROBE_NOT_CONFIGURED: 'The SMS gateway Base URL and API key are not set.',
    PROBE_UNREACHABLE: 'GMweb did not answer; check the host, the port and the network path.',
    PROBE_CONTRACT_MISSING: ('GMweb is reachable but does not serve the transport-health '
                             'contract; upgrade GMweb to a version supporting contract v1.'),
    PROBE_VERSION_MISMATCH: 'GMweb answers a different transport-health contract version.',
    PROBE_AUTH_FAILED: 'GMweb rejected the configured API key.',
    PROBE_SCOPE_DENIED: 'The configured project key is missing the transport:read scope.',
    PROBE_INVALID: 'GMweb answered, but not with a usable transport-health contract.',
}

PROBE_TIMEOUT_SECONDS = 5


def _count(value):
    try:
        return max(0, min(1_000_000_000, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _text(value, maximum=64):
    return str(value)[:maximum] if value is not None else None


def _project_optional_diagnostic(name, value):
    if not isinstance(value, dict):
        return None
    if name == 'androidActivity':
        return {
            'authoritative': bool(value.get('authoritative')),
            'lastSuccessfulPullAt': _text(value.get('lastSuccessfulPullAt')),
            'lastTaskPulledAt': _text(value.get('lastTaskPulledAt')),
            'lastValidateAt': _text(value.get('lastValidateAt')),
            'lastValidateResult': _text(value.get('lastValidateResult')),
        }
    if name == 'carrierReports':
        return {
            'total': _count(value.get('total')),
            'delivered': _count(value.get('delivered')),
            'failed': _count(value.get('failed')),
            'duplicates': _count(value.get('duplicates')),
            'conflicts': _count(value.get('conflicts')),
            'unknownRequests': _count(value.get('unknownRequests')),
            'lastReportAt': _text(value.get('lastReportAt')),
        }
    if name == 'callbackOutbox':
        return {
            'pending': _count(value.get('pending')),
            'retry_wait': _count(value.get('retry_wait')),
            'delivering': _count(value.get('delivering')),
            'delivered': _count(value.get('delivered')),
            'dead_letter': _count(value.get('dead_letter')),
            'oldest_pending_age_ms': (
                _count(value.get('oldest_pending_age_ms'))
                if value.get('oldest_pending_age_ms') is not None else None),
            'last_success_at': _text(value.get('last_success_at')),
            'last_failure_at': _text(value.get('last_failure_at')),
        }
    return None


def probe_states():
    """The declared vocabulary, or our own constants when the file is unreadable."""
    declared = gmweb_contract.transport_health_probe_states()
    return declared or sorted({
        PROBE_CONNECTED, PROBE_NOT_CONFIGURED, PROBE_UNREACHABLE, PROBE_CONTRACT_MISSING,
        PROBE_VERSION_MISMATCH, PROBE_AUTH_FAILED, PROBE_SCOPE_DENIED, PROBE_INVALID,
    })


def expected_contract_version():
    """The response contract version GMweb must echo."""
    return gmweb_contract.transport_health_contract_version()


def probe_state_for_status(status_code):
    """Map a GMweb HTTP status onto the shared probe vocabulary."""
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return PROBE_INVALID
    if code == 401:
        return PROBE_AUTH_FAILED
    if code == 403:
        return PROBE_SCOPE_DENIED
    if code in (404, 405, 501):
        return PROBE_CONTRACT_MISSING
    if code >= 500:
        return PROBE_UNREACHABLE
    return PROBE_INVALID


def project_sections(payload):
    """Project only the fields the shared contract declares.

    The field list comes FROM the contract file rather than being restated by the
    caller: an earlier hand-written list asked for `device.age_ms` and would have
    silently dropped `device.last_seen_age_ms`, because this projection only
    copies keys it names.
    """
    declared = gmweb_contract.transport_health_sections()
    safe = {}
    if not isinstance(payload, dict):
        return safe
    for section, keys in declared.items():
        value = payload.get(section)
        if isinstance(value, dict):
            safe[section] = {key: value.get(key) for key in keys if key in value}
    diagnostics = payload.get('diagnostics')
    if isinstance(diagnostics, dict):
        target = safe.setdefault('diagnostics', {})
        for name in gmweb_contract.transport_health_optional_diagnostics():
            projected = _project_optional_diagnostic(name, diagnostics.get(name))
            if projected is not None:
                target[name] = projected
    return safe


def verdict(state, *, status=None, contract=None, health=None, error=None):
    """A machine-readable probe result. Never contains credentials or content."""
    payload = {
        'probe_state': state,
        'diagnostic': error or PROBE_DIAGNOSTICS.get(state),
        'http_status': status,
        'contract_version': contract,
        'contract_supported': contract is not None and contract == expected_contract_version(),
        'health': health,
    }
    if state == PROBE_CONNECTED:
        payload.pop('diagnostic')
    return payload


def summarize(payload, *, state, status=None, contract=None):
    """Flatten a probe result into the scalar fields a doctor snapshot reports.

    Deliberately PII-free: state names, counters, ages and the contract version
    only - never a key, a recipient or message content.
    """
    sections = project_sections(payload) if isinstance(payload, dict) else {}
    transport = sections.get('transport') or {}
    device = sections.get('device') or {}
    queue = sections.get('queue') or {}
    last_ack = sections.get('last_ack') or {}
    return {
        'configured': state != PROBE_NOT_CONFIGURED,
        'reachable': state not in (PROBE_UNREACHABLE, PROBE_NOT_CONFIGURED),
        'probe_state': state,
        'http_status': status,
        'contract_version': contract,
        'contract_supported': (contract is not None
                               and contract == expected_contract_version()),
        'active_transport': transport.get('active'),
        'device_state': device.get('state'),
        'device_last_seen_age_ms': device.get('last_seen_age_ms', device.get('age_ms')),
        'queue_pending': queue.get('pending'),
        'queue_inflight': queue.get('inflight'),
        'last_ack_outcome': last_ack.get('outcome'),
        'last_ack_at': last_ack.get('at'),
    }
