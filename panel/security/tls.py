"""TLS policy for outbound HTTP calls.

Certificate verification is never disabled. Deployments that use a private CA
can point a purpose-specific environment variable at a PEM bundle.
"""

import os
from pathlib import Path


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
