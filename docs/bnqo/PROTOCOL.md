# BNQO — Wire Protocol Design (Phase 0)

Status: Design (Phase 0). Normative references are to `RFP_DIGEST.md` sections (e.g. §5 = digest section 5).
Components: `bnqo-agent` (probe agent + host metrics, Rust/tokio), `bnqo-reflector` (secure reflector, Rust/tokio), control plane (`bnqo-cp`). All multi-byte integer fields on the wire are **big-endian (network byte order)** unless stated otherwise. All timestamps are UTC.

---

## 1. Scope and design constraints

This document defines:

1. **BNQO-UDP v1** — the authenticated bidirectional UDP probe protocol (§3, §5 FR-MEASURE-002).
2. **Session establishment and key management** for the probe protocol (§4.2, §10).
3. **Reflector behavior** — validation order, replay handling, silent-drop policy, anti-amplification (§4.2).
4. **Measurement math** — loss, burst loss, reordering, duplication, jitter per RFC 3393, RTT, one-way delay with clock-confidence marking (§3, §25).
5. **Control stream protocol** — the gRPC control channel between agent and control plane, including batch upload ACK semantics for at-least-once + idempotent delivery (§4.1, §15, §23).
6. **Protocol versioning and downgrade-attack prevention** (§22 threat "downgrade attack").

Design constraints inherited from the digest:

- No UDP reply to unauthenticated packets; response never larger than request (§4.2).
- Short-lived session keys; HMAC/AEAD verification; timestamp/sequence/nonce checks; replay window (§4.2).
- One-way delay is only ever reported with clock-confidence status; never as a precise value when sync is bad (§3, §31).
- STAMP (RFC 8762) inspired; TWAMP (RFC 5357) interop is optional and out of scope for v1 wire format (§3).
- Agents run autonomously during control-plane outage with the last valid signed config (§2, §4.1).

---

## 2. BNQO-UDP v1 probe packet format

### 2.1 Design goals

- Carry every field required by §5 FR-MEASURE-002: `protocol_version`, `session_id`, `test_id`, `sequence_number`, `sender_timestamp`, `nonce`, `payload_length`, `flags`, `authentication_tag`.
- Fixed header small enough that the smallest MTU-suite probe size (64 bytes, §7) is a legal packet: **fixed portion = 64 bytes = 48-byte authenticated header + 16-byte AEAD tag**. Variable padding follows the tag.
- Reflector must be able to stamp receive/transmit times **without growing the packet** (anti-amplification, §4.2): the sender pre-allocates zeroed timestamp fields which the reflector overwrites in place.
- Authenticated encryption per packet so that the first packet of a session already proves key possession — no in-band handshake on the UDP path is required or permitted (§4.2).

### 2.2 Byte-level layout

UDP payload of a BNQO-UDP v1 packet (offset in bytes from start of UDP payload):

| Offset | Size | Field                  | Type  | Description |
|-------:|-----:|------------------------|-------|-------------|
| 0      | 2    | `magic`                | u16   | Constant `0x424E` ("BN"). Demux + fast reject. |
| 2      | 1    | `protocol_version`     | u8    | `0x01` for BNQO-UDP v1. |
| 3      | 1    | `flags`                | u8    | Bit field, see §2.3. |
| 4      | 4    | `test_id`              | u32   | Control-plane-assigned test/profile instance id. |
| 8      | 8    | `session_id`           | u64   | Control-plane-assigned probe session id. |
| 16     | 4    | `sequence_number`      | u32   | Per-session, per-direction, strictly increasing by 1. |
| 20     | 8    | `sender_timestamp`     | ntp64 | Originator transmit time (§2.4). Echoed unchanged in reflections. |
| 28     | 8    | `receive_timestamp`    | ntp64 | **Zero in forward packets.** Reflector stamps its receive time. |
| 36     | 4    | `reflector_turnaround_us` | u32 | **Zero in forward packets.** Reflector stamps T3−T2 in microseconds (saturates at 2^32−1 ≈ 4294 s). |
| 40     | 4    | `nonce`                | u32   | Cryptographically random per packet. Feeds AEAD nonce and duplicate/replay disambiguation. |
| 44     | 2    | `payload_length`       | u16   | Total UDP payload length in bytes, **including** header, tag, and padding. Must equal actual UDP payload size. |
| 46     | 2    | `key_epoch`            | u16   | Session-key epoch for zero-downtime rekey (§3.3). |
| 48     | 16   | `authentication_tag`   | 16B   | XChaCha20-Poly1305 tag over the AAD (§2.5). |
| 64     | …    | `padding`              | bytes | Zero or more bytes to reach `payload_length`; see §2.6. |

Invariants:

- Minimum legal packet size: **64 bytes** (header + tag, no padding). Maximum: `payload_length ≤ 9000` (jumbo MTU tests); the reflector MUST NOT accept or emit packets larger than the per-session `max_packet_size` from the signed session config.
- Bytes 0..48 are the AEAD **additional authenticated data (AAD)**; they are transmitted in cleartext and integrity-protected. There is no per-packet ciphertext body: all header fields are needed in the clear for middlebox-friendly measurement, and padding carries no information. Confidentiality of measurement metadata is provided by the control/telemetry channel (mTLS), not the probe path — the probe path requires **authenticity and integrity**, which AEAD over AAD provides (§4.2, §22 "telemetry replay / result tampering").
- A reflected packet is the **same length** as the request, with only `flags.bit0`, `receive_timestamp`, `reflector_turnaround_us`, and `authentication_tag` modified. `sender_timestamp`, `sequence_number`, `nonce`, `session_id`, `test_id`, `payload_length`, `key_epoch`, and all padding bytes are echoed verbatim. Response size == request size, satisfying anti-amplification (§4.2) by construction.

### 2.3 Flag bits (`flags`, u8)

