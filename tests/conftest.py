"""Shared test hygiene.

Every test module binds the same Flask app (and therefore the same scoped
session and engine) in one pytest process. Several suites bulk-delete rows in
tearDown but leave ORM instances in the session identity map; sqlite then
reuses primary keys (id starts again at 1 on an emptied table), so a later
suite can flush a NEW object over a STALE identity-map entry from an earlier
file ("Identity map already had an identity ... replacing" SAWarning) and read
back the wrong object — order-dependent, flaky cross-file failures.

Resetting the scoped session after every test makes identity-map leakage
impossible regardless of what any individual test file forgets to clean up.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_db_session_between_tests():
    yield
    try:
        from app import db
        db.session.rollback()
    except Exception:
        pass
    try:
        from app import db
        db.session.remove()
    except Exception:
        pass
