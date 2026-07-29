//! Reflector path (PROTOCOL.md §4): validation order, silent drop, replay
//! window with duplicate cap, same-size reflection, per-peer rate limit.
//!
//! Everything here is platform-independent: the network loop feeds
//! datagrams in and sends back what `handle_datagram` returns.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::time::{Duration, Instant};

use bnqo_proto::crypto::{self, DirectionKeys};
use bnqo_proto::packet::{
    ntp64_from_unix_nanos, unix_nanos_from_ntp64, ClockQuality, Flags, Header,
    MIN_PACKET_SIZE,
};
use bnqo_proto::replay::{ReplayDecision, ReplayWindow};

use crate::model::LinkConfig;

/// Per-session counters (§4.5). Read by the stats exporter (Phase-2
/// telemetry); kept public for that consumer.
#[allow(dead_code)]
#[derive(Debug, Default, Clone, Copy)]
pub struct ReflectorStats {
    pub drop_malformed: u64,
    pub drop_bad_version: u64,
    pub drop_unknown_session: u64,
    pub drop_rate_limited: u64,
    pub drop_auth_fail: u64,
    pub drop_replay_stale: u64,
    pub drop_stale_timestamp: u64,
    pub dup_reflected: u64,
    pub dup_suppressed: u64,
    pub rx_ok: u64,
    pub tx_ok: u64,
}

/// Token bucket rate limiter (§4.3 step 4).
#[derive(Debug, Clone)]
pub struct TokenBucket {
    rate_per_sec: f64,
    capacity: f64,
    tokens: f64,
    last: Instant,
}

impl TokenBucket {
    pub fn new(rate_per_sec: f64) -> Self {
        TokenBucket {
            rate_per_sec,
            capacity: 2.0 * rate_per_sec,
            tokens: 2.0 * rate_per_sec,
            last: Instant::now(),
        }
    }

    pub fn allow(&mut self) -> bool {
        let now = Instant::now();
        let elapsed = now.duration_since(self.last).as_secs_f64();
        self.last = now;
        self.tokens = (self.tokens + elapsed * self.rate_per_sec).min(self.capacity);
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            true
        } else {
            false
        }
    }
}

pub struct ReflectorSession {
    #[allow(dead_code)]
    pub link_id: u64,
    /// Our sending direction on this link (true = a_to_b). The peer sends in
    /// the opposite direction, so we verify with that key and reflect with
    /// ours.
    pub we_are_a: bool,
    pub keys: DirectionKeys,
    pub replay: ReplayWindow,
    pub bucket: TokenBucket,
    pub mtu_bucket: TokenBucket,
    pub stats: ReflectorStats,
    pub clock_quality: ClockQuality,
}

impl ReflectorSession {
    pub fn from_link(link: &LinkConfig, rate_pps: f64, clock_quality: ClockQuality) -> Option<Self> {
        let seed_hex = &link.session_seed;
        let seed_bytes = hex::decode(seed_hex).ok()?;
        let seed: [u8; 32] = seed_bytes.try_into().ok()?;
        Some(ReflectorSession {
            link_id: link.link_id,
            we_are_a: link.direction == "a_to_b",
            keys: DirectionKeys::derive(&seed),
            replay: ReplayWindow::new(),
            bucket: TokenBucket::new(rate_pps),
            mtu_bucket: TokenBucket::new(10.0),
            stats: ReflectorStats::default(),
            clock_quality,
        })
    }
}

/// Sessions keyed by (session_id, peer socket address) — §4.1.
/// Phase 1: session_id == link_id (see bnqo/README.md deviations).
pub type SessionTable = HashMap<(u64, SocketAddr), ReflectorSession>;

pub fn build_session_table(
    links: &[LinkConfig],
    bind_port_matches: u16,
    rate_pps: f64,
    clock_quality: ClockQuality,
) -> SessionTable {
    let mut table = HashMap::new();
    for link in links {
        let Ok(ip) = link.peer.address.parse::<std::net::IpAddr>() else {
            tracing::warn!(link_id = link.link_id, addr = %link.peer.address, "peer address unparseable, skipping reflector session");
            continue;
        };
        let peer = SocketAddr::new(ip, link.peer.port);
        let _ = bind_port_matches; // peer binds its own configured port
        if let Some(session) = ReflectorSession::from_link(link, rate_pps, clock_quality) {
            table.insert((link.link_id, peer), session);
        }
    }
    table
}

