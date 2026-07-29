# BNQO — Test Strategy

Status: Phase-0 planning document. Normative source: `docs/bnqo/RFP_DIGEST.md` (cited as §N). Companions: `IMPLEMENTATION_PLAN.md`, `SLO.md`. Per §31, security and failure tests ship alongside code, not in a later phase.

## 1. Test pyramid

| Layer | Covers | Tooling | Runs |
|-------|--------|---------|------|
| Unit | PDV/jitter math (RFC 3393), loss/burst/reorder/dup classifiers, WAL append/replay, config/job signature verify, packet codec round-trips, threshold/baseline logic | `cargo test`, `cargo nextest`, `proptest` for codec invariants, `loom` for lock-free WAL paths | Per PR |
| Integration | agent↔reflector sessions, WAL→uploader→ingest ACK, enrollment→identity→config flow, scheduler→job→agent execution, dedup/idempotency | `cargo nextest` with testcontainers (Postgres, NATS/Kafka, ClickHouse), `tokio-test` for async control | Per PR |
| E2E | full path: enroll → signed config → scheduled probes → ingest → storage → query API → dashboard data; incident → alert | docker-compose lab (dev-only per §27), kind/k3s for CP | Per PR (smoke), nightly (full) |
| Netem | complete §24 failure scenario matrix (§4 of this doc) | `tc netem` + Linux network namespaces, parametrized harness (`tests/netem/`) | Nightly; blocking subset per PR |
| Security | §22 threat validation tests, §24 security scenarios, §25 security acceptance | nftables, custom attack scripts, cosign/cert tooling, OWASP ZAP for REST | Nightly + pre-release |
| Fuzz | packet parser, session handshake, job envelope, config parser, gRPC handlers, WAL record parser | `cargo-fuzz` (libfuzzer), structured corpus in `fuzz/corpus/`, `go test -fuzz` for any Go handlers | Short per PR; 1h+ nightly; OSS-Fuzz later |
| Performance | §25 targets: idle CPU <1%, RSS <150MB, ingest p95 <5s, dashboard freshness <15s; scale to 1,000-agent projection | `criterion` (micro), pidstat/psrecord, k6/custom load generator, Prometheus | Nightly smoke; full pre-release |
| Chaos | CP/collector/DB/MQ down, network partition, clock drift, cert expiry mid-run, rolling upgrades | namespace isolation + nftables, `pumba`/manual container kills, `libfaketime`/chrony in VMs | Nightly subset; weekly full |

Coverage expectations: ≥80% line coverage on `bnqo-measure`, `bnqo-security`, `bnqo-storage` (WAL); 100% branch coverage on signature-verification and replay-window code paths.

## 2. Netem harness specification (`tests/netem/`)

All §24 scenarios execute inside Linux network namespaces — no physical lab rewiring, fully scriptable in CI (nightly runners with `CAP_NET_ADMIN`).

Topology builder (per scenario run):

```bash
# three namespaces: agent A (Iran side), middle M (emulates WAN), agent B (outside)
ip netns add A; ip netns add M; ip netns add B
ip link add vethA type veth peer name vethA-M
ip link add vethB type veth peer name vethB-M
ip link set vethA netns A;   ip link set vethA-M netns M
ip link set vethB netns B;   ip link set vethB-M netns M
# addressing: A=10.0.0.1/30, M has 10.0.0.2 + 10.0.1.2, B=10.0.1.1/30; M forwards (net.ipv4.ip_forward=1)
# impairment applied on M's egress queues => per-direction control:
#   A->B impairment: tc on vethA-M (M side facing B traffic path), B->A: tc on vethB-M
ip netns exec M tc qdisc add dev vethA-M root netem loss 1%
```

Direction convention: impairments applied on the M-side interface that *forwards toward B* affect the A→B direction only; symmetric rules on both interfaces = bidirectional. Every scenario asserts per-direction metrics separately (§1, §8).

Harness layout:

