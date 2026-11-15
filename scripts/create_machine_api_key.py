"""Issue a read-only machine API key for the EVE Context API v1.

There is deliberately no HTTP endpoint for key creation: an operator runs this
script on the EVE host. The plaintext token is printed exactly once and only its
digest is stored, so a lost token must be reissued.

Examples::

    python scripts/create_machine_api_key.py \
        --name god-ai-phase3 \
        --scope context.customer.read --scope context.service.read \
        --scope context.renewal.read --scope context.payment.read_masked \
        --scope context.support.read --scope context.knowledge.read \
        --created-by operator

    # Optional expiry (UTC ISO-8601)
    python scripts/create_machine_api_key.py --name god-ai --scope context.customer.read \
        --expires-at 2027-01-01T00:00:00Z
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

# A CLI must not start background workers or run the implicit import migration
# before it has chosen its own working directory/env.
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _parse_expiry(value):
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        raise SystemExit('--expires-at must be an ISO-8601 timestamp')
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True, help='operator-facing key name')
    parser.add_argument('--scope', action='append', default=[],
                        help='repeatable scope, e.g. context.service.read')
    parser.add_argument('--expires-at', default=None,
                        help='optional UTC ISO-8601 expiry (e.g. 2027-01-01T00:00:00Z)')
    parser.add_argument('--created-by', default=None, help='who issued the key')
    parser.add_argument('--list', action='store_true', help='list existing keys (no secrets)')
    args = parser.parse_args(argv)

    from app import app, db  # noqa: E402  (env is configured above)
    from panel.migrate import run_migrations  # noqa: E402
    from panel.models import MachineApiKey  # noqa: E402
    from panel.services import machine_api  # noqa: E402

    with app.app_context():
        run_migrations()
        if args.list:
            rows = MachineApiKey.query.order_by(MachineApiKey.id.asc()).all()
            for row in rows:
                state = ('revoked' if row.is_revoked()
                         else 'expired' if row.is_expired() else 'active')
                print('#%s %-24s %-8s scopes=%s' % (
                    row.id, row.name, state, ','.join(sorted(row.scope_set())) or '-'))
            return 0
        try:
            row, token = machine_api.create_machine_api_key(
                args.name, args.scope, expires_at=_parse_expiry(args.expires_at),
                created_by=args.created_by,
            )
        except machine_api.MachineApiError as exc:
            print('error: %s' % exc.message, file=sys.stderr)
            return 2
        print('Created machine API key #%s (%s)' % (row.id, row.name))
        print('Scopes: %s' % ', '.join(sorted(row.scope_set())))
        if row.expires_at:
            print('Expires: %sZ' % row.expires_at.isoformat())
        print('')
        print('Token (shown once, store it now):')
        print(token)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