| Bit | Mask   | Name              | Meaning |
|----:|--------|-------------------|---------|
| 0   | `0x01` | `REFLECTED`       | 0 = forward probe from sender; 1 = reflected by reflector. |
| 1–2 | `0x06` | `CLOCK_QUALITY`   | Clock-sync state of the **writer** of the packet: `00` = unknown, `01` = good (within profile limit), `10` = low-confidence, `11` = invalid-clock-sync (§3, §8, §6.6). |
| 3   | `0x08` | `MTU_PROBE`       | Packet is part of an MTU/PMTU discovery suite (§7); reflector applies the MTU-suite rate limit. |
| 4   | `0x10` | `DF_REQUESTED`    | Informational copy of the IP Don't-Fragment bit used by the sender (receiver cannot see DF on reassembled datagrams). |
| 5   | `0x20` | `SESSION_TEARDOWN`| Last packet of the session; receiver releases session state after responding. |
| 6   | `0x40` | reserved          | MUST be zero; rejected otherwise. |
| 7   | `0x80` | reserved          | MUST be zero; rejected otherwise. |

The `CLOCK_QUALITY` bits travel in every packet so the receiver of a reflection can mark derived one-way delays without a separate side channel (§3).

### 2.4 Timestamp format (`sender_timestamp`, `receive_timestamp`)

NTP-style 64-bit fixed-point timestamp (same convention as STAMP/TWAMP, RFC 5905 §6):

- Upper 32 bits: seconds since 1900-01-01T00:00:00Z (NTP era 0).
- Lower 32 bits: fraction of a second; resolution ≈ 233 ps.

Conversion from Unix nanoseconds `t_ns`:

```
seconds     = floor(t_ns / 1e9) + 2_208_988_800        // NTP–Unix era offset
fraction    = round(((t_ns mod 1e9) / 1e9) * 2^32)
timestamp   = (seconds << 32) | fraction
```

Timestamps are taken from the agent's **UTC wall clock** (§4.1). Durations computed on a single host (e.g. reflector turnaround, RTT at the sender) use the host **monotonic clock** and are converted to the wire only at microsecond resolution (`reflector_turnaround_us`), per §4.1 ("monotonic clock for durations, UTC wall clock for events"). Senders SHOULD take `sender_timestamp` as close to the transmit syscall as practical (SO_TIMESTAMPING where available).

### 2.5 AEAD construction

**Chosen algorithm: XChaCha20-Poly1305** (RFC 8439 construction with extended 192-bit nonce, via `chacha20poly1305` Rust crate — constant-time, no AES-NI required).

Justification over AES-GCM:

1. **Nonce-misuse resistance headroom.** The 192-bit nonce comfortably fits a deterministic construction (below) with a per-packet random component; AES-GCM's 96-bit nonce with random components has a catastrophic birthday-bound failure mode.
2. **Constant-time in pure software** on both x86_64 and aarch64 VPS hardware common in Iran/outside hosting; AES-GCM without AES-NI is either slow or leaky (timing side channels).
3. Throughput (~1 GB/s per core software ChaCha20) is orders of magnitude above probe bandwidth budgets (§25: agent idle CPU <1%).

Per-packet construction:

```
nonce24 = nonce_salt[12 B]        // per-session, from KDF, NEVER on the wire
        || key_epoch[2 B, BE]     // wire
        || nonce[4 B, BE]         // wire (random per packet)
        || sequence_number[4 B,BE]// wire
        || flags[1 B]             // wire
        || protocol_version[1 B]  // wire

tag  = XChaCha20-Poly1305-Encrypt(key = packet_key,
                                  nonce = nonce24,
                                  plaintext = "",        // AAD-only mode
                                  aad = packet[0..48])
```

- Uniqueness guarantee: `sequence_number` is strictly increasing per (session, direction, key_epoch), therefore `(nonce_salt, key_epoch, seq)` never repeats for a given `packet_key`. The random 32-bit `nonce` field additionally (a) satisfies the §5 field list, (b) makes nonce uniqueness robust against implementation bugs that reuse sequence numbers, and (c) gives the duplicate detector a second disambiguator (network duplication copies nonce *and* seq; a broken sender reusing seq almost surely has a fresh nonce).
- Key separation per direction: `packet_key_fwd` and `packet_key_rev` are distinct (§3.2), so a reflection is never a valid forgery of a forward packet even though most fields are echoed.
- Tag verification MUST be constant-time; failed tags MUST NOT leak which bytes differ (§22 "invalid HMAC" scenario).

### 2.6 Padding for MTU/size tests (§7)

