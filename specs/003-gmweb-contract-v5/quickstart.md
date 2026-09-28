# Quickstart Validation: GMweb Contract v5 Consumer

## Prerequisites

- EVE feature branch `feat/gmweb-contract-v5`
- Local GMweb checkout containing commit `2ba7ec0837b248ddc4e6e8c84ab90f5c3ee5af0c`
- `.venv-test\Scripts\python.exe`

## Contract equality

Compare EVE's `shared/eve-gmweb-contract-v1.json` byte-for-byte with the file at the exact peer commit. Expected: no difference and contract version 5.

## Focused validation

Run contract, callback evidence/order, metadata identity, transport health, read-client, migration, notification-debt, and SMS Operations tests. Expected: all pass with no network or real Redis.

## Full regression

Run the repository's complete pytest suite, release check, documentation index, UI audit, and static/syntax checks. Expected: zero failures; existing documented skips only.

## Rollout compatibility

Mock GMweb v4 returning 404 for delivery-event search while existing send/status/health/callback operations succeed. Expected: only the v5 diagnostic capability is unavailable.

## Non-automated acceptance

Real Android, SIM, production GMweb, carrier receipt, and staging end-to-end remain explicitly unverified until an operator-approved controlled canary is performed. Never send a deliberate test SMS to a real customer.