- `tests/netem/harness/topo.sh` — namespace/veth lifecycle, cleanup trap, optional 3-hop topology (A→M1→M2→B) for route scenarios.
- `tests/netem/harness/run_scenario.sh <scenario.yaml>` — applies impairment, starts `bnqo-agent` + `bnqo-reflector` binaries (release build) in A and B, runs workload, captures pcaps both ends (`tcpdump -i any -w`), collects agent output, runs assertions + pcap reconciliation (§5), tears down.
- `tests/netem/scenarios/*.yaml` — one file per scenario row below: `{ name, impairment_cmds: [...], duration_s, workload: {profile, rate}, expect: { ... assertions ... }, tags: [pr-blocking|nightly|weekly] }`.
- `tests/netem/bin/pcap-reconcile` — reference loss/RTT extractor (§5.1).

Assertion model: each scenario declares expectations on agent-reported metrics (directional loss %, jitter range, status classification per §8, alert/not-alert, confidence). Tolerances defined in §5.

## 3. Notes on realism

- Namespaces emulate link-layer conditions well (loss, delay, reorder, MTU). Host-failure scenarios run against the agent process inside its namespace with cgroup limits. Platform-failure scenarios (CP/DB/MQ down) use the kind/k3s lab, not namespaces.
- Scenarios marked **[VM]** (clock drift, some host failures) run on nested VMs or dedicated runners because `date`/clock namespaces don't fully virtualize `CLOCK_REALTIME` behavior needed for clock-drift tests; use `libfaketime` where acceptable and real VMs for §25 clock acceptance.

## 4. Complete netem scenario matrix (§24)

Every row = one scenario YAML. Commands shown as executed on the middle namespace M unless noted; `dev vethA-M` = A→B direction, `dev vethB-M` = B→A direction. All runs also capture pcaps both ends and run §5 reconciliation.

### 4.1 Packet loss (§24: loss 0/0.1/1/5/20/100%, random, burst, per direction)

| ID | Scenario | Exact command(s) | Expected result |
|----|----------|------------------|-----------------|
| L-00 | Baseline, no impairment | `tc qdisc add dev vethA-M root netem` (and B side) | 0% loss both dirs, status `healthy` |
| L-01 | 0.1% loss A→B | `tc qdisc add dev vethA-M root netem loss 0.1%` | fwd loss ≈0.1% ±0.05pp, rev 0%, `healthy` |
| L-02 | 1% loss A→B | `... loss 1%` | fwd ≈1% ±0.2pp; warning after ≥3 windows (§9) |
| L-03 | 5% loss B→A | `tc qdisc add dev vethB-M root netem loss 5%` | rev ≈5% ±0.5pp; critical (§9); fwd 0% |
| L-04 | 20% loss both dirs | both ifaces `loss 20%` | both ≈20% ±2pp; critical, per-direction evidence |
| L-05 | 100% loss A→B (one-way black hole) | `tc qdisc change dev vethA-M root netem loss 100%` | fwd 100%, rev normal ⇒ directional failure, NOT "unreachable" both ways (§8) |
| L-06 | 100% both (complete loss) | both `loss 100%` | `unreachable` for link, `critical`, alert ≤60s (SLO) |
| L-07 | Random loss 1% uncorrelated | `... loss 1% random` (default) | loss distribution matches pcap, no false burst classification |
| L-08 | Burst loss (25% correlation) | `... loss 5% 25%` | burst_loss_count >0, max_loss_burst matches pcap ±1 |
| L-09 | Severe burst (50% corr, 20%) | `... loss 20% 50%` | burst metrics + micro-outage detection (§1) |
| L-10 | Gemodel loss (realistic burst) | `... loss gemodel 1% 10% 70% 0.1% 0.01%` (p,r,1-h,1-k) | burst/consecutive-loss length tracked vs pcap |

### 4.2 Latency (§24: 20/80/150/300ms, sudden, variable, asymmetric)

