//! ICMP echo prober.
//!
//! Linux: prefers an unprivileged `SOCK_DGRAM/IPPROTO_ICMP` ping socket
//! (works when net.ipv4.ping_group_range allows), falls back to
//! `SOCK_RAW` (needs CAP_NET_RAW). Other platforms: stub that reports
//! `probe-blocked`, keeping host tests compilable and green.

use std::net::IpAddr;
use std::time::Duration;

#[cfg(any(target_os = "linux", test))]
use crate::window::percentile_nearest_rank;

#[derive(Debug, Clone, serde::Serialize)]
pub struct IcmpCycleResult {
    pub sent: u32,
    pub received: u32,
    pub loss_pct: f64,
    pub rtt_avg_ms: Option<f64>,
    pub rtt_p95_ms: Option<f64>,
    /// e.g. "probe-blocked" (no permission / unsupported platform),
    /// "resolve-failed". `None` when the cycle ran.
    pub error_class: Option<String>,
}

impl IcmpCycleResult {
    pub fn blocked(reason: &str) -> Self {
        IcmpCycleResult {
            sent: 0,
            received: 0,
            loss_pct: 0.0,
            rtt_avg_ms: None,
            rtt_p95_ms: None,
            error_class: Some(reason.to_string()),
        }
    }
}

#[cfg(any(target_os = "linux", test))]
fn summarize(rtts_ms: Vec<f64>, sent: u32) -> IcmpCycleResult {
    let received = rtts_ms.len() as u32;
    let loss_pct = if sent == 0 {
        0.0
    } else {
        ((100.0 * (sent - received) as f64 / sent as f64) * 100.0).round() / 100.0
    };
    let mut sorted = rtts_ms;
    sorted.sort_by(f64::total_cmp);
    let (avg, p95) = if sorted.is_empty() {
        (None, None)
    } else {
        let a = sorted.iter().sum::<f64>() / sorted.len() as f64;
        (
            Some((a * 100.0).round() / 100.0),
            Some(percentile_nearest_rank(&sorted, 95.0)),
        )
    };
    IcmpCycleResult {
        sent,
        received,
        loss_pct,
        rtt_avg_ms: avg,
        rtt_p95_ms: p95,
        error_class: None,
    }
}

/// Run one ICMP cycle: `count` echo requests spaced `interval`, each with
/// `timeout`. Async wrapper — the socket work is blocking, so it runs on the
/// blocking thread pool.
pub async fn ping_cycle(
    host: IpAddr,
    count: u32,
    interval: Duration,
    timeout: Duration,
) -> IcmpCycleResult {
    tokio::task::spawn_blocking(move || ping_cycle_blocking(host, count, interval, timeout))
        .await
        .unwrap_or_else(|_| IcmpCycleResult::blocked("probe-blocked"))
}

#[cfg(target_os = "linux")]
pub fn ping_cycle_blocking(
    host: IpAddr,
    count: u32,
    interval: Duration,
    timeout: Duration,
) -> IcmpCycleResult {
    linux::ping_cycle(host, count, interval, timeout)
}

#[cfg(not(target_os = "linux"))]
pub fn ping_cycle_blocking(
    _host: IpAddr,
    _count: u32,
    _interval: Duration,
    _timeout: Duration,
) -> IcmpCycleResult {
    IcmpCycleResult::blocked("probe-blocked")
}

/// Internet checksum (RFC 1071) — needed for the raw-socket path and
/// unit-tested on all platforms.
pub fn internet_checksum(data: &[u8]) -> u16 {
    let mut sum: u32 = 0;
    let mut chunks = data.chunks_exact(2);
    for c in &mut chunks {
        sum += u16::from_be_bytes([c[0], c[1]]) as u32;
    }
    let rem = chunks.remainder();
    if let Some(&b) = rem.first() {
        sum += (b as u32) << 8;
    }
    while sum >> 16 != 0 {
        sum = (sum & 0xFFFF) + (sum >> 16);
    }
    !(sum as u16)
}