pub enum ReflectOutcome {
    /// Silent drop (§4.5): zero bytes emitted.
    Drop,
    /// Send these bytes back to the source. Same length as the request.
    Reflect(Vec<u8>),
}

/// Apply the §4.3 validation order to one inbound datagram.
///
/// - `now_unix_ns`: wall clock for T2 stamping and the timestamp sanity check.
/// - `max_skew`: §4.3 step 6 bound (default 300 s).
///
/// The returned reflection is fully sealed; the caller only has to stamp
/// nothing further — turnaround is approximated as the time spent inside
/// this function, which is the honest single-host duration available here.
pub fn handle_datagram(
    table: &mut SessionTable,
    datagram: &[u8],
    src: SocketAddr,
    now_unix_ns: u64,
    max_skew: Duration,
) -> ReflectOutcome {
    let started = Instant::now();

    // Steps 1–2: length/structure, magic/version/reserved flags.
    let header = match Header::decode(datagram) {
        Ok(h) => h,
        Err(e) => {
            use bnqo_proto::packet::PacketError::*;
            match e {
                BadMagic(_) | BadVersion(_) | ReservedFlags(_) => {
                    bump_global(table, src, |s| s.drop_bad_version += 1)
                }
                _ => bump_global(table, src, |s| s.drop_malformed += 1),
            }
            return ReflectOutcome::Drop;
        }
    };

    // Reflected packets belong to the sender path, never the reflector.
    if header.is_reflected() {
        return ReflectOutcome::Drop;
    }

    // Step 3: session lookup (session_id, src_ip, src_port).
    let Some(session) = table.get_mut(&(header.session_id, src)) else {
        return ReflectOutcome::Drop; // unknown sessions are silent by design
    };

    // Step 4: rate limit (MTU suite gets its own tighter bucket).
    let mtu = header.flags & Flags::MTU_PROBE != 0;
    let allowed = if mtu {
        session.mtu_bucket.allow()
    } else {
        session.bucket.allow()
    };
    if !allowed {
        session.stats.drop_rate_limited += 1;
        return ReflectOutcome::Drop;
    }

    // Step 5: AEAD verify with the peer's sending-direction key.
    let (verify_key, verify_salt) = session.keys.sending(!session.we_are_a);
    if !crypto::open_packet(verify_key, verify_salt, datagram) {
        session.stats.drop_auth_fail += 1;
        return ReflectOutcome::Drop;
    }

    // Step 6: timestamp sanity |now - sender_timestamp| <= max_skew.
    let skew_ms = (now_unix_ns as i128 - unix_nanos_from_ntp64(header.sender_timestamp) as i128)
        .abs() as f64
        / 1.0e6;
    if skew_ms > max_skew.as_secs_f64() * 1000.0 {
        session.stats.drop_stale_timestamp += 1;
        return ReflectOutcome::Drop;
    }

    // Step 7: replay window (§4.2) with the duplicate cap (§4.4).
    match session.replay.check(header.sequence_number) {
        ReplayDecision::Stale => {
            session.stats.drop_replay_stale += 1;
            return ReflectOutcome::Drop;
        }
        ReplayDecision::Duplicate { allowed: false } => {
            session.stats.dup_suppressed += 1;
            return ReflectOutcome::Drop;
        }
        ReplayDecision::Duplicate { allowed: true } => {
            session.stats.dup_reflected += 1;
        }
        ReplayDecision::New | ReplayDecision::Late => {}
    }
    session.stats.rx_ok += 1;

    // Step 8: reflect — copy buffer, set REFLECTED, stamp T2 + turnaround,
    // re-tag with our sending-direction key. Same length in, same out.
    let mut out = datagram.to_vec();
    let mut rh = header;
    rh.flags |= Flags::REFLECTED;
    rh.flags = (rh.flags & !Flags::CLOCK_QUALITY_MASK)
        | Flags::clock_quality_to_bits(session.clock_quality);
    rh.receive_timestamp = ntp64_from_unix_nanos(now_unix_ns);
    let turnaround_us = (started.elapsed().as_micros().min(u32::MAX as u128)) as u32;
    rh.reflector_turnaround_us = turnaround_us;
    rh.encode(&mut out);
    let (send_key, send_salt) = session.keys.sending(session.we_are_a);
    crypto::seal_packet(send_key, send_salt, &mut out);
    session.stats.tx_ok += 1;
    ReflectOutcome::Reflect(out)
}