| ID | Scenario | Exact command(s) | Expected result |
|----|----------|------------------|-----------------|
| D-01 | 20ms RTT-add both dirs | both `delay 10ms` | RTT ≈ base+20ms within tolerance (§5.2) |
| D-02 | 80ms | both `delay 40ms` | RTT within tolerance |
| D-03 | 150ms | both `delay 75ms` | RTT within tolerance |
| D-04 | 300ms | both `delay 150ms` | RTT within tolerance; no timeout misclassification |
| D-05 | Sudden change 20→300ms mid-run | `tc qdisc change dev vethA-M root netem delay 150ms` at t=30s | latency regression detected (RTT p95 > baseline+50% ⇒ warning §9), timestamp of change ±1 window |
| D-06 | Variable latency (uniform jitter) | both `delay 50ms 20ms` | jitter ≈ expected, RTT avg ≈100ms |
| D-07 | Asymmetric: 10ms A→B, 150ms B→A | A side `delay 5ms`, B side `delay 75ms` | OWD asymmetry reported per direction (with clock confidence); path asymmetry flagged (§1) |

### 4.3 Jitter / PDV (§24: low, moderate, severe, periodic, random; RFC 3393)

| ID | Scenario | Exact command(s) | Expected result |
|----|----------|------------------|-----------------|
| J-01 | Low jitter | both `delay 25ms 2ms distribution normal` | PDV small; `healthy` |
| J-02 | Moderate | both `delay 25ms 10ms distribution normal` | PDV matches RFC 3393 computation vs pcap ±10% |
| J-03 | Severe | both `delay 25ms 50ms distribution normal` | jitter above profile limit ⇒ warning (§9) |
| J-04 | Periodic jitter | harness alternates every 5s: `tc qdisc change ... delay 25ms 2ms` ↔ `delay 25ms 30ms` | periodicity visible in inter-arrival distribution (§5-002); no false loss |
| J-05 | Random heavy (Pareto) | both `delay 25ms 40ms distribution pareto` | p99 RTT/PDV captured; percentile math vs pcap |
| J-06 | Jitter one direction only | A side only `delay 25ms 30ms` | per-direction jitter differs; attribution correct |

### 4.4 Reordering, duplication, corruption (§24)

| ID | Scenario | Exact command(s) | Expected result |
|----|----------|------------------|-----------------|
| R-01 | 25% reorder | `tc qdisc add dev vethA-M root netem delay 10ms reorder 25% 50%` | reordered count + distance match pcap exactly (§25: correctly detected) |
| R-02 | 100% reorder gap 5 | `... delay 10ms reorder 100% 100% gap 5` | reorder distance ≈5; NOT counted as loss |
| R-03 | 1% duplication | `tc qdisc change dev vethA-M root netem duplicate 1%` | duplicate count matches pcap exactly; not counted as loss/gain |
| R-04 | 0.1% corruption | `tc qdisc change dev vethA-M root netem corrupt 0.1%` | corrupted count via payload integrity check; auth tag failures logged (reflector stats §4.2) |
| R-05 | Combined reorder+loss | `... loss 1% delay 10ms reorder 25% 50%` | loss vs reorder disjoint in report; pcap reconciliation passes |

### 4.5 Fragmentation / MTU (§24; §5-007)

| ID | Scenario | Exact command(s) | Expected result |
|----|----------|------------------|-----------------|
| M-01 | Path MTU 1400 | M: `ip link set dev vethA-M mtu 1400` | PMTU discovery reports 1400; no black hole |
| M-02 | MTU black hole (ICMP too-big dropped) | M: `ip link set dev vethB-M mtu 1280` + `nft add rule inet filter forward ip protocol icmp icmpv6 type packet-too-big drop` + `icmp type fragmentation-needed drop` | MTU black hole detected + reported (§25 acceptance), correct per-direction threshold (§5-007 sizes 64…1472) |
| M-03 | Per-direction MTU difference | vethA-M mtu 1500, vethB-M mtu 1400 | per-direction MTU difference reported (§5-007) |
| M-04 | Fragmentation needed + allowed | M mtu 1200, DF off probes | fragmentation behavior recorded; throughput impact noted |
| M-05 | IPv6 MTU | same as M-01/M-02 over IPv6 ULA addressing | IPv6 MTU path works; icmpv6 too-big handling (§5-007) |

