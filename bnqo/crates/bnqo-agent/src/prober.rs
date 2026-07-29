//! Prober path: per-link authenticated probes into measurement windows
//! (PROTOCOL.md §5 sender behavior, §6 math via bnqo-measure), plus ICMP
//! cycles and service-target probes per the link profile.

use std::net::SocketAddr;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use bnqo_measure::host::HostMetrics;
use bnqo_measure::icmp;
use bnqo_measure::service::{self, ServiceTarget};
use bnqo_measure::window::WindowAggregator;
use bnqo_proto::crypto::{self, DirectionKeys};
use bnqo_proto::packet::{ntp64_from_unix_nanos, Flags, Header};
use rand::Rng;
use tokio::net::UdpSocket;
use tokio::sync::mpsc;

use crate::model::{
    IcmpRec, LinkConfig, MeasurementRec, ServiceProbeRec, TelemetryItem,
};

/// Shared host clock telemetry, refreshed by the host sampler task.
pub type SharedHost = Arc<RwLock<HostMetrics>>;

fn unix_nanos_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

/// Run one link's probing until `shutdown` fires. Reflections arrive on
/// `rx` from the socket demux task.
pub async fn run_link(
    link: LinkConfig,
    socket: Arc<UdpSocket>,
    mut rx: mpsc::Receiver<Vec<u8>>,
    sink: mpsc::Sender<TelemetryItem>,
    host: SharedHost,
    mut shutdown: tokio::sync::watch::Receiver<bool>,
) {
    let Ok(peer_ip) = link.peer.address.parse::<std::net::IpAddr>() else {
        tracing::warn!(link_id = link.link_id, "unparseable peer address, link idle");
        return;
    };
    let peer = SocketAddr::new(peer_ip, link.peer.port);
    let Ok(seed) = hex::decode(&link.session_seed) else {
        tracing::warn!(link_id = link.link_id, "bad session_seed hex, link idle");
        return;
    };
    let seed: [u8; 32] = match seed.try_into() {
        Ok(s) => s,
        Err(_) => {
            tracing::warn!(link_id = link.link_id, "session_seed must be 32 bytes, link idle");
            return;
        }
    };
    let keys = DirectionKeys::derive(&seed);
    let we_are_a = link.direction == "a_to_b";
    let (recv_key, recv_salt) = keys.sending(!we_are_a);

    let packet_size = link
        .profile
        .packet_size
        .clamp(bnqo_proto::MIN_PACKET_SIZE, bnqo_proto::MAX_PACKET_SIZE);
    let interval = Duration::from_millis(link.profile.interval_ms.max(10));
    let window = Duration::from_secs(link.profile.window_sec.max(1));

    let mut agg = WindowAggregator::new();
    let mut seq: u32 = rand::thread_rng().gen();
    let mut window_start = Instant::now();
    let mut window_start_iso = crate::model::utc_now_iso();
    let mut last_icmp = Instant::now() - Duration::from_secs(3600);
    let mut last_service = Instant::now() - Duration::from_secs(3600);

    loop {
        // Jittered send interval: uniform [0.5T, 1.5T] (§5).
        let jitter_factor = rand::thread_rng().gen_range(0.5..1.5);
        let sleep = interval.mul_f64(jitter_factor);

        tokio::select! {
            _ = tokio::time::sleep(sleep) => {
                seq = seq.wrapping_add(1);
                let pkt = build_probe(
                    &link, &keys, we_are_a, seq, packet_size,
                    clock_quality_of(&host),
                );
                let ts = unix_nanos_now();
                if let Err(e) = socket.send_to(&pkt, peer).await {
                    tracing::debug!(link_id = link.link_id, error = %e, "probe send failed");
                } else {
                    let h = Header::decode(&pkt).unwrap();
                    agg.on_send(seq, h.nonce, ntp64_from_unix_nanos(ts));
                }
            }
            Some(datagram) = rx.recv() => {
                handle_reflection(&mut agg, &datagram, recv_key, recv_salt);
            }
            _ = shutdown.changed() => {
                if *shutdown.borrow() {
                    break;
                }
            }
        }

        // Window close.
        if window_start.elapsed() >= window {
            let stats = agg.finish_window(&host_clock_estimate(&host));
            let rec = MeasurementRec {
                link_id: link.link_id,
                direction: link.direction.clone(),
                window_start: window_start_iso.clone(),
                window_end: crate::model::utc_now_iso(),
                sent: stats.sent,
                received: stats.received,
                loss_pct: stats.loss_pct,
                rtt_min_ms: stats.rtt_min_ms,
                rtt_avg_ms: stats.rtt_avg_ms,
                rtt_p95_ms: stats.rtt_p95_ms,
                rtt_max_ms: stats.rtt_max_ms,
                owd_ms: stats.owd_ms,
                clock_quality: stats.clock_quality.as_str().to_string(),
                jitter_ms: stats.jitter_ms,
                reordered: stats.reordered,
                duplicated: stats.duplicated,
                corrupted: stats.corrupted,
                burst_max: stats.burst_max,
            };
            if sink.send(TelemetryItem::Measurement(rec)).await.is_err() {
                break;
            }
            window_start = Instant::now();
            window_start_iso = crate::model::utc_now_iso();
        }

        // ICMP cycle per profile.
        if link.profile.icmp_enabled
            && last_icmp.elapsed() >= Duration::from_secs(link.profile.icmp_interval_sec)
        {
            last_icmp = Instant::now();
            let result = icmp::ping_cycle(
                peer_ip,
                link.profile.icmp_count,
                Duration::from_millis(200),
                Duration::from_secs(1),
            )
            .await;
            let _ = sink
                .send(TelemetryItem::Icmp(IcmpRec {
                    link_id: link.link_id,
                    direction: link.direction.clone(),
                    sent: result.sent,
                    received: result.received,
                    loss_pct: result.loss_pct,
                    rtt_avg_ms: result.rtt_avg_ms,
                    rtt_p95_ms: result.rtt_p95_ms,
                    error_class: result.error_class,
                }))
                .await;
        }

        // Service targets per profile.
        if !link.profile.service_targets.is_empty()
            && last_service.elapsed()
                >= Duration::from_secs(
                    link.profile
                        .service_targets
                        .iter()
                        .map(|t| t.interval_sec)
                        .min()
                        .unwrap_or(30),
                )
        {
            last_service = Instant::now();
            for t in &link.profile.service_targets {
                let target = ServiceTarget {
                    name: t.name.clone(),
                    host: t.host.clone(),
                    port: t.port,
                    tls: t.tls,
                    http_get: t.tls,
                };
                let r = service::probe(&target, Duration::from_secs(5)).await;
                let _ = sink
                    .send(TelemetryItem::ServiceProbe(ServiceProbeRec {
                        link_id: link.link_id,
                        target_name: t.name.clone(),
                        ok: r.ok,
                        tcp_ms: r.tcp_ms,
                        tls_ms: r.tls_ms,
                        http_status: r.http_status,
                        error_class: r.error_class,
                    }))
                    .await;
            }
        }
    }
}

