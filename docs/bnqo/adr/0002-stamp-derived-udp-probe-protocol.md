# ADR-0002: STAMP-derived custom secure UDP probe protocol

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §3 (STAMP core, optional TWAMP interop, RFC 3393 PDV), §4.2 (reflector authentication/anti-amplification), §5 FR-MEASURE-002 (packet fields, per-direction metrics), §13 (measurement record)

## Context

The core bidirectional measurement is a secure UDP probe producing per-direction
loss, burst loss, RTT, one-way delay, jitter/PDV (RFC 3393), reordering, duplication,
corruption, and inter-arrival distribution (RFP §5 FR-MEASURE-002). RFP §3 fixes the
base: "Core UDP engine based on STAMP (RFC 8762, extension of TWAMP RFC 5357);
optional TWAMP interop."

Three options: raw TWAMP, OWAMP, or a STAMP-derived custom protocol. The protocol
must additionally satisfy reflector security invariants that TWAMP's threat model
does not fully cover for our exposure: cryptographic silence to unauthenticated
packets, short-lived session keys, replay windows, and strict no-amplification
(RFP §4.2), plus field-level requirements fixed by RFP §5:
`protocol_version, session_id, test_id, sequence_number, sender_timestamp, nonce,
payload_length, flags, authentication_tag`.

## Decision

Define **BNQO-STAMP v1**: a STAMP (RFC 8762) compatible-in-spirit but self-contained
UDP probe protocol implemented in the agent and reflector.

Wire format (probe packet, sender → reflector):

| Field | Size | Notes |
|---|---|---|
| `protocol_version` | u8 | 0x01; mismatch → silent drop |
| `flags` | u8 | bit0: reply-requested; bit1: timestamp-quality-low; bit2: MTU-probe (DF set); bit3: authenticated-mode |
| `payload_length` | u16 | total UDP payload length; enables padding to probe size |
| `session_id` | u64 | control-plane-issued, expires with session keys |
| `test_id` | u32 | maps to scheduled test/profile |
| `sequence_number` | u32 | per (session,direction); gap/reorder/dup analysis |
| `sender_timestamp` | 8 B | NTP-format (RFC 5905) seconds+fraction, UTC |
| `nonce` | 16 B | random per packet; replay-window member |
| `mbz` | 12 B | reserved, zeroed, covered by tag |
| `authentication_tag` | 16 B | AEAD tag (see below) |
| padding | 0..1400 B | zeroed, covered by tag; sized per FR-MEASURE-007 probe sizes |

Reflector reply mirrors header fields, copies `sender_timestamp`, adds its own
`receive_timestamp` and `reply_timestamp` (STAMP 4-timestamp model), and **truncates
padding so `reply_len ≤ request_len`** (no amplification, RFP §4.2).

Security mode: AEAD `ChaCha20-Poly1305` over header+padding with the nonce field as
AEAD nonce; key = per-session key derived via HKDF from a control-plane-issued
session secret + `session_id`, rotated with session expiry (RFP §4.2 short-lived
session keys). A reduced HMAC-SHA-256-128 mode (STAMP authenticated-mode
compatible) is kept for interop probes against third-party TWAMP/STAMP reflectors.
Unauthenticated packets receive **no reply** — the reflector is silent by default.

Receiver-side computations follow IPPM: loss from sequence gaps, reordering count +
distance (RFC 4737-style), duplicates, PDV/jitter per RFC 3393, one-way delay only
when clock-confidence gates pass (RFP §3: otherwise stored with status
`invalid-clock-sync`/`low-confidence`).

Optional **TWAMP interop mode** (RFP §3): a separate reflector configuration that
speaks RFC 5357/8762 toward third-party test equipment; disabled by default, same
registered-peer and rate-limit rules.

## Consequences

Positive:

- One protocol serves continuous profiles, burst tests, MTU probes, and diagnostic
  bursts (flag-driven), avoiding multiple probe implementations.
- 4-timestamp STAMP model gives per-direction OWD and reflector processing delay for
  free; field set maps 1:1 onto the RFP §13 measurement record.
- Silence-to-unauthenticated + AEAD + replay window + no-amplification satisfy
  RFP §4.2 by construction, and defeat the "UDP reflection" and "telemetry replay"
  threats of RFP §22.

Negative:

- Custom wire format means we own the spec, the parser, and the fuzzing (parser is a
  `cargo-fuzz` target; see ADR-0001).
- Not interoperable with off-the-shelf TWAMP test heads in the default mode — accepted;
  the interop mode covers that need explicitly.

## Alternatives considered

- **Raw TWAMP (RFC 5357)**: its control protocol (TCP, TWAMP-Control) adds a second
  channel and its security modes are optional and weakly deployed; bolting our
  session-key/replay/anti-amplification rules onto it yields a dialect anyway, with
  worse fit to the required field set. Rejected as the core; retained as interop mode.
- **OWAMP**: one-way focused, TCP control channel, no STAMP extensions, heavier
  session machinery, poor fit for high-frequency lightweight probing. Rejected.