### 4.6 DSCP / ECN (§24)

| ID | Scenario | Exact command(s) | Expected result |
|----|----------|------------------|-----------------|
| Q-01 | DSCP remarking mid-path | M: `nft add rule inet filter forward ip dscp cs0 counter ip dscp set cs4` | agent detects DSCP change (sent vs received DSCP field §5-001/002) |
| Q-02 | DSCP-dependent policing (EF class throttled) | M: `tc qdisc add dev vethA-M root handle 1: htb default 10` + `tc class add dev vethA-M parent 1: classid 1:10 htb rate 100mbit` + `tc class add dev vethA-M parent 1: classid 1:20 htb rate 1mbit` + `tc filter add dev vethA-M protocol ip parent 1: prio 1 u32 match ip tos 0xb8 0xfc flowid 1:20` | rate-limit/congestion detection (§1) tied to DSCP class |
| Q-03 | ECN bleaching | M: `nft add rule inet filter forward counter ip ecn set not-ect` | ECN field change detected; no false congestion claim |

### 4.7 Protocol/port blocking & app-layer interference (§24)

| ID | Scenario | Exact command(s) | Expected result |
|----|----------|------------------|-----------------|
| B-01 | ICMP blocked | M: `nft add rule inet filter forward ip protocol icmp drop` | status `probe-blocked` for ICMP only; UDP/TCP still measured; ICMP-blocked ≠ service down (§5-001, §8) |
| B-02 | UDP probe port blocked | `nft add rule inet filter forward udp dport <reflect_port> drop` | UDP-only blocked status; TCP/TLS confirm path alive (§8) |
| B-03 | TCP service port blocked | `nft add rule inet filter forward tcp dport 443 drop` | TCP connect timeout classification; service-failed vs link-down distinguished (§8) |
| B-04 | Single port only, others pass | B-03 + verify 80/udp pass | port-scoped attribution correct |
| B-05 | TLS handshake failure (MITM reset) | M: `nft add rule inet filter forward tcp dport 443 tcp flags syn,ack syn,ack counter reject with tcp reset` (or TLS-terminating middlebox service that resets after ClientHello) | TLS probe error class `handshake-reset`; app-layer interference flagged (§5-004) |
| B-06 | SNI-based blocking | middlebox harness service drops ClientHello with specific SNI, allows others | SNI failure classified per §5-004 |
| B-07 | DNS failure | harness: poison/blackhole DNS in agent namespace (`nft ... udp dport 53 drop`) | DNS test in Profile C reports failure; not conflated with link failure |
| B-08 | WebSocket upgrade blocked | harness middlebox returns 400 on `Upgrade: websocket` | WS failure in Profile B with distinct error class (§5-005) |

### 4.8 Route scenarios (§24; §5-006) — 3-hop topology A→M1→M2→B

| ID | Scenario | Exact command(s) | Expected result |
|----|----------|------------------|-----------------|
| T-01 | Hop change | M1: `ip route replace <B-net> via <M2-alt>` (pre-staged alternate path) | route hash change detected; first differing hop identified (§9 evidence) |
| T-02 | Route flap | harness loops T-01 forward/back every 10s ×6 | each flap = route-change event; no duplicate suppression of distinct changes; alert grouping works (§17) |
| T-03 | ECMP | M1: `ip route add <B-net> nexthop via <M2a> weight 1 nexthop via <M2b> weight 1` | Paris traceroute flow-stable: no false route change per flow (§5-006); ECMP noted |
| T-04 | Silent hop | M2: `nft add rule inet filter forward ip ttl lt 2 drop` + `sysctl -w net.ipv4.icmp_ratemask=...` (suppress time-exceeded) | missing hop recorded; downstream hops still reported; mid-hop silence ≠ path failure (§5-006) |
| T-05 | ASN change | logical: route via alternate "provider" namespace with distinct hop addresses; rDNS/ASN mapping stubbed per path | route-change event with ASN delta in evidence (§16 route timeline) |
| T-06 | Mid-hop latency increase | M2: `tc qdisc add dev <M2-egress> root netem delay 80ms` | hop table shows latency starting at correct hop (§16); end-to-end regression correlated to that hop |