/// Attribute structural-drop counters to a session when we can find one;
/// otherwise they are dropped silently with no session to charge (§4.5
/// counters are best-effort for unknown tuples).
fn bump_global(table: &mut SessionTable, src: SocketAddr, f: impl Fn(&mut ReflectorStats)) {
    if let Some((_, s)) = table.iter_mut().find(|((_, peer), _)| *peer == src) {
        f(&mut s.stats);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bnqo_proto::crypto::DirectionKeys;

    fn link() -> LinkConfig {
        LinkConfig {
            link_id: 7,
            name: "IR-DE".into(),
            peer: crate::model::PeerInfo {
                name: "ir-tb-1".into(),
                address: "198.51.100.5".into(),
                port: 44818,
            },
            direction: "b_to_a".into(), // we are B
            session_seed: hex::encode([1u8; 32]),
            profile: Default::default(),
        }
    }

    fn peer() -> SocketAddr {
        "198.51.100.5:44818".parse().unwrap()
    }

    fn now_ns() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos() as u64
    }

    fn make_probe(keys: &DirectionKeys, a_to_b: bool, seq: u32, nonce: u32, ts_ns: u64) -> Vec<u8> {
        let h = Header {
            flags: 0,
            test_id: 7,
            session_id: 7,
            sequence_number: seq,
            sender_timestamp: ntp64_from_unix_nanos(ts_ns),
            receive_timestamp: 0,
            reflector_turnaround_us: 0,
            nonce,
            payload_length: MIN_PACKET_SIZE as u16,
            key_epoch: 0,
        };
        let mut buf = vec![0u8; MIN_PACKET_SIZE];
        h.encode(&mut buf);
        let (k, s) = keys.sending(a_to_b);
        crypto::seal_packet(k, s, &mut buf);
        buf
    }

    #[test]
    fn full_validation_order_and_reflection() {
        let l = link();
        let keys = DirectionKeys::derive(&[1u8; 32]);
        let mut table = build_session_table(&[l], 44818, 100.0, ClockQuality::Good);
        let now = now_ns();

        // Peer is A (we are B): peer sends a_to_b.
        let probe = make_probe(&keys, true, 1, 0xAAAA, now);
        let out = handle_datagram(&mut table, &probe, peer(), now, Duration::from_secs(300));
        let ReflectOutcome::Reflect(refl) = out else {
            panic!("expected reflection");
        };
        assert_eq!(refl.len(), probe.len()); // same size in/out
        let rh = Header::decode(&refl).unwrap();
        assert!(rh.is_reflected());
        assert_eq!(rh.sequence_number, 1);
        assert_eq!(rh.nonce, 0xAAAA); // echoed verbatim
        assert_eq!(rh.sender_timestamp, ntp64_from_unix_nanos(now)); // echoed
        assert_ne!(rh.receive_timestamp, 0); // T2 stamped
        assert_eq!(rh.clock_quality(), ClockQuality::Good);
        // Verifiable with the b_to_a key (our sending direction).
        let (k, s) = keys.sending(false);
        assert!(crypto::open_packet(k, s, &refl));
        // And NOT with the a_to_b key (key separation, §2.5).
        let (k2, s2) = keys.sending(true);
        assert!(!crypto::open_packet(k2, s2, &refl));
    }

    #[test]
    fn silent_drop_matrix() {
        let l = link();
        let keys = DirectionKeys::derive(&[1u8; 32]);
        let now = now_ns();

        // 1. Malformed: too short.
        let mut table = build_session_table(&[l.clone()], 44818, 100.0, ClockQuality::Unknown);
        assert!(matches!(
            handle_datagram(&mut table, &[0u8; 10], peer(), now, Duration::from_secs(300)),
            ReflectOutcome::Drop
        ));

        // 2. Bad magic.
        let mut bad = make_probe(&keys, true, 1, 1, now);
        bad[0] = 0xFF;
        assert!(matches!(
            handle_datagram(&mut table, &bad, peer(), now, Duration::from_secs(300)),
            ReflectOutcome::Drop
        ));

        // 3. Unknown session (wrong source tuple).
        let probe = make_probe(&keys, true, 1, 2, now);
        let stranger: SocketAddr = "203.0.113.9:44818".parse().unwrap();
        assert!(matches!(
            handle_datagram(&mut table, &probe, stranger, now, Duration::from_secs(300)),
            ReflectOutcome::Drop
        ));

        // 5. Auth failure (wrong key).
        let wrong = DirectionKeys::derive(&[9u8; 32]);
        let forged = make_probe(&wrong, true, 1, 3, now);
        assert!(matches!(
            handle_datagram(&mut table, &forged, peer(), now, Duration::from_secs(300)),
            ReflectOutcome::Drop
        ));
        assert_eq!(
            table[&(7, peer())].stats.drop_auth_fail,
            1
        );

        // 6. Stale timestamp (> 300 s skew).
        let old = make_probe(&keys, true, 1, 4, now - 400_000_000_000);
        assert!(matches!(
            handle_datagram(&mut table, &old, peer(), now, Duration::from_secs(300)),
            ReflectOutcome::Drop
        ));
        assert_eq!(table[&(7, peer())].stats.drop_stale_timestamp, 1);
    }

    #[test]
    fn replay_duplicate_cap_and_stale() {
        let l = link();
        let keys = DirectionKeys::derive(&[1u8; 32]);
        let mut table = build_session_table(&[l], 44818, 1000.0, ClockQuality::Unknown);
        let now = now_ns();

        let probe = make_probe(&keys, true, 5, 0xBEEF, now);
        assert!(matches!(
            handle_datagram(&mut table, &probe, peer(), now, Duration::from_secs(300)),
            ReflectOutcome::Reflect(_)
        ));
        // Duplicates: reflected while under the cap of 8 total reflections.
        let mut reflected = 1;
        let mut suppressed = 0;
        for _ in 0..20 {
            match handle_datagram(&mut table, &probe, peer(), now, Duration::from_secs(300)) {
                ReflectOutcome::Reflect(_) => reflected += 1,
                ReflectOutcome::Drop => suppressed += 1,
            }
        }
        assert_eq!(reflected, 8);
        assert_eq!(suppressed, 13);
        let s = &table[&(7, peer())].stats;
        assert_eq!(s.dup_reflected, 7);
        assert_eq!(s.dup_suppressed, 13);

        // Stale: seq far behind the window.
        let stale = make_probe(&keys, true, 5 + 4096 + 1, 1, now);
        assert!(matches!(
            handle_datagram(&mut table, &stale, peer(), now, Duration::from_secs(300)),
            ReflectOutcome::Reflect(_)
        ));
        let old_probe = make_probe(&keys, true, 5, 0x1234, now); // new nonce, old seq
        // seq 5 is now H - 4097 -> beyond the window -> stale drop
        assert!(matches!(
            handle_datagram(&mut table, &old_probe, peer(), now, Duration::from_secs(300)),
            ReflectOutcome::Drop
        ));
    }

    #[test]
    fn rate_limit_drops_excess() {
        let l = link();
        let keys = DirectionKeys::derive(&[1u8; 32]);
        // 1 pps, burst 2: the third packet in a burst must be dropped.
        let mut table = build_session_table(&[l], 44818, 1.0, ClockQuality::Unknown);
        let now = now_ns();
        let mut outcomes = Vec::new();
        for seq in 1..=5u32 {
            let p = make_probe(&keys, true, seq, seq, now);
            outcomes.push(matches!(
                handle_datagram(&mut table, &p, peer(), now, Duration::from_secs(300)),
                ReflectOutcome::Reflect(_)
            ));
        }
        assert_eq!(
            outcomes,
            vec![true, true, false, false, false],
            "burst of 2 allowed, rest rate-limited"
        );
        assert_eq!(table[&(7, peer())].stats.drop_rate_limited, 3);
    }
}