fn clock_quality_of(host: &SharedHost) -> bnqo_proto::ClockQuality {
    host_clock_estimate(host).quality()
}

fn host_clock_estimate(host: &SharedHost) -> bnqo_measure::window::ClockEstimate {
    host.read()
        .map(|h| h.clock_estimate(5.0))
        .unwrap_or_default()
}

/// Build a forward probe packet (§2.2 layout, §2.5 AEAD, §2.6 padding).
pub fn build_probe(
    link: &LinkConfig,
    keys: &DirectionKeys,
    we_are_a: bool,
    seq: u32,
    packet_size: usize,
    clock_quality: bnqo_proto::ClockQuality,
) -> Vec<u8> {
    let header = Header {
        flags: Flags::clock_quality_to_bits(clock_quality),
        // Phase 1: test_id/session_id come from the link id; key_epoch 0.
        test_id: link.link_id as u32,
        session_id: link.link_id,
        sequence_number: seq,
        sender_timestamp: ntp64_from_unix_nanos(unix_nanos_now()),
        receive_timestamp: 0,
        reflector_turnaround_us: 0,
        nonce: rand::thread_rng().gen(),
        payload_length: packet_size as u16,
        key_epoch: 0,
    };
    let mut buf = vec![0u8; packet_size];
    header.encode(&mut buf);
    if packet_size > bnqo_proto::MIN_PACKET_SIZE {
        crypto::fill_padding(
            &keys.verify_key,
            seq,
            &mut buf[bnqo_proto::MIN_PACKET_SIZE..],
        );
    }
    let (key, salt) = keys.sending(we_are_a);
    crypto::seal_packet(key, salt, &mut buf);
    buf
}