### 4.9 Host failures (§24; §7)

| ID | Scenario | Method | Expected result |
|----|----------|--------|-----------------|
| H-01 | CPU saturation | `stress-ng --cpu 0` in agent netns (cgroup cpu.max unconstrained) | host metrics show saturation; diagnosis correlates; no false "link broken" without evidence (§7) |
| H-02 | Memory pressure | cgroup `memory.max=128M` on agent + memory hog | memory pressure metrics; agent self-health reports; watchdog behavior observed |
| H-03 | Disk pressure | cgroup io limits + fill WAL volume toward quota | WAL quota enforced, oldest-eviction per §15, agent never fills OS filesystem; alert "local queue near capacity" (§17) |
| H-04 | Interface/qdisc drops | agent netns: `tc qdisc add dev vethA root handle 1: netem limit 10` + burst flood | qdisc drops visible in NIC metrics; correlated with measured loss (§7) |
| H-05 | Conntrack full | `sysctl -w net.netfilter.nf_conntrack_max=64` + many TCP connections | conntrack usage metric critical; TCP connect failures attributed to host, not network |
| H-06 | Socket/FD exhaustion | cgroup `pids.max`/ulimit -n 64 + connection churn | FD usage metric; agent degrades gracefully, self-health alert |
| H-07 | Agent crash/restart | `kill -9 $AGENT` mid-run; systemd restart | watchdog restarts; WAL intact; measurement resumes; gap visible in timeline, status `agent-unhealthy` for gap (§8) |
| H-08 | Kernel buffer exhaustion | `sysctl -w net.core.rmem_max=212992` + high-rate UDP flood at reflector | UDP receive errors metric; reflector sheds load within rate limits (§4.2) |

### 4.10 Platform failures (§24; §2, §15, §26)

| ID | Scenario | Method | Expected result |
|----|----------|--------|-----------------|
| P-01 | Control plane down | stop CP deployment in kind/k3s | measurements continue autonomously; last valid config kept (§2, §4.1); status `control-plane-unavailable` surfaced |
| P-02 | Collector down | stop OTel collector(s) | agents spool to WAL; on restore, ordered resend, zero loss, no logical duplicates (§15) |
| P-03 | DB down | stop PostgreSQL in CP lab | CP serves degraded read mode or documented failure; no agent impact; recovery without data corruption |
| P-04 | MQ down | stop NATS/Kafka | ingest gateway backpressure → 4xx/backoff to agents → WAL spool; replay after restore with dedup |
| P-05 | Network partition agent↔CP | `nft add rule inet filter forward ip saddr <agent> ip daddr <cp> drop` (VM lab) | same as P-01/P-02 combined; status `telemetry-delayed` then `control-plane-unavailable` (§8) |
| P-06 | 72h offline agent | partition held 72h (weekly soak; see §9) | ≥72h buffer honored (§15, §25); quota never exceeded; full ordered catch-up; no logical duplicates |
| P-07 | Duplicate batch injection | test harness re-sends captured batch with same idempotency key | server-side dedup: single logical copy (§15) |
| P-08 | Out-of-order batch injection | harness replays batches shuffled | server orders by agent sequence; gap detection; no corruption (§15) |
| P-09 | Clock drift [VM] | chrony stepped offset ±500ms on agent VM | clock offset/uncertainty reported; OWD flagged `invalid-clock-sync`/`low-confidence`, never stored as precise (§3, §25) |
| P-10 | Cert expiration mid-run | short-lived test cert (minutes) | agent rotates before expiry (§10); measurement gap = 0; expiration alert if rotation fails (§17) |
| P-11 | Cert revocation | revoke agent cert in CP | telemetry rejected immediately/within documented window (§25); audit event; dashboard security view shows it |
| P-12 | Invalid config signature | flip bytes in signed config / resign with wrong key | config rejected; last valid config kept (§25); security event logged + alerted (§16 security view) |
| P-13 | Config downgrade | replay older validly-signed config (lower `config_version`) | rejected by monotonic version check (§12 fields, §22 downgrade threat); audited |

