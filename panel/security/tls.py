"""TLS policy for outbound HTTP calls.

Certificate verification is never disabled globally and no caller hardcodes the
value: Deployments that use a private CA can point a purpose-specific environment
variable at a PEM bundle, and an X-UI panel that the operator explicitly marked
allow_insecure opts out of validation for its own connections only through
panel_tls_verify().
"""

import os
from pathlib import Path

XUI_PURPOSE_ENV = "EVE_XUI_CA_BUNDLE"


def outbound_tls_verify(*purpose_env_names: str) -> bool | str:
    """Return the CA policy accepted by ``requests``' ``verify`` argument.

    Purpose-specific variables take precedence over ``EVE_OUTBOUND_CA_BUNDLE``.
    The normal platform/Requests trust store is used when no override is set.
    """
    candidates = (*purpose_env_names, 'EVE_OUTBOUND_CA_BUNDLE')
    for env_name in candidates:
        raw = (os.environ.get(env_name) or '').strip()
        if not raw:
            continue
        bundle = Path(raw).expanduser()
        if not bundle.is_file():
            raise RuntimeError(f'{env_name} does not point to a readable CA bundle: {bundle}')
        return str(bundle)
    return True


def panel_tls_verify(server, *purpose_env_names: str) -> bool | str:
    """The ``requests`` ``verify`` value for one X-UI panel.

    A server the operator explicitly marked ``allow_insecure`` opts out of
    certificate validation for its own connections only. Every other server keeps
    full verification (validity, hostname/IP match, trusted or configured CA).
    The flag is read on every call, so flipping it takes effect as soon as the
    cached session is dropped, and no caller hardcodes the value.
    """
    if bool(getattr(server, "allow_insecure", False)):
        return False
    return outbound_tls_verify(*(purpose_env_names or (XUI_PURPOSE_ENV,)))
