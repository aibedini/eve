# Quickstart: Validate V3 Client Normalization

Use `.venv-test`; do not use production Redis/network or run Tier 3/full suite.

```powershell
.venv-test\Scripts\python.exe scripts/affected_tests.py --tier 1 --changed panel/core/memory_report.py panel/core/memory_probe.py panel/core/snapshot_model.py panel/core/redis_client.py panel/core/snapshot_delta.py panel/jobs/schedulers.py panel/jobs/refresh.py
.venv-test\Scripts\python.exe -m pytest -q tests/test_snapshot_model.py tests/test_memory_probe.py tests/test_memory_attribution.py tests/test_snapshot_redis.py tests/test_snapshot_delta.py
.venv-test\Scripts\python.exe scripts/measure_snapshot_footprint.py --json
```

Before handoff, run Tier 2 once with every changed application path. Expected: one mirrored
v3 account becomes one entity plus memberships; legacy same-email rows stay distinct;
old/v2 blocks round-trip; mutation affects membership keys only; external payloads remain
expanded; telemetry is exact; diagnostics stay bounded and PII-free.