### 4.11 Security scenarios (§24; §22; detailed suite in §6 of this doc)

| ID | Scenario | Method | Expected result |
|----|----------|--------|-----------------|
| S-01 | Packet replay at reflector | re-send captured authenticated probe packets | replay window rejects old/dup; counter + log + audit (§4.2, §25) |
| S-02 | Forged certificate | agent presents self-signed / wrong-CA cert | mTLS handshake fails; no telemetry accepted (§25) |
| S-03 | Invalid HMAC/AEAD tag | mutate auth tag in probe packets | reflector silent (no reply); invalid-auth stats logged (§4.2) |
| S-04 | Oversized telemetry | batch > size limit | rejected at gateway with size-limit error; no partial write (§11) |
| S-05 | Excessive jobs | CP-side or forged control stream floods jobs | agent rate/concurrency limits (§11); suspicious-job security event |
| S-06 | Arbitrary target / SSRF | job envelope with peer not in policy / metadata IP 169.254.169.254 | rejected by policy engine (§10-003, §12); audited |
| S-07 | Shell injection via job params | job parameters containing `;`, `$()`, paths | typed-schema validation rejects; nothing executes (§12: free-form forbidden) |
| S-08 | SQL injection via REST query | `' OR 1=1--` in filters/pagination params | parameterized queries; 400 on invalid schema (§11); no data leak |
| S-09 | Cross-tenant access | tenant A token queries tenant B link IDs | per-object authorization denies (§11); audit event |
| S-10 | Stolen session | replay agent session keys after session expiry | expired session rejected (§4.2 auto-expire); audit |
| S-11 | Rate-limit bypass | parallel connections, rotating source ports at gateway/REST | per-identity limits hold regardless of connection count (§11) |
| S-12 | Malicious update artifact | unsigned/modified agent binary in update channel | cosign verify fails; update refused (§21); no unknown-binary execution (§19) |
| S-13 | Amplification probe | small unauthenticated UDP request to reflector | zero response (response ≤ request invariant; unauthenticated ⇒ silent) (§4.2) |

## 5. Accuracy validation methodology (§25)

### 5.1 Reference pcap comparison (loss, reorder, duplication)

Procedure (executed by `tests/netem/bin/pcap-reconcile` on every netem scenario):

1. Start `tcpdump` on both endpoints before probes: `ip netns exec A tcpdump -i vethA -w A.pcap udp port <probe>` and same on B.
2. Run scenario for its duration; stop captures; ensure clock-free reconciliation (sequence numbers, not timestamps, define loss).
3. Reconciler extracts per-direction sequence numbers from probe payloads in both pcaps:
   - `sent_A→B` = sequences seen leaving A; `received_A→B` = sequences seen arriving B; `lost` = sent − received.
   - duplicates = sequences arriving B more than once; reordered = arrivals violating sequence order, distance = displacement.
4. Compare with agent-reported `sent/received/lost`, `loss_percent`, `duplicate_packets`, `reordered_packets` for the same window (±1 batch boundary; windows aligned by sequence ranges, not wall time).

Tolerances (hard gates for §25 acceptance):

| Metric | Tolerance |
|--------|-----------|
| Packet counts (sent/received/lost/dup/reorder/corrupt) | exact match (±0), allowing ±1 batch-boundary packet |
| loss_percent | exact to 2 decimals given count equality |
| max_loss_burst / burst_loss_count | ±1 |
| jitter_ms (RFC 3393) | within ±10% or ±0.5ms, whichever greater, vs pcap-derived PDV |
| RTT percentiles (p50/p95/p99) | within ±1ms or ±2%, whichever greater, vs pcap-timestamp-derived RTT |

