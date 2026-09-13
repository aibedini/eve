"""Usage Intelligence: deterministic analytics over telemetry + business events.

The RFP's architectural rule, enforced by this package's layout:

* **telemetry** - ``UsageCounterState`` / ``UsageHourly`` / ``UsageDaily`` say how much
  traffic was recorded (owned by ``panel/jobs/schedulers.py``);
* **business events** - ``RenewalEvent`` says when a customer's commercial cycle
  changed and on what terms (written by ``events.py`` from an explicit, verified
  mutation, never inferred from a counter movement);
* **analytics** - cycles, metrics, trend, forecast, confidence and package selection
  combine the two and may never impersonate either.

The analytics modules are pure functions over dataclasses so they can be unit tested
without Flask, the database or a panel (RFP sections 63-65).
"""
from panel.services.usage_intelligence.events import (  # noqa: F401
    has_recent_cycle_boundary,
    latest_cycle_boundary,
    record_inferred_reset,
    record_renewal_event,
    record_verified_renewal,
)

__all__ = [
    'has_recent_cycle_boundary',
    'latest_cycle_boundary',
    'record_inferred_reset',
    'record_renewal_event',
    'record_verified_renewal',
]