- Padding bytes follow the tag. Content: **deterministic pseudo-random** — `ChaCha8 keystream keyed by (packet_key, seq)` — so corruption is detectable by the receiver (payload integrity, §5 FR-MEASURE-002 "payload integrity / corrupted packets") and padding is not trivially compressible by middleboxes.
- The receiver verifies padding integrity only for sessions whose profile sets `verify_payload = true` (cheap check, off by default at high probe rates; always on for Profile A/B and MTU suites, §6).
- MTU suite sizes (§7): 64, 128, 256, 512, 1200, 1280, 1400, 1472, and `discovered_pmtu − 28` (IPv4) / `− 48` (IPv6). With `DF_REQUESTED` set and the socket `IP_MTU_DISCOVER=IP_PMTUDISC_DO`, send-side EMSGSIZE and inbound `ICMP_FRAG_NEEDED` are recorded as `error_class` values (§13). Padding never changes the 64-byte fixed portion; only `payload_length` grows.
- Reflector echoes padding **verbatim** (no re-verification of padding content on the reflector's hot path; the sender verifies reflected padding).

---

## 3. Session establishment and key management

### 3.1 Where session material comes from

There is **no in-band UDP handshake**. The control plane is the session broker (§4.2 "answers only registered peers"; §10 identity):

1. The control plane generates, per probe session:
   - `session_id` (u64, from CSPRNG, unique per link+time),
   - `test_id` (u32, references the test profile instance),
   - `key_seed` (32 bytes, CSPRNG),
   - `not_before_utc`, `not_after_utc` (session lifetime: **900 s default, max 3600 s** — "short-lived session keys", §4.2),
   - `key_epoch` (u16, starts at a random value, +1 per rekey),
   - policy: `max_rate_pps`, `max_packet_size`, `allowed_peer_ips`, `verify_payload`.
2. The material is delivered to **both** endpoints inside the signed configuration over the mutually-authenticated gRPC control stream (§7, `ProbeSessionConfig` in §9). Neither endpoint ever invents session parameters.
3. Both endpoints bind the session to the exact peer IP/port tuple from the config (§4.2 "binds only to configured IP/interface", §10 SR-IDENTITY-003 "connect to defined peers only").

Because both parties are already strongly authenticated (SPIFFE X.509-SVID mTLS, §10 SR-IDENTITY-001), the pre-distributed `key_seed` is itself a challenge–response substitute: possession of the derived `packet_key` — proven by a valid AEAD tag on the very first packet — authenticates the peer. A separate cookie exchange (à la DTLS) would add round trips and a stateless-reply surface; it is intentionally omitted. The anti-amplification property a cookie mechanism provides is instead guaranteed structurally: **the reflector emits nothing at all until a packet passes full AEAD verification** (§4).

### 3.2 Key derivation

```
PRK             = HKDF-Extract(salt = session_id_be64, IKM = key_seed)
packet_key_fwd  = HKDF-Expand(PRK, info = "bnqo-udp-v1|a2b|key", 32)
packet_key_rev  = HKDF-Expand(PRK, info = "bnqo-udp-v1|b2a|key", 32)
nonce_salt_fwd  = HKDF-Expand(PRK, info = "bnqo-udp-v1|a2b|salt", 12)
nonce_salt_rev  = HKDF-Expand(PRK, info = "bnqo-udp-v1|b2a|salt", 12)
verify_key      = HKDF-Expand(PRK, info = "bnqo-udp-v1|payload", 32)  // padding keystream
```

Direction labels `a2b`/`b2a` are relative to the link definition (§13 `direction`). On session rollover the control plane issues a new `key_seed` and `key_epoch` (see §3.3); keys are erased from memory at `not_after_utc + max_skew`.

### 3.3 Rekey and overlap

- The control plane pushes a successor `ProbeSessionConfig` (new `session_id`, `key_epoch+1`, `not_before = old.not_after − 30 s`) before expiry. Both endpoints accept **two overlapping sessions** (current + successor) during the 30 s overlap window, then the old session auto-expires (§4.2 "auto-expire sessions").
- An endpoint that misses the push (control-plane outage, §2) continues the current session until `not_after_utc`, then idles that test and reports `control-plane-unavailable` for new sessions; it never extends key lifetime on its own authority.

### 3.4 Anti-amplification summary (§4.2)

| Property | Mechanism |
|---|---|
| No reply to unauthenticated packets | Silent drop before any output (§4.3); AEAD tag = proof of possession. |
| Response ≤ request | Reflection is byte-for-byte the same length (§2.2). Error paths send nothing. |
| Reflection off unregistered peers | Session table lookup includes exact `(session_id, src_ip, src_port)`; unknown tuples dropped. |
| Rate abuse by a valid peer | Per-session token bucket (`max_rate_pps`) and global concurrent-session cap; excess dropped + counted. |

---

## 4. Reflector behavior (§4.2)

### 4.1 Session table

The reflector maintains an in-memory session table keyed by `(session_id, src_ip, src_port)`, populated **only** from signed configuration. Each entry holds: both direction keys (it needs only `rev` for sending and `fwd` for verifying, or vice versa depending on role), `not_before/not_after`, replay state, rate limiter, `max_packet_size`, stats block. Entries are hard-expired at `not_after + max_skew` (default `max_skew = 300 s`).

### 4.2 Replay window design

Per session+direction, receiver-side state:

- `H` — highest authenticated sequence number seen.
- Bitmap `W` of the last **4096** sequence numbers (`W` chosen ≥ 2× worst-case in-flight packets at `max_rate_pps` × reorder horizon; e.g. 1000 pps × 4 s = 4000).

Packet with sequence `s`:

```
if s > H:                 # forward progress
    shift W right by (s − H); set bit 0; H = s
elif H − s >= 4096:       # too old to be plausible reordering
    drop as STALE (counted)
elif bit(H − s) set:      # seen before
    DUPLICATE path (§4.4)
else:
    set bit(H − s)        # late/reordered arrival — accept
```

Rationale: strict replay rejection (drop all duplicates) would destroy the duplicate-detection measurement signal required by §5 FR-MEASURE-002 — a network-duplicated packet is bit-identical to an attacker-replayed packet. The window therefore **accepts and reflects** duplicates but caps them (§4.4) and accounts for them separately.

### 4.3 Validation order (every inbound UDP datagram)

Order matters: cheap structural checks first, cryptographic check before any state mutation, and **no output of any kind** until step 6 succeeds.

1. **Length/structure:** 64 ≤ len ≤ `max_udp_payload`; else drop (`drop_malformed`).
2. **Magic/version:** `magic == 0x424E`, `protocol_version` supported (≥ `min_supported_protocol_version` from signed config, §8); reserved flag bits zero; else drop (`drop_bad_version`).
3. **Session lookup:** `(session_id, src_ip, src_port)` in table and `not_before ≤ now ≤ not_after + max_skew`; else drop (`drop_unknown_session`).
4. **Rate/concurrency:** per-session token bucket and global reflector load shed; else drop (`drop_rate_limited`).
5. **AEAD verify:** recompute tag with `packet_key_fwd`, `key_epoch`, constant-time compare; else drop (`drop_auth_fail`).
6. **Timestamp sanity:** `|now − sender_timestamp| ≤ max_skew`; else drop (`drop_stale_timestamp`). (Bounds time-shifted replay, §22 "time manipulation".)
7. **Replay window:** apply §4.2; `STALE` → drop (`drop_replay_stale`); `DUPLICATE` → §4.4; else accept.
8. **Reflect:** copy datagram buffer, set `REFLECTED` flag, stamp `receive_timestamp` (T2), recompute `reflector_turnaround_us` at send time, re-tag with `packet_key_rev`, transmit to the exact source tuple. Same length in, same length out.

### 4.4 Duplicate reflection cap

For an authenticated duplicate (step 7 DUPLICATE path): reflect it **only if** fewer than 8 reflections have been emitted for this `sequence_number`; increment `dup_reflected` / `dup_suppressed` counters accordingly. The cap bounds replay-storm amplification (an attacker replaying a captured packet at line rate still gets ≤8 equal-size responses per captured packet) while preserving legitimate duplicate measurement (real networks rarely duplicate >2×).

### 4.5 Silent-drop policy and error statistics

- Every drop path above is **silent**: zero bytes are emitted. There is no ICMP-style error packet and no "unauthenticated error response" (§4.2, §25 "reflector silent to unknown packets").
- Statistics: per-session counters `{drop_malformed, drop_bad_version, drop_unknown_session, drop_rate_limited, drop_auth_fail, drop_replay_stale, drop_stale_timestamp, dup_reflected, dup_suppressed, rx_ok, tx_ok}` exported via OTLP every 10 s (labels: `session_id`, `test_id`, `peer_id`) and surfaced on the security dashboard (§16 "replay attempts, invalid probes").
- Audit logging: drops are **sampled** (first occurrence per counter per minute + 1% sample) into the audit stream; never per-packet logging (disk-exhaustion and log-injection guard, §22). `drop_auth_fail` rate above profile threshold raises the `reflector-auth-attack` alert (§17).

---

## 5. Sender behavior

- Sends at the profile's jittered interval (§6 Profile A: jittered intervals) drawn uniformly from `[0.5·T, 1.5·T]` to avoid phase locking with periodic congestion.
- Takes `sender_timestamp` at transmit; stores `(seq, monotonic_send_time, nonce)` in a ring buffer sized to the loss-timeout horizon.
- On receiving a reflection: validates magic/version, `REFLECTED` bit, session binding, AEAD tag (`packet_key_rev`), then matches `(seq, nonce)` to the ring. Reflections failing validation are counted (`rx_invalid`) and dropped — the sender is a reflector for the reverse direction and applies §4 symmetrically.
- Declares a packet **lost** when no reflection arrives within `loss_timeout = max(2 s, 3 × RTT_p99_recent)` (bounds loss accounting without unbounded state; §25 loss accuracy is verified against reference pcaps in the netem matrix, §24).

---

## 6. Measurement math

Notation for packet `i` on a session:

- `T1_i` — sender transmit time (sender clock).
- `T2_i` — reflector receive time (reflector clock).
- `T3_i` — reflector transmit time (reflector clock) = `T2_i + reflector_turnaround_us`.
- `T4_i` — sender receive time of the reflection (sender clock).
- Over a measurement window `W` (e.g. 10–30 s summary, §6 Profile A): `S` = set of sequence numbers sent, `R` = set received back, `S_ref`/`R_ref` = reflector-side sent/received sets reported via the reflector's telemetry counters (piggybacked in its own measurement stream — the reflector is also an agent, §2).

### 6.1 Round-trip time

```
RTT_i = (T4_i − T1_i) − (T3_i − T2_i)
```

Both parenthesized terms are single-clock durations (monotonic on their hosts), so RTT is **immune to clock offset** between hosts. Reported per window: min/avg/p50/p95/p99/max/stddev (§5 FR-MEASURE-001/002, §13).

### 6.2 One-way delay and clock-offset correction

Let `θ` be the estimated clock offset (reflector clock − sender clock) and `u` the uncertainty, both primarily from the host clock telemetry (NTP `offset`/`rootdisp`, or PTP `offsetFromMaster`/`meanPathDelay`, §7 host metrics — not derived from probe traffic).

```
OWD_fwd_i = (T2_i − T1_i) − θ
OWD_rev_i = (T4_i − T3_i) + θ
```

Sanity cross-check (symmetry assumption, used only for diagnostics, never for correction):

```
θ_probe = ((T2_i − T1_i) − (T4_i − T3_i)) / 2     # assumes OWD_fwd == OWD_rev
```

Large `|θ_probe − θ_ntp|` indicates path asymmetry (itself a §1 detection goal) — recorded as evidence, not "corrected away".

**Marking rules (§3, §31 "no OWD without clock confidence")** — `u_max` is the profile limit, default 5 ms:

| Condition | `clock_quality` | Stored OWD |
|---|---|---|
| `u ≤ u_max` and source locked (NTP synchronized / PTP locked) | `good` (`01`) | Precise value. |
| `u_max < u ≤ 10·u_max` or source recently re-selected | `low-confidence` (`10`) | Value stored, flagged; excluded from alerting baselines. |
| `u > 10·u_max`, unsynchronized, or no clock telemetry | `invalid-clock-sync` (`11`) | Stored with status `invalid-clock-sync`; **never** presented as a precise value (§3). |

`clock_quality` is stamped into each packet's `CLOCK_QUALITY` bits by both writers (§2.3) and into the measurement record fields `clock_offset_ms`, `clock_uncertainty_ms`, `clock_quality` (§13). Link status contribution: `clock-unsynchronized` (§8).

### 6.3 Loss (per direction, per window)

Forward (sender → reflector) and reverse (reflector → sender) are computed independently (§1, §31 "every metric has explicit direction"):

```
sent_fwd      = |S|
recv_fwd      = |S_ref|                       # reflector's authenticated receive count
lost_fwd      = sent_fwd − recv_fwd
loss_fwd_%    = 100 · lost_fwd / sent_fwd

sent_rev      = |S_ref|  (reflected)          # = recv_fwd, minus reflector overload drops
recv_rev      = |R|
lost_rev      = sent_rev − recv_rev
loss_rev_%    = 100 · lost_rev / sent_rev

loss_roundtrip_% = 100 · (|S| − |R|) / |S|    # cannot attribute direction alone
```

A packet counts as lost only after `loss_timeout` (§5). Reflector overload drops (its own `drop_rate_limited`) are exported separately so they are never misattributed to network loss (§1 "host-level faults masquerading as network faults").

### 6.4 Burst loss (§5 "burst loss, consecutive-loss length")

Scan the window's loss indicator sequence `L_i ∈ {0,1}` ordered by `seq`:

- A **burst** is a maximal run of consecutive 1s.
- `burst_loss_count` = number of runs of length ≥ 2 (isolated single losses are not bursts).
- `max_loss_burst` = length of the longest run.
- Also emitted: `micro_outage_count` = runs of length ≥ `micro_outage_min` (default 3) spanning ≥ 100 ms — feeds §1 "micro-outages" and §9 "repeated micro-outages".

### 6.5 Reordering and duplication

Receiver-side, per direction, over the window:

- **Reordering distance:** let `M` = highest seq received so far. An arrival with seq `s < M` is reordered with distance `d = M − s`. Report `reordered_packets`, `max_reorder_distance`, and a fixed-bucket histogram of `d` (§5 "reordering count+distance").
- **Duplicates:** an arrival whose `(seq, nonce)` pair exactly matches a previously received packet → `duplicate_packets++`. Same `seq`, different `nonce` → `corrupted_packets++` (sender bug or on-path tampering, §22) and is additionally counted as a security event.
- **Corrupted:** AEAD failure at receiver → `corrupted_packets` (§5); padding-mismatch when `verify_payload` → `payload_integrity_fail`.
- **Inter-arrival distribution:** gaps `a_i = T4_i − T4_{i-1}` on received reflections, emitted as a log-scale histogram (16 buckets, 0.1 ms … 10 s) for §5 "inter-arrival distribution".
- **Effective bitrate:** `8 · Σ payload_length(received) / window_duration`, per direction (§5 "effective bitrate").

### 6.6 Jitter / PDV (RFC 3393, §3)

Per direction, using single-clock transit differences. For the reverse direction at the sender, `D_i = T4_i` is the only same-clock arrival series available, so PDV is computed on **relative transit times**:

```
RT_i  = T4_i − T3_i            # reverse transit (needs θ only for absolute OWD, NOT for jitter)
D_i   = RT_i − RT_{i-1}        # variation between consecutive received packets
J_i   = J_{i-1} + (|D_i| − J_{i-1}) / 16     # RFC 3393 §4.6 estimator
```

- Consecutive means consecutive **received** packets in sequence order (RFC 3393 selection function: all in-order received packets; reordered packets are included per their arrival and flagged).
- Reported as `jitter_ms` (final `J` of window) plus `pdv_p95_ms` (95th percentile of `|D_i|`) per direction. Clock offset `θ` cancels in the difference; jitter is valid even under `low-confidence` clocks, which is why jitter thresholds (§9) remain active when OWD is suppressed.

---

## 7. Control stream protocol (gRPC)

Transport: gRPC over TLS 1.3 with mutual authentication using SPIFFE X.509-SVIDs (§10, §11). The agent dials the control plane (agents are never required to listen); the session is a single bidirectional stream `AgentControl/OpenControlStream` (§23) that multiplexes config push, job dispatch, heartbeat, and batch upload. Long-lived stream, automatic reconnect with exponential backoff + jitter (§15).

### 7.1 Stream lifecycle

```
Agent                                Control plane
  | ---- ClientHello ------------->  |  agent_id, boot_id, last_applied_config_version,
  |                                  |  last_upload_seq, agent_version, protocol_versions[]
  | <---- ServerHello -------------  |  server_time_utc, min_supported_protocol_version,
  |                                  |  heartbeat_interval, resume_from_seq
  |                                  |
  | <== ConfigPush (signed) ==       |  on change / resume after outage
  | == ConfigAck =================>  |  applied (config_version) or rejected(reason)
  |                                  |
  | <== JobDispatch (signed) ====    |  typed jobs only (§12)
  | == JobAck / JobResult =========> |  accepted/rejected/completed
  |                                  |
  | == MeasurementBatch ==========>  |  upload_seq-ordered batches (§7.2)
  | <== BatchAck ==================  |  cumulative high-watermark ACK
  |                                  |
  | == Heartbeat ==================> |  every heartbeat_interval (§7.3)
  | <== HeartbeatAck =============   |
```

### 7.2 Batch upload: at-least-once + idempotent delivery (§4.1, §15)

- Every `MeasurementBatch` carries `(agent_id, upload_seq)` where `upload_seq` is a **strictly increasing per-agent u64 persisted in the WAL header** (survives restarts). Batches also carry `config_version` and per-record `(measurement_id, record_seq)` keys.
- The control plane tracks `high_watermark[agent_id]` = highest contiguous applied `upload_seq`.
- `BatchAck{batch_id, upload_seq, status, high_watermark, duplicate_ranges[]}` semantics:
  - `APPLIED` — batch applied; watermark advanced (possibly beyond this seq if it fills a gap).
  - `DUPLICATE` — batch already applied (idempotent hit); safe for the agent to truncate WAL.
  - `REJECTED` — permanent (schema/signature/authz); agent moves batch to WAL dead-letter and reports `error_class`.
- Agent-side rules: resend un-ACKed batches in `upload_seq` order after reconnect ("ordered resend after reconnect", §4.1); truncate WAL segments only below the cumulative `high_watermark`; retry with exponential backoff + jitter (§15).
- Server-side dedup key: `(agent_id, measurement_id)` unique constraint in the ingest path plus watermark comparison — together these make redelivery safe and exactly the §25 "no logical duplicates after reconnect" criterion.

### 7.3 Heartbeat

`Heartbeat` every `heartbeat_interval` (default 10 s, CP-adjustable) carries: `agent_time_utc`, `uptime`, `config_version`, `last_upload_seq`, WAL depth/utilization, clock summary (`clock_offset_ms`, `clock_uncertainty_ms`, `clock_source`, `clock_quality`), and one-line health rollup. Missing heartbeats drive `agent-unhealthy` / `telemetry-delayed` link statuses (§8) and `agent offline`/`telemetry stale` alerts (§17). Heartbeats are idempotent by sequence (`heartbeat_seq`); stale heartbeats are logged, not applied.

### 7.4 Config push and anti-rollback

`ConfigPush` carries a `SignedConfiguration` (§9.1). The agent verifies: Ed25519 signature against the pinned control-plane signing key set (with `key_id` rotation support), `agent_id` match, `not_before/not_after` validity, and **monotonicity: `config_version > last_applied_config_version`** (§22 "config tampering / downgrade attack"). On success it applies atomically (new config staged, validated, swapped; on validation failure the previous config stays active) and sends `ConfigAck{applied=true, config_version}`. On failure: `ConfigAck{applied=false, reject_reason}`, previous config untouched (§25 "invalid config never replaces valid one").

---

## 8. Versioning and downgrade-attack prevention (§22, §26)

- `protocol_version` (u8) in every probe packet and `protocol_versions[]` in `ClientHello`; the control plane's signed config includes `min_supported_protocol_version`. Endpoints refuse sessions/packets below the floor (`drop_bad_version`, §4.3).
- Config anti-rollback: monotonic `config_version` (§7.4); signing-key rotation via `key_id` set where new keys are introduced *by a config signed with an old key* and old keys carry `not_after`; a config signed by a retired key is rejected.
- Job anti-replay: `job_id` uniqueness + `expires_at` + agent-side executed-job cache (§12 "rejects expired/unsigned/duplicate/out-of-policy jobs").
- Version skew policy (§26): agent must interwork with a control plane one minor version ahead/behind; unknown enum values and unknown proto fields MUST be tolerated (proto3 `reserved` discipline, §9) and never silently enable new behavior.

---

## 9. Protobuf sketch (proto3)

File: `proto/bnqo/v1/control.proto` (excerpt — RPC service surface is defined fully in `API.md`; this sketch covers wire-relevant messages: signed config, probe session, job envelope with all §12 fields, measurement batch).

```proto
syntax = "proto3";
package bnqo.v1;

import "google/protobuf/timestamp.proto";
import "google/protobuf/duration.proto";

// ---------- Signed configuration (§4.1, §7.4) ----------

message SignedConfiguration {
  bytes payload = 1;        // canonical-serialized Configuration
  string key_id = 2;        // control-plane signing key id (rotation support, §8)
  bytes signature = 3;      // Ed25519 over ("bnqo-config-v1" || payload)
}

message Configuration {
  string agent_id = 1;
  uint64 config_version = 2;            // strictly monotonic per agent (anti-rollback)
  uint32 min_supported_protocol_version = 3;
  google.protobuf.Timestamp not_before = 4;
  google.protobuf.Timestamp not_after = 5;
  repeated ProbeSessionConfig probe_sessions = 6;
  repeated TestProfile test_profiles = 7;
  TelemetryPolicy telemetry = 8;
  ResourceLimits resource_limits = 9;   // CPU/RAM/disk/bandwidth (§4.1)
}

message ProbeSessionConfig {
  uint64 session_id = 1;
  uint32 test_id = 2;
  string link_id = 3;
  Direction direction = 4;              // this agent's sending direction
  string peer_id = 5;
  string peer_address = 6;              // exact IP (no DNS at probe time, §10-003)
  uint32 peer_port = 7;
  uint32 local_port = 8;
  bytes key_seed = 9;                   // 32 B; HKDF input (§3.2)
  uint32 key_epoch = 10;
  google.protobuf.Timestamp not_before = 11;
  google.protobuf.Timestamp not_after = 12;  // short-lived (§4.2)
  uint32 max_rate_pps = 13;
  uint32 max_packet_size = 14;
  bool verify_payload = 15;
  uint32 loss_timeout_ms = 16;
  double clock_uncertainty_max_ms = 17; // u_max (§6.2)
}

enum Direction {
  DIRECTION_UNSPECIFIED = 0;
  DIRECTION_A_TO_B = 1;   // Iran→Outside by link convention
  DIRECTION_B_TO_A = 2;
}

enum ClockQuality {                      // mirrors packet flags §2.3
  CLOCK_QUALITY_UNSPECIFIED = 0;
  CLOCK_QUALITY_GOOD = 1;
  CLOCK_QUALITY_LOW_CONFIDENCE = 2;
  CLOCK_QUALITY_INVALID_SYNC = 3;
}

// ---------- Test profiles (§6) ----------

message TestProfile {
  uint32 test_id = 1;
  ProfileClass profile_class = 2;   // A/B/C/D (§6)
  ServiceClass service_class = 3;   // Web/RealTime/VoiceVideo/Tunnel/Bulk/Mgmt (§9)
  uint32 interval_ms = 4;
  double jitter_fraction = 5;
  uint32 packet_size = 6;
  uint32 dscp = 7;
  uint32 summary_interval_ms = 8;   // 10–30 s (Profile A)
  bool enable_icmp = 9;
  bool enable_tcp_connect = 10;
  repeated uint32 mtu_probe_sizes = 11;  // §7
}

enum ProfileClass {
  PROFILE_CLASS_UNSPECIFIED = 0;
  PROFILE_CLASS_A_CONTINUOUS_LIGHTWEIGHT = 1;
  PROFILE_CLASS_B_SERVICE_QUALITY = 2;
  PROFILE_CLASS_C_DEEP_DIAGNOSTIC = 3;
  PROFILE_CLASS_D_SCHEDULED_CAPACITY = 4;
}

enum ServiceClass {
  SERVICE_CLASS_UNSPECIFIED = 0;
  SERVICE_CLASS_WEB = 1;
  SERVICE_CLASS_REAL_TIME = 2;
  SERVICE_CLASS_VOICE_VIDEO = 3;
  SERVICE_CLASS_TUNNEL = 4;
  SERVICE_CLASS_BULK_TRANSFER = 5;
  SERVICE_CLASS_MANAGEMENT_PANEL = 6;
}

// ---------- Job envelope — all §12 fields ----------

message SignedJobEnvelope {
  bytes payload = 1;        // canonical-serialized JobEnvelope
  string key_id = 2;
  bytes signature = 3;      // Ed25519 over ("bnqo-job-v1" || payload)
}

message JobEnvelope {           // §12 job fields, verbatim
  string job_id = 1;            // UUIDv7 — also the idempotency key
  JobType job_type = 2;         // typed jobs ONLY (§12)
  string agent_id = 3;
  string peer_id = 4;
  google.protobuf.Struct parameters = 5;  // per-type schema-validated (§11)
  google.protobuf.Timestamp created_at = 6;
  google.protobuf.Timestamp not_before = 7;
  google.protobuf.Timestamp expires_at = 8;
  google.protobuf.Duration max_duration = 9;
  uint64 max_bandwidth_bps = 10;
  string approval_id = 11;      // required for heavy tests (§4.3, §5-008)
  uint64 config_version = 12;   // config under which the job was authorized
  // signature lives in SignedJobEnvelope (detached, like SignedConfiguration)
}

enum JobType {                  // §12 closed set; anything else MUST be rejected
  JOB_TYPE_UNSPECIFIED = 0;
  JOB_TYPE_RUN_ICMP_PROBE = 1;
  JOB_TYPE_RUN_UDP_PROBE = 2;
  JOB_TYPE_RUN_TCP_PROBE = 3;
  JOB_TYPE_RUN_TLS_PROBE = 4;
  JOB_TYPE_RUN_MTR = 5;
  JOB_TYPE_RUN_ROUTE_TRACE = 6;
  JOB_TYPE_RUN_MTU_DISCOVERY = 7;
  JOB_TYPE_RUN_THROUGHPUT_TEST = 8;
  JOB_TYPE_COLLECT_HOST_SNAPSHOT = 9;
  JOB_TYPE_ROTATE_IDENTITY = 10;
  JOB_TYPE_UPDATE_SIGNED_CONFIG = 11;
}

// ---------- Measurement batch upload (§7.2, §13) ----------

message MeasurementBatch {
  string batch_id = 1;              // UUIDv7
  string agent_id = 2;
  uint64 upload_seq = 3;            // strictly increasing, WAL-persisted
  uint64 config_version = 4;
  uint32 schema_version = 5;        // event schema versioning (§13, DATA_MODEL §7)
  repeated MeasurementRecord records = 6;
  repeated RouteObservation routes = 7;
  bytes batch_checksum = 8;         // SHA-256 over records (tamper-evidence in WAL relay)
}

message MeasurementRecord {
  string measurement_id = 1;        // UUIDv7 — server dedup key (§7.2)
  uint32 record_seq = 2;
  uint64 session_id = 3;
  uint32 test_id = 4;
  string link_id = 5;
  Direction direction = 6;
  TestType test_type = 7;
  google.protobuf.Timestamp window_start_utc = 8;
  google.protobuf.Timestamp window_end_utc = 9;
  uint64 duration_monotonic_ns = 10;
  // counters (§6.3–6.5)
  uint64 packets_sent = 11;
  uint64 packets_received = 12;
  double loss_percent = 13;
  uint32 burst_loss_count = 14;
  uint32 max_loss_burst = 15;
  uint32 duplicate_packets = 16;
  uint32 reordered_packets = 17;
  uint32 max_reorder_distance = 18;
  uint32 corrupted_packets = 19;
  // latency (§6.1, §6.2, §6.6)
  double rtt_min_ms = 20;
  double rtt_avg_ms = 21;
  double rtt_p50_ms = 22;
  double rtt_p95_ms = 23;
  double rtt_p99_ms = 24;
  double rtt_max_ms = 25;
  double owd_p50_ms = 26;           // meaning gated by clock_quality (§6.2)
  double owd_p95_ms = 27;
  double owd_p99_ms = 28;
  double jitter_ms = 29;
  double pdv_p95_ms = 30;
  // clock state
  double clock_offset_ms = 31;
  double clock_uncertainty_ms = 32;
  ClockQuality clock_quality = 33;
  LinkStatus status = 34;           // §8 encoding, defined in API.md
  double confidence = 35;           // 0..1 (§9)
  string error_class = 36;
  string agent_version = 37;
  string reflector_version = 38;
}

enum TestType {
  TEST_TYPE_UNSPECIFIED = 0;
  TEST_TYPE_ICMP_PROBE = 1;
  TEST_TYPE_UDP_PROBE = 2;
  TEST_TYPE_TCP_PROBE = 3;
  TEST_TYPE_TLS_PROBE = 4;
  TEST_TYPE_APP_SYNTHETIC = 5;
  TEST_TYPE_MTR = 6;
  TEST_TYPE_MTU_DISCOVERY = 7;
  TEST_TYPE_THROUGHPUT = 8;
}

message RouteObservation {          // §13 route record summary; hops in DATA_MODEL
  string route_id = 1;
  string measurement_id = 2;
  bytes route_hash = 3;             // 16 B BLAKE3 over ordered hop addresses
  bool destination_reached = 4;
  uint32 hop_count = 5;
}

// ---------- Control stream envelopes (§7.1) ----------

message ClientEvent {
  oneof event {
    ClientHello hello = 1;
    Heartbeat heartbeat = 2;
    ConfigAck config_ack = 3;
    JobAck job_ack = 4;
    JobResult job_result = 5;
    MeasurementBatch batch = 6;
  }
}

message ServerCommand {
  oneof command {
    ServerHello hello = 1;
    SignedConfiguration config_push = 2;
    SignedJobEnvelope job_dispatch = 3;
    BatchAck batch_ack = 4;
    HeartbeatAck heartbeat_ack = 5;
  }
}

message ClientHello {
  string agent_id = 1;
  string boot_id = 2;                       // changes on every agent restart
  uint64 last_applied_config_version = 3;
  uint64 last_upload_seq = 4;
  string agent_version = 5;
  repeated uint32 protocol_versions = 6;    // supported probe protocol versions
}

message ServerHello {
  google.protobuf.Timestamp server_time_utc = 1;
  uint32 min_supported_protocol_version = 2;
  uint32 heartbeat_interval_ms = 3;
  uint64 resume_from_seq = 4;               // server high-watermark + 1 (§7.2)
}

message ConfigAck {
  uint64 config_version = 1;
  bool applied = 2;
  string reject_reason = 3;
}

message JobAck {
  string job_id = 1;
  JobAckStatus status = 2;    // ACCEPTED / REJECTED_EXPIRED / REJECTED_SIGNATURE /
                              // REJECTED_DUPLICATE / REJECTED_OUT_OF_POLICY (§12)
  string reason = 3;
}

enum JobAckStatus {
  JOB_ACK_STATUS_UNSPECIFIED = 0;
  JOB_ACK_STATUS_ACCEPTED = 1;
  JOB_ACK_STATUS_REJECTED_EXPIRED = 2;
  JOB_ACK_STATUS_REJECTED_SIGNATURE = 3;
  JOB_ACK_STATUS_REJECTED_DUPLICATE = 4;
  JOB_ACK_STATUS_REJECTED_OUT_OF_POLICY = 5;
}

message JobResult {
  string job_id = 1;
  google.protobuf.Timestamp started_at_utc = 2;
  google.protobuf.Timestamp ended_at_utc = 3;
  JobResultStatus status = 4;
  string error_class = 5;
  string measurement_id = 6;          // result rows findable in the event store
  string diagnostic_artifact_uri = 7; // object-storage key, if any (§14)
}

enum JobResultStatus {
  JOB_RESULT_STATUS_UNSPECIFIED = 0;
  JOB_RESULT_STATUS_COMPLETED = 1;
  JOB_RESULT_STATUS_FAILED = 2;
  JOB_RESULT_STATUS_CANCELED = 3;
  JOB_RESULT_STATUS_AUTO_STOPPED = 4; // production-harm auto-stop (§5-008, §6-D)
}

message BatchAck {
  string batch_id = 1;
  uint64 upload_seq = 2;
  BatchAckStatus status = 3;          // APPLIED / DUPLICATE / REJECTED
  uint64 high_watermark = 4;          // cumulative contiguous watermark (§7.2)
  string reject_reason = 5;
}

enum BatchAckStatus {
  BATCH_ACK_STATUS_UNSPECIFIED = 0;
  BATCH_ACK_STATUS_APPLIED = 1;
  BATCH_ACK_STATUS_DUPLICATE = 2;
  BATCH_ACK_STATUS_REJECTED = 3;
}

message Heartbeat {
  string agent_id = 1;
  uint64 heartbeat_seq = 2;
  google.protobuf.Timestamp agent_time_utc = 3;
  google.protobuf.Duration uptime = 4;
  uint64 config_version = 5;
  uint64 last_upload_seq = 6;
  uint64 wal_depth_records = 7;
  double wal_utilization = 8;         // 0..1, drives "queue near capacity" (§17)
  double clock_offset_ms = 9;
  double clock_uncertainty_ms = 10;
  string clock_source = 11;           // "ntp" | "ptp" | "none"
  ClockQuality clock_quality = 12;
  string health_rollup = 13;
}

message HeartbeatAck {
  uint64 heartbeat_seq = 1;
  google.protobuf.Timestamp server_time_utc = 2;  // coarse clock sanity signal
}
```

Field-number discipline: numbers 19000–19999 are reserved per message for experimental fields; removed fields MUST use `reserved` numbers and names so one-version-skew peers (§8) never misparse.

---

## 10. Traceability summary

| Design element | Digest basis |
|---|---|
| 64-byte fixed packet, required fields, flags | §5 FR-MEASURE-002, §7 probe sizes |
| NTP-64 timestamps, monotonic durations | §3 (STAMP), §4.1 |
| XChaCha20-Poly1305, per-direction keys | §4.2 AEAD, §22 tampering |
| No handshake, CP-issued short-lived keys | §4.2, §10, §2 |
| Silent drop, ≤-size response, replay window | §4.2, §25 |
| Per-direction loss, RFC 3393 jitter, clock gating | §1, §3, §5, §31 |
| Streamed config/jobs, batch ACK watermark | §4.1, §12, §15, §23 |
| Anti-rollback, min protocol version | §22 downgrade, §26 |