### 5.2 RTT reference-tool comparison

- Same netem condition, parallel runs: `bnqo` UDP probe vs `hping3 -S` (TCP) and `ping` (ICMP) at matched rates.
- Acceptance: bnqo RTT avg/p95 within ±1ms or ±2% of the reference tool on the same path/protocol family; deviations documented per scenario in the benchmark report (§25 "RTT within documented tolerance").
- Note: ICMP-vs-UDP RTT differences under middlebox conditions are expected and must be reported as *separate signals*, never averaged (§1, §5-001).

### 5.3 Throughput error budget

- Reference: `iperf3` ephemeral adapter session (§5-008 constraints apply even in lab) on the same netem-conditioned path.
- Acceptance: bnqo scheduled-capacity result within **±5%** of iperf3 for TCP single-stream and UDP fixed-bitrate modes, over rates 1–500 Mbps and loss 0–1%. Outside ±5%: investigate, document, or fix before Phase-5 sign-off.

### 5.4 MTU black-hole detection proof

- Scenario M-02 must demonstrate: (a) probes at sizes ≤1280 succeed, (b) probes >1280 with DF set fail with *no* ICMP too-big received (black hole), (c) agent reports `mtu_blackhole` status with the detected per-direction threshold size and direction, within 3 probe cycles of the discovery run, (d) incident evidence includes the failing size series (§5-007 size ladder 64/128/256/512/1200/1280/1400/1472/near-PMTU).
- Also verify the negative: M-01 (PMTU 1400, ICMP delivered) must classify as normal PMTU discovery, *not* black hole.

## 6. Security test suite & fuzz targets

### 6.1 Threat-model validation tests (§22)

Every §22 threat row has a `Validation Test` field pointing to an executable test. Mapping highlights beyond the §4.11 table: agent spoofing (S-02 + enrollment fingerprint tests), metric poisoning (ingest schema/range validation: negative counts, loss >100%, out-of-window timestamps rejected), result tampering (signature + sequence gaps detected), time manipulation (P-09 + `not_before`/`expires_at` enforcement §12), queue flooding/disk exhaustion (H-03, P-04), API enumeration/brute force (rate-limit + lockout tests on REST), privilege escalation (systemd hardening inspection: `systemd-analyze security bnqo-agent` score asserted in CI, NoNewPrivileges verified), pcap leakage (§18: pcap feature default-off test; enable-path encryption/audit test), insider threat (audit-log completeness test: every sensitive op from §4.3 emits an audit event).

### 6.2 Fuzz targets (`fuzz/`, cargo-fuzz/libfuzzer)

| Target | Input domain | Invariants |
|--------|--------------|------------|
| `fuzz_packet_parse` | arbitrary bytes → probe packet parser | no panic; parse failures → silent drop; never allocate > max payload |
| `fuzz_session_handshake` | mutated handshake messages | no session established without valid HMAC/AEAD; replay window never accepts stale nonce |
| `fuzz_job_envelope` | mutated job protobuf + signature field | unsigned/expired/unknown-type jobs always rejected (§12) |
| `fuzz_config_parse` | mutated signed-config blobs | invalid signature always rejected; valid sig + invalid content → reject, keep last-valid |
| `fuzz_grpc_handlers` | structured protobuf mutation against ingest/control handlers (in-process) | no panic; size/rate limits enforced; no partial batch commit |
| `fuzz_wal_record` | corrupted WAL segments (bit flips, truncation) | reader stops at first bad checksum; never fabricates records; recovery resumes after last good record |

Corpus: seeded from valid captured traffic + minimized crashes, stored in `fuzz/corpus/`, grown nightly. Go services (if any per ADR-0001) run native `go test -fuzz` equivalents.

## 7. Reliability tests (§25)

