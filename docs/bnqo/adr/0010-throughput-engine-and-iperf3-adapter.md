# ADR-0010: Throughput testing — built-in Rust engine primary, ephemeral iperf3 adapter optional

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §5 FR-MEASURE-008 (3 profiles, modes, iperf3 constraints), §4.2 (no permanent open public iperf3 port), §6 (Profile D scheduled capacity), §22 (throughput-test DoS abuse), §25 (throughput within defined error; throughput limits enforced), §12 (RUN_THROUGHPUT_TEST typed job with approval)

## Context

RFP §5-008 requires three throughput profiles — lightweight capacity sample,
scheduled off-peak capacity test, and on-demand RBAC+approval diagnostic — across
modes: TCP single-stream, TCP limited multi-stream, UDP fixed bitrate, each
direction separately and both simultaneously, at various packet sizes. It permits
iperf3 *only as an adapter* under strict conditions: ephemeral sessions,
random/controlled ports, firewall restricted to the peer, short-lived session
token, max bitrate/duration, cleanup after the run, full audit, auto-stop if
production traffic is harmed. RFP §4.2 additionally forbids any permanent open
public iperf3 port. RFP §1 warns never to rely on iperf3 alone.

The decision: build our own throughput engine, or wrap iperf3 under those
constraints, or both.

## Decision

**Primary: a built-in Rust throughput engine in `bnqo-agent`/`bnqo-reflector`.
Optional: a tightly-constrained ephemeral iperf3 adapter** for cross-validation
and interoperability, disabled by default.

### Built-in engine (default)

- **Transport**: TCP streams over `tokio` (single-stream; limited multi-stream with
  a hard cap, default ≤4) and UDP fixed-bitrate over the BNQO-STAMP session
  machinery (ADR-0002) so UDP capacity tests inherit authentication, replay
  protection, and no-amplification for free.
- **Control**: always launched via `RUN_THROUGHPUT_TEST` signed jobs (ADR-0009)
  carrying `parameters {mode, direction, duration_s ≤ max_duration,
  target_bps ≤ max_bandwidth, streams, packet_size, dscp}` and, for the
  on-demand profile, a verified `approval_id`. The reflector side accepts a
  throughput session only within the job's validity window, bound to the peer's
  registered address, on an ephemeral port negotiated over the authenticated
  session — the port exists only for the run's duration (satisfies "no permanent
  open port" without any firewall choreography).
- **Safety**: token-bucket pacing (never exceed `target_bps`), production-harm
  auto-stop — the engine samples its own host metrics (RFP §7) and the live
  TCP/TLS probe success rates during the run and aborts on production-error
  correlation (RFP §5-008 auto-stop, §6 Profile D); every run audited with
  `approval_id`, actual bps/duration, stop reason.
- **Measurements**: goodput, retransmits during load (TCP_INFO), loss/jitter under
  UDP load, per-direction and simultaneous-bidirectional results — recorded as
  RFP §13 measurement records with `test_type=throughput`.

### Ephemeral iperf3 adapter (optional, off by default)

For cross-validating the built-in engine and for interop with third-party
iperf3 servers, an adapter implementing RFP §5-008 literally:

1. agent spawns `iperf3` client/server per run (no daemon); server binds a random
   high port on the peer-facing interface only;
2. a short-lived iptables/nft rule scoped to the peer's source IP is added before
   bind and removed in a `Drop` guard + post-run cleanup sweep (no permanent
   exposure, RFP §4.2);
3. one-time session token gates the control connection; max bitrate (`-b`),
   max duration (`-t`), and stream count are clamped to the job's signed limits;
4. JSON output parsed into standard measurement records; the adapter is itself a
   typed-job variant, never a config-persistent service.

The adapter exists behind a cargo feature + signed-config flag; production builds
ship without it unless interop is explicitly requested.

## Consequences

Positive: the default path has zero external process management, zero firewall
mutation, and inherits the probe protocol's authentication — the
"throughput-test DoS abuse" threat (RFP §22) is bounded by signed job limits and
agent-side ceilings; one codebase means throughput results use identical record
schemas and clock discipline as all other measurements; iperf3 cross-validation
gives the "throughput within defined error" acceptance criterion (RFP §25) an
independent reference.

Negative: we own throughput-engine correctness (mitigated by the iperf3 adapter as
the calibration reference in lab tests, RFP §24 netem matrix); simultaneous
bidirectional TCP needs careful socket buffer tuning to avoid self-induced loss —
covered by performance tests with documented tolerance (RFP §25); the adapter's
nft/iptables manipulation is the most privileged agent operation — constrained to
a dedicated helper with seccomp-scoped `CAP_NET_ADMIN` granted only when the
feature is enabled, otherwise absent.

## Alternatives considered

- **iperf3 as the only throughput mechanism**: requires the full ephemeral-session
  machinery for every capacity check (including lightweight samples), keeps an
  external binary in the supply chain for a core measurement, and still needs a
  custom control wrapper; RFP §1 explicitly warns against iperf3-only reliance.
  Rejected as primary.
- **Built-in engine only, no adapter**: cleanest supply chain, but loses the
  independent calibration reference and third-party interop that make our
  throughput numbers credible. Rejected (adapter retained, off by default).
- **Speedtest-style HTTP transfers against public endpoints**: violates
  SR-IDENTITY-003 (no arbitrary internet targets) and measures the wrong thing.
  Rejected.
