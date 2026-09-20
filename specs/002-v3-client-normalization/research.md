# Research: V3 Client Normalization

## Normalize after processing, before retained commit

`process_inbounds()` constructs familiar rows. Normalize each processed v3 block before it
enters `GLOBAL_SERVER_DATA` or Redis. Raw and processed graphs may briefly coexist; the
probe measures that transient. Normalizing raw payloads would entangle panel parsing;
normalizing Redis alone would recreate duplication on hydration.

## Canonical store plus memberships

Each v3 block uses one `clients` map and inbound `client_refs`. Memberships carry only
inbound-specific differences. Legacy blocks stay expanded behind the same access helpers.
Reliable UUID is primary; missing UUID uses normalized email scoped to one server. Distinct
reliable UUIDs never merge by email.

## Shared retained compatibility graph; materialize only at external boundaries

Existing internal consumers still iterate `inbound["clients"]`, but each membership points
to the same canonical entity dict instead of a copied row. This is the compatibility layer:
it preserves the hot read/mutation API while retaining one entity. A membership overlay is
copied only when its fields genuinely differ. HTTP full/delta responses materialize only
requested normalized inbounds (including the membership-specific `inbound_id`). No expanded
fleet is retained or cached.

## Revision propagation

A client-to-membership index makes entity mutation yield affected inbound keys. Existing
bounded delta history records them, so mutation is O(memberships), not O(fleet rows).

## Versioned blocks

Schema v2 contains `schema_version`, `server_id`, `clients`, and `inbounds`. Readers accept
legacy lists and v2 dictionaries. Unknown future versions preserve the last good block.
The existing JSON-compatible gzip codec remains; pickle is prohibited.

## Bounded retention probe

Use a fixed-length Redis ring with a process-local fallback, `/proc` PSS/USS,
caller-supplied counts, and one 30-second settled sample that captures no raw results. The
shared ring lets the Web memory endpoint report Background checkpoints. Static inspection proves Background owns
scheduler/executor state absent from Web, but cannot explain ~500 MiB confidently. No clear
accidental retained reference is established before implementation; the probe gathers evidence.

## Fix telemetry first

`redis_snapshot_bytes()` uses the writer's canonical `_decode_snapshot()` and focused tests
assert exact compressed sizes for multiple blocks.

## Isolated measurement

Compare expanded and normalized retained deep bytes, JSON bytes, compressed bytes,
publication peak, and hydration peak. Keep production estimates separate from measurements;
do not remove `raw_client`, formatted fields, or full Web hydration.