| Test | Method | Pass criteria |
|------|--------|---------------|
| Kill -9 during WAL write | loop: agent writing at max rate, `kill -9` at random offsets/times ×500 iterations | every committed (ACKed) record intact; recovery scans to last good record; zero committed-record loss (§15, §25) |
| 72h offline soak | P-06 scenario, weekly | ≥72h buffered, quota respected, ordered resend, zero loss/dup (§25) |
| Duplicate/out-of-order batches | P-07/P-08 harness | server-side dedup + reordering correct; idempotent (§15) |
| Config-signature downgrade | P-12/P-13 | rejected; last valid kept; audited (§25) |
| No duplicate job execution | scheduler kill/restart mid-dispatch; job ACK redelivery | exactly-once execution semantics via job_id idempotency (§25) |
| Invalid config never replaces valid | fuzz_config_parse + P-12 in CI | invariant asserted in every failure test touching config |
| Hub outage doesn't stop measurement | P-01/P-05 | probe schedule continuity verified from WAL sequence gaps = 0 during outage |

## 8. Performance benchmarks (§25)

Micro: `criterion` benches for packet encode/decode, HMAC verify, WAL append (targets set in Phase 1; e.g., WAL append p99 <1ms/record).

System-level methodology:

| Target (§25) | Measurement method | Gate |
|--------------|--------------------|------|
| Agent idle CPU <1% | `pidstat -p $AGENT 60 10` during Profile A at default intervals; report avg + p95 over 10 min | avg <1% of one core |
| Agent memory <150MB | `/proc/$AGENT/status` VmRSS sampled 1/min over 24h soak incl. WAL spool under P-02 | p99 RSS <150MB |
| Probe bandwidth limited/configurable | measure egress at agent interface vs configured `max_bandwidth` for each profile | observed ≤ configured +5% burst tolerance |
| Ingest p95 <5s | load generator: 10× initial-scope batch rate; end-to-end latency instrumented from agent `UploadMeasurementBatch` ACK timestamp until the row is queryable via `/v1/links/{id}/measurements` | p95 <5s sustained 1h |
| Dashboard freshness <15s | synthetic probe write with known marker; poll dashboard query API until visible | p95 <15s over 1h (SLO SLI-2) |
| Auto recovery after restart | reboot agent host, CP nodes, full lab; measure time-to-normal | all services healthy <5 min, zero manual steps |

Scale projection (R4 in plan): Phase-5 load test at projected 1,000 agents / 10,000 paths via simulated agents (batch replay at scale) against ingest + storage; publish capacity numbers.

## 9. CI gating

| Suite | Per PR | Nightly | Weekly | Pre-release |
|-------|--------|---------|--------|-------------|
| fmt / clippy / build / unit / integration | ✅ (blocking) | ✅ | ✅ | ✅ |
| E2E smoke (compose) | ✅ (blocking) | ✅ | ✅ | ✅ |
| E2E full (kind/k3s) | — | ✅ | ✅ | ✅ |
| Netem: L-*, D-01…05, R-01, R-03, B-01…03 (tag `pr-blocking`) | ✅ (blocking, ~15 min) | ✅ | ✅ | ✅ |
| Netem: complete §4 matrix | — | ✅ | ✅ | ✅ (100% pass or waived per Phase-5 exit) |
| Security suite §6.1 + S-* | S-01…03, S-07 per PR | ✅ all | ✅ | ✅ + manual review |
| Fuzz | 5 min/target smoke | 1h/target | 8h/target | 24h/target clean |
| Performance | — | CPU/mem/ingest smoke | freshness + recovery | full §8 matrix + scale projection, published report |
| Chaos / platform (P-01…05, P-07, P-08, P-10…13) | — | subset | ✅ all | ✅ all |
| 72h offline soak (P-06) | — | — | ✅ | ✅ (must pass in the release window) |
| Backup/restore + RPO/RTO drill (§26) | — | — | — | ✅ |
| SBOM / SAST / dep-scan / cosign verify | ✅ | ✅ | ✅ | ✅ + reproducible-build check |

Flake policy: any netem/e2e test flaking twice in a week is quarantined with an owner and a 1-week fix SLA; quarantined `pr-blocking` tests must be replaced by an equivalent gate.
