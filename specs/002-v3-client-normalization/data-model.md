# Data Model: V3 Client Normalization

## NormalizedServerBlock (schema 2)

- `schema_version`: exactly `2`.
- `server_id`: stable server id.
- `clients`: canonical key → ClientEntity.
- `inbounds`: ordered NormalizedInbound list.
- Optional derived client-to-inbound index; never authoritative.

Every reference resolves inside its block. Unknown versions cannot replace last good state.

## ClientEntity

Key `uuid:<normalized UUID>` when reliable; fallback `email:<normalized email>` only without
a reliable UUID and scoped to the server. Stores account-global fields once plus revision.
Distinct reliable UUIDs remain distinct even with the same email.

## NormalizedInbound / Membership

Inbound metadata plus ordered `client_refs` and optional per-client overrides for fields
that truly differ. Membership deletion leaves an entity used elsewhere; account deletion
removes the entity and all memberships.

## Materialized View

Ephemeral merge of one entity and membership for a requested server, inbound, delta, or
operation. Never retained in the process snapshot or Redis.

## BackgroundLifecycleSample

Cycle id, checkpoint, timestamp, optional PSS/USS, and aggregate raw/processed rows,
inflight work, and retained results. Fixed-size and PII-free.

`idle_before_fetch → after_panel_fetch → after_process_inbounds → after_snapshot_commit → after_redis_publish → after_worker_result_release → settled_30s_after_fetch`