/// Validate a candidate reflection (§5): AEAD tag with the reverse key,
/// REFLECTED bit, then feed the aggregator.
pub fn handle_reflection(
    agg: &mut WindowAggregator,
    datagram: &[u8],
    recv_key: &[u8; 32],
    recv_salt: &[u8; 12],
) {
    let Ok(header) = Header::decode(datagram) else {
        agg.count_corrupted();
        return;
    };
    if !header.is_reflected() {
        return; // forward packets go to the reflector path
    }
    if !crypto::open_packet(recv_key, recv_salt, datagram) {
        agg.count_corrupted();
        return;
    }
    let t4 = ntp64_from_unix_nanos(unix_nanos_now());
    agg.on_receive(
        header.sequence_number,
        header.nonce,
        header.receive_timestamp,
        header.reflector_turnaround_us,
        t4,
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use bnqo_proto::packet::MIN_PACKET_SIZE;

    fn test_link() -> LinkConfig {
        LinkConfig {
            link_id: 3,
            name: "t".into(),
            peer: crate::model::PeerInfo {
                name: "p".into(),
                address: "127.0.0.1".into(),
                port: 44818,
            },
            direction: "a_to_b".into(),
            session_seed: hex::encode([5u8; 32]),
            profile: Default::default(),
        }
    }

    #[test]
    fn probe_packet_wellformed_and_reflectable() {
        let link = test_link();
        let keys = DirectionKeys::derive(&[5u8; 32]);
        let pkt = build_probe(&link, &keys, true, 42, 256, bnqo_proto::ClockQuality::Good);
        assert_eq!(pkt.len(), 256);
        let h = Header::decode(&pkt).unwrap();
        assert_eq!(h.session_id, 3);
        assert_eq!(h.sequence_number, 42);
        assert!(!h.is_reflected());
        assert_eq!(h.clock_quality(), bnqo_proto::ClockQuality::Good);
        let (k, s) = keys.sending(true);
        assert!(crypto::open_packet(k, s, &pkt));
        // Padding is keystream-filled, not zero.
        assert!(pkt[MIN_PACKET_SIZE..].iter().any(|&b| b != 0));
    }

    #[test]
    fn reflection_feeds_aggregator() {
        let link = test_link();
        let keys = DirectionKeys::derive(&[5u8; 32]);
        let mut agg = WindowAggregator::new();

        let pkt = build_probe(&link, &keys, true, 1, MIN_PACKET_SIZE, bnqo_proto::ClockQuality::Unknown);
        let h = Header::decode(&pkt).unwrap();
        agg.on_send(1, h.nonce, h.sender_timestamp);

        // Simulate reflector: flip flag, stamp T2/turnaround, re-tag b_to_a.
        let mut refl = pkt.clone();
        let mut rh = h;
        rh.flags |= Flags::REFLECTED;
        rh.receive_timestamp = ntp64_from_unix_nanos(unix_nanos_now());
        rh.reflector_turnaround_us = 100;
        rh.encode(&mut refl);
        let (k, s) = keys.sending(false);
        crypto::seal_packet(k, s, &mut refl);

        handle_reflection(&mut agg, &refl, k, s);
        let stats = agg.finish_window(&Default::default());
        assert_eq!(stats.sent, 1);
        assert_eq!(stats.received, 1);
        assert_eq!(stats.loss_pct, 0.0);
    }

    #[test]
    fn forged_reflection_counted_corrupted() {
        let link = test_link();
        let keys = DirectionKeys::derive(&[5u8; 32]);
        let wrong = DirectionKeys::derive(&[6u8; 32]);
        let mut agg = WindowAggregator::new();

        let pkt = build_probe(&link, &wrong, false, 1, MIN_PACKET_SIZE, bnqo_proto::ClockQuality::Unknown);
        let mut forged = pkt;
        forged[3] |= Flags::REFLECTED;
        let (wk, ws) = wrong.sending(false);
        crypto::seal_packet(wk, ws, &mut forged);

        let (k, s) = keys.sending(false);
        handle_reflection(&mut agg, &forged, k, s);
        let stats = agg.finish_window(&Default::default());
        assert_eq!(stats.received, 0);
        assert_eq!(stats.corrupted, 1);
    }
}