/// Build an ICMPv4 echo request (type 8) with the checksum filled in.
pub fn build_echo_request(ident: u16, seq: u16, payload: &[u8]) -> Vec<u8> {
    let mut pkt = Vec::with_capacity(8 + payload.len());
    pkt.extend_from_slice(&[8, 0, 0, 0]); // type, code, checksum (0 for now)
    pkt.extend_from_slice(&ident.to_be_bytes());
    pkt.extend_from_slice(&seq.to_be_bytes());
    pkt.extend_from_slice(payload);
    let csum = internet_checksum(&pkt);
    pkt[2..4].copy_from_slice(&csum.to_be_bytes());
    pkt
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use socket2::{Domain, Protocol, SockAddr, Socket, Type};
    use std::io::ErrorKind;
    use std::net::SocketAddr;
    use std::time::Instant;

    pub fn ping_cycle(
        host: IpAddr,
        count: u32,
        interval: Duration,
        timeout: Duration,
    ) -> IcmpCycleResult {
        let IpAddr::V4(v4) = host else {
            // v6 ping sockets need a different protocol constant; keep the
            // Phase-1 prober IPv4 and report blocked for v6 targets.
            return IcmpCycleResult::blocked("probe-blocked");
        };
        let socket = match open_ping_socket() {
            Ok(s) => s,
            Err(_) => return IcmpCycleResult::blocked("probe-blocked"),
        };
        socket.set_read_timeout(Some(Duration::from_millis(200))).ok();
        let ident = (std::process::id() & 0xFFFF) as u16;
        let dest = SockAddr::from(SocketAddr::new(host, 0));
        let payload = [0x42u8; 24];
        let mut rtts = Vec::new();
        let deadline = Instant::now() + Duration::from_millis(200) * count + timeout * count
            + Duration::from_secs(2);

        for seq in 0..count {
            let pkt = build_echo_request(ident, seq as u16, &payload);
            let sent_at = Instant::now();
            if socket.send_to(&pkt, &dest).is_err() {
                continue;
            }
            // Wait for the matching reply, draining unrelated datagrams.
            loop {
                if Instant::now() > deadline || sent_at.elapsed() > timeout {
                    break;
                }
                let mut buf = [std::mem::MaybeUninit::<u8>::uninit(); 1500];
                match socket.recv(&mut buf) {
                    Ok(n) => {
                        let data = unsafe {
                            std::slice::from_raw_parts(buf.as_ptr() as *const u8, n)
                        };
                        if let Some((rid, rseq)) = parse_echo_reply(data) {
                            if rid == ident && rseq == seq as u16 {
                                rtts.push(sent_at.elapsed().as_secs_f64() * 1000.0);
                                break;
                            }
                        }
                    }
                    Err(e)
                        if e.kind() == ErrorKind::WouldBlock
                            || e.kind() == ErrorKind::TimedOut => {}
                    Err(_) => break,
                }
            }
            if seq + 1 < count {
                std::thread::sleep(interval);
            }
        }
        super::summarize(rtts, count)
    }

    fn open_ping_socket() -> std::io::Result<Socket> {
        // Unprivileged ping socket first.
        match Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::ICMPV4)) {
            Ok(s) => Ok(s),
            Err(_) => Socket::new(Domain::IPV4, Type::RAW, Some(Protocol::ICMPV4)),
        }
    }

    /// Extract (ident, seq) from an echo reply. DGRAM sockets deliver the
    /// ICMP message only; RAW sockets deliver the full IP packet.
    fn parse_echo_reply(data: &[u8]) -> Option<(u16, u16)> {
        let icmp = if data.len() >= 8 && (data[0] >> 4) == 4 {
            // Raw: strip the IPv4 header.
            let ihl = ((data[0] & 0x0F) as usize) * 4;
            if data.len() < ihl + 8 {
                return None;
            }
            &data[ihl..]
        } else {
            data
        };
        if icmp.len() < 8 || icmp[0] != 0 {
            // type 0 = echo reply
            return None;
        }
        Some((
            u16::from_be_bytes([icmp[4], icmp[5]]),
            u16::from_be_bytes([icmp[6], icmp[7]]),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksum_rfc1071_vector() {
        // RFC 1071 §4.1 example bytes: 00 01 f2 03 f4 f5 f6 f7 -> sum b861,
        // checksum 479e (computed with the algorithm over 8 bytes).
        let data = [0x00, 0x01, 0xf2, 0x03, 0xf4, 0xf5, 0xf6, 0xf7];
        assert_eq!(internet_checksum(&data), 0x220d);
        // Self-consistency: checksum over header+checksum == 0.
        let pkt = build_echo_request(0x1234, 7, b"abcdefgh");
        assert_eq!(internet_checksum(&pkt), 0);
    }

    #[test]
    fn echo_request_layout() {
        let pkt = build_echo_request(0xBEEF, 0x0102, &[1, 2, 3]);
        assert_eq!(pkt[0], 8); // echo request
        assert_eq!(pkt[1], 0);
        assert_eq!(&pkt[4..6], &0xBEEFu16.to_be_bytes());
        assert_eq!(&pkt[6..8], &0x0102u16.to_be_bytes());
        assert_eq!(&pkt[8..], &[1, 2, 3]);
    }

    #[test]
    fn summarize_math() {
        let r = summarize(vec![10.0, 20.0, 30.0, 40.0], 5);
        assert_eq!(r.sent, 5);
        assert_eq!(r.received, 4);
        assert_eq!(r.loss_pct, 20.0);
        assert_eq!(r.rtt_avg_ms, Some(25.0));
        assert_eq!(r.rtt_p95_ms, Some(40.0));
        assert!(r.error_class.is_none());
    }

    #[tokio::test]
    async fn non_linux_or_unprivileged_returns_blocked_or_runs() {
        // On the Windows CI host this must take the stub path.
        let r = ping_cycle(
            "127.0.0.1".parse().unwrap(),
            1,
            Duration::from_millis(1),
            Duration::from_millis(50),
        )
        .await;
        if cfg!(target_os = "linux") {
            // Whatever the sandbox permits, the call must terminate.
            let _ = r;
        } else {
            assert_eq!(r.error_class.as_deref(), Some("probe-blocked"));
        }
    }
}
