# Research: GMweb Contract v5 Consumer

## Exact contract authority

- **Decision**: Use `shared/eve-gmweb-contract-v1.json` from GMweb commit `2ba7ec0837b248ddc4e6e8c84ab90f5c3ee5af0c` and verify byte equality.
- **Rationale**: The cross-repository drift gate compares canonical fixtures; approximation would defeat the gate.
- **Alternatives considered**: Hand-editing EVE's v4 fixture was rejected because it can miss semantics and formatting.

## Notification identity

- **Decision**: Serialize the existing durable EVE notification event ID as `eveNotificationId` whenever it exists and preserve it across retries.
- **Rationale**: It is already the uniqueness/idempotency authority and contains no recipient or message content.
- **Alternatives considered**: A parallel UUID table was rejected as a duplicate identity authority; generating per retry was rejected as non-correlatable.

## Evidence projection

- **Decision**: Keep immutable events and derive separate submission/carrier projections using occurrence order plus evidence strength.
- **Rationale**: Callback arrival order is not evidence order, and carrier receipt is strictly stronger than submission evidence.
- **Alternatives considered**: Last callback wins was rejected because delayed `send.sent` could erase a delivered fact.

## Diagnostic read endpoint

- **Decision**: Add an on-demand bounded read client and comparison route; do not persist remote rows as if they were authenticated callbacks.
- **Rationale**: It supports investigation of missed callbacks without creating a second authority or polling dependency.
- **Alternatives considered**: Continuous polling and automatic remote-to-local journal insertion were rejected.

## Consumer-first compatibility

- **Decision**: Treat 404/405/501 on the v5-only endpoint as capability unavailable and keep v4 send/health/callback paths unchanged.
- **Rationale**: EVE must merge and deploy before GMweb PR #17.
- **Alternatives considered**: Hard-requiring v5 at startup was rejected because it would break the rollout window.

## Carrier failure policy

- **Decision**: Persist and display carrier failure; do not automatically retry it.
- **Rationale**: Existing policy does not authorize a new physical send after a late carrier result.
- **Alternatives considered**: Immediate retry was rejected due to duplicate-delivery risk and undefined business policy.

## Permissions

- **Decision**: Retain the existing SMS Operations permission for this contract adoption and document granular permissions as deferred.
- **Rationale**: Permission refactoring is orthogonal and must not block the minimal safe v5 synchronization.
- **Alternatives considered**: Introducing several new RBAC grants in the same migration was rejected as unnecessary scope expansion.
