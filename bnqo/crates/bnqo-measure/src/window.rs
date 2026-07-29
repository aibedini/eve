//! Per-window measurement aggregation (PROTOCOL.md §6).
//!
//! All math here is platform-independent and unit-tested against reference
//! vectors. Timestamps enter as NTP-64 values; RTT uses same-host durations
//! (T4 - T1 at the sender, reflector turnaround at the reflector) so it is
//! immune to clock offset (§6.1). OWD is only ever emitted with a clock
//! confidence mark (§6.2).

use bnqo_proto::packet::{ntp64_diff_ms, ClockQuality};
use std::collections::{BTreeMap, HashMap};

/// Clock telemetry of the local host, fed in from the host-metrics reader.
#[derive(Debug, Clone, Copy)]
pub struct ClockEstimate {
    /// Local clock offset vs UTC in ms (positive = local clock ahead), from
    /// NTP/PTP telemetry — never from probe traffic (§6.2).
    pub offset_ms: f64,
    /// Uncertainty `u` in ms (root delay/2 + root dispersion or equivalent).
    /// `None` = no clock telemetry at all.
    pub uncertainty_ms: Option<f64>,
    /// Time source locked/synchronized.
    pub locked: bool,
    /// Profile limit `u_max` (§6.2 default 5 ms).
    pub u_max_ms: f64,
}

impl Default for ClockEstimate {
    fn default() -> Self {
        ClockEstimate {
            offset_ms: 0.0,
            uncertainty_ms: None,
            locked: false,
            u_max_ms: 5.0,
        }
    }
}

impl ClockEstimate {
    /// Marking rules of §6.2 / PROTOCOL.md §2.3.
    pub fn quality(&self) -> ClockQuality {
        match self.uncertainty_ms {
            None => ClockQuality::Unknown,
            Some(_) if !self.locked => ClockQuality::Invalid,
            Some(u) if u <= self.u_max_ms => ClockQuality::Good,
            Some(u) if u <= 10.0 * self.u_max_ms => ClockQuality::Low,
            Some(_) => ClockQuality::Invalid,
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct SentRec {
    t1: u64,
}

#[derive(Debug, Clone, Copy)]
struct RecvRec {
    seq: u32,
    #[allow(dead_code)]
    nonce: u32,
    t1: u64,
    t2: u64,
    t4: u64,
    turnaround_us: u32,
}

/// Aggregates one measurement window for one link/direction at the sender.
#[derive(Debug, Default)]
pub struct WindowAggregator {
    sent: BTreeMap<u32, SentRec>,
    /// Received reflections in arrival order (jitter needs the arrival
    /// series, §6.6).
    received: Vec<RecvRec>,
    /// nonce seen per seq, for duplicate/corrupt classification (§6.5).
    seq_nonce: HashMap<u32, u32>,
    highest_recv: Option<u32>,
    reordered: u32,
    max_reorder_distance: u32,
    duplicates: u32,
    corrupted: u32,
}

impl WindowAggregator {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn on_send(&mut self, seq: u32, _nonce: u32, sender_timestamp: u64) {
        self.sent.insert(seq, SentRec { t1: sender_timestamp });
    }

    /// Record a validated reflection (AEAD already verified by the caller).
    pub fn on_receive(
        &mut self,
        seq: u32,
        nonce: u32,
        t2: u64,
        turnaround_us: u32,
        t4: u64,
    ) {
        // Duplicate/corrupt classification (§6.5).
        if let Some(&first_nonce) = self.seq_nonce.get(&seq) {
            if first_nonce == nonce {
                self.duplicates += 1;
                // A duplicate is not an independent sample: keep the first.
                return;
            }
            // Same seq, different nonce: sender bug or on-path tampering.
            self.corrupted += 1;
            return;
        }
        let Some(&s) = self.sent.get(&seq) else {
            // Reflection for a packet we never sent (or already aged out):
            // count as corrupted rather than silently accepting.
            self.corrupted += 1;
            return;
        };
        self.seq_nonce.insert(seq, nonce);

        // Reordering (§6.5): arrival with seq < M is reordered, d = M - s.
        if let Some(m) = self.highest_recv {
            if seq < m {
                self.reordered += 1;
                self.max_reorder_distance = self.max_reorder_distance.max(m - seq);
            }
        }
        self.highest_recv = Some(self.highest_recv.map_or(seq, |m| m.max(seq)));

        self.received.push(RecvRec {
            seq,
            nonce,
            t1: s.t1,
            t2,
            t4,
            turnaround_us,
        });
    }

    /// Count an AEAD / structural validation failure on the receive path
    /// (§6.5 "corrupted").
    pub fn count_corrupted(&mut self) {
        self.corrupted += 1;
    }

    pub fn sent_count(&self) -> u64 {
        self.sent.len() as u64
    }

    /// Compute the window record and reset the aggregator for the next
    /// window. OWD is clock-gated per §6.2; the quality mark always travels
    /// with the value.
    pub fn finish_window(&mut self, clock: &ClockEstimate) -> WindowStats {
        let stats = compute_stats(
            &self.sent,
            &self.received,
            self.reordered,
            self.max_reorder_distance,
            self.duplicates,
            self.corrupted,
            clock,
        );
        *self = Self::new();
        stats
    }
}

/// One finished window (contract §2.4 `measurements[]` fields).
#[derive(Debug, Clone, serde::Serialize)]
pub struct WindowStats {
    pub sent: u64,
    pub received: u64,
    pub loss_pct: f64,
    pub rtt_min_ms: Option<f64>,
    pub rtt_avg_ms: Option<f64>,
    pub rtt_p95_ms: Option<f64>,
    pub rtt_max_ms: Option<f64>,
    pub jitter_ms: Option<f64>,
    pub reordered: u32,
    pub max_reorder_distance: u32,
    pub duplicated: u32,
    pub corrupted: u32,
    pub burst_loss_count: u32,
    pub burst_max: u32,
    /// Mean forward OWD (ms), gated by `clock_quality` (§6.2). `None` when
    /// no reflections arrived or no clock telemetry exists.
    pub owd_ms: Option<f64>,
    pub clock_quality: ClockQuality,
}

fn compute_stats(
    sent: &BTreeMap<u32, SentRec>,
    received: &[RecvRec],
    reordered: u32,
    max_reorder_distance: u32,
    duplicates: u32,
    corrupted: u32,
    clock: &ClockEstimate,
) -> WindowStats {
    let n_sent = sent.len() as u64;
    let n_recv = received.len() as u64;
    let loss_pct = if n_sent == 0 {
        0.0
    } else {
        round2(100.0 * (n_sent - n_recv) as f64 / n_sent as f64)
    };

    // RTT (§6.1): (T4 - T1) - (T3 - T2); both terms single-clock.
    let mut rtts: Vec<f64> = received
        .iter()
        .map(|r| ntp64_diff_ms(r.t4, r.t1) - r.turnaround_us as f64 / 1000.0)
        .filter(|rtt| rtt.is_finite() && *rtt >= 0.0)
        .collect();
    rtts.sort_by(f64::total_cmp);
    let (rtt_min, rtt_avg, rtt_p95, rtt_max) = if rtts.is_empty() {
        (None, None, None, None)
    } else {
        (
            // round2: NTP-64 quantization leaves ±0.25 µs of noise on each
            // diff; the report schema carries 2-decimal ms values.
            rtts.first().map(|&v| round2(v)),
            Some(round2(rtts.iter().sum::<f64>() / rtts.len() as f64)),
            Some(round2(percentile_nearest_rank(&rtts, 95.0))),
            rtts.last().map(|&v| round2(v)),
        )
    };

    // Jitter (§6.6, RFC 3393 §4.6): on the arrival series of reverse
    // transit times RT_i = T4_i - T3_i; clock offset cancels in D_i.
    let jitter_ms = if received.len() >= 2 {
        let rts: Vec<f64> = received
            .iter()
            .map(|r| ntp64_diff_ms(r.t4, r.t2) - r.turnaround_us as f64 / 1000.0)
            .collect();
        let mut j = 0.0f64;
        for w in rts.windows(2) {
            let d = (w[1] - w[0]).abs();
            j += (d - j) / 16.0;
        }
        Some(round2(j))
    } else {
        None
    };

    // Burst loss (§6.4): loss indicator over the window's seq span.
    let (burst_loss_count, burst_max) = burst_stats(sent, received);

    // OWD (§6.2): OWD_fwd = (T2 - T1) - theta with theta = e_r - e_s.
    // The peer offset e_r is not on the wire in Phase 1; we correct with our
    // own offset (theta = -e_s, i.e. assume the reflector sits on true UTC)
    // and let the uncertainty gate decide how the value may be used.
    let quality = clock.quality();
    let owd_ms = if received.is_empty() || clock.uncertainty_ms.is_none() {
        None
    } else {
        let sum: f64 = received
            .iter()
            .map(|r| ntp64_diff_ms(r.t2, r.t1) + clock.offset_ms)
            .sum();
        Some(round2(sum / received.len() as f64))
    };

    WindowStats {
        sent: n_sent,
        received: n_recv,
        loss_pct,
        rtt_min_ms: rtt_min,
        rtt_avg_ms: rtt_avg,
        rtt_p95_ms: rtt_p95,
        rtt_max_ms: rtt_max,
        jitter_ms,
        reordered,
        max_reorder_distance,
        duplicated: duplicates,
        corrupted,
        burst_loss_count,
        burst_max,
        owd_ms,
        clock_quality: quality,
    }
}

fn burst_stats(sent: &BTreeMap<u32, SentRec>, received: &[RecvRec]) -> (u32, u32) {
    if sent.is_empty() {
        return (0, 0);
    }
    let first = *sent.keys().next().unwrap();
    let last = *sent.keys().next_back().unwrap();
    let received_seqs: std::collections::HashSet<u32> =
        received.iter().map(|r| r.seq).collect();
    let mut runs: Vec<u32> = Vec::new();
    let mut cur = 0u32;
    for seq in first..=last {
        // Seqs that were never sent do not occur (sender is strictly
        // increasing by 1), but treat gaps as not-lost to stay robust.
        if sent.contains_key(&seq) && !received_seqs.contains(&seq) {
            cur += 1;
        } else if cur > 0 {
            runs.push(cur);
            cur = 0;
        }
    }
    if cur > 0 {
        runs.push(cur);
    }
    let burst_loss_count = runs.iter().filter(|&&r| r >= 2).count() as u32;
    let burst_max = runs.iter().copied().max().unwrap_or(0);
    (burst_loss_count, burst_max)
}

/// Nearest-rank percentile over a sorted slice.
pub fn percentile_nearest_rank(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let rank = ((p / 100.0) * sorted.len() as f64).ceil() as usize;
    sorted[rank.max(1).min(sorted.len()) - 1]
}

fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use bnqo_proto::packet::ntp64_from_unix_nanos;

    fn ts(ms_since_epoch: u64) -> u64 {
        ntp64_from_unix_nanos(ms_since_epoch * 1_000_000)
    }

    fn good_clock() -> ClockEstimate {
        ClockEstimate {
            offset_ms: 0.5,
            uncertainty_ms: Some(1.0),
            locked: true,
            u_max_ms: 5.0,
        }
    }

    /// Build a window: seqs 1..=10 sent at t=1000ms spaced 100ms; all
    /// reflected with RTT 100ms and turnaround 1ms except seq 4 and 5 lost.
    fn reference_window(agg: &mut WindowAggregator) {
        for seq in 1..=10u32 {
            let t1 = ts(1000 + (seq as u64 - 1) * 100);
            agg.on_send(seq, 0x1000 + seq, t1);
            if seq == 4 || seq == 5 {
                continue; // lost
            }
            let t2 = ts(1000 + (seq as u64 - 1) * 100 + 50); // 50 ms OWD
            let t4 = ts(1000 + (seq as u64 - 1) * 100 + 101); // RTT 100 ms
            agg.on_receive(seq, 0x1000 + seq, t2, 1000, t4);
        }
    }

    #[test]
    fn loss_and_rtt_math() {
        let mut agg = WindowAggregator::new();
        reference_window(&mut agg);
        let s = agg.finish_window(&good_clock());
        assert_eq!(s.sent, 10);
        assert_eq!(s.received, 8);
        assert_eq!(s.loss_pct, 20.0);
        // RTT = (T4-T1) - turnaround = 101 - 1 = 100 ms exactly for all.
        assert_eq!(s.rtt_min_ms, Some(100.0));
        assert_eq!(s.rtt_avg_ms, Some(100.0));
        assert_eq!(s.rtt_max_ms, Some(100.0));
        assert_eq!(s.rtt_p95_ms, Some(100.0));
        // Loss run of seq 4,5 is a single burst of length 2.
        assert_eq!(s.burst_loss_count, 1);
        assert_eq!(s.burst_max, 2);
        // OWD = (T2-T1) + our offset = 50 + 0.5 = 50.5, quality good.
        assert_eq!(s.owd_ms, Some(50.5));
        assert_eq!(s.clock_quality, ClockQuality::Good);
    }

    #[test]
    fn rfc3393_jitter_reference_vector() {
        // Arrival transit times (ms): 10, 12, 11, 20, 20
        // |D|: 2, 1, 9, 0
        // J: 2/16=0.125; 0.125+(1-0.125)/16=0.1796875; +(9-0.1796875)/16=0.73095703;
        //    +(0-0.73095703)/16=0.6852722...
        let mut agg = WindowAggregator::new();
        let transits = [10.0f64, 12.0, 11.0, 20.0, 20.0];
        for (i, &rt) in transits.iter().enumerate() {
            let seq = (i + 1) as u32;
            let t1 = ts(10_000 + i as u64 * 100);
            agg.on_send(seq, seq, t1);
            // Choose turnaround 0 and t4 so that (T4-T2) = rt ms: set T2=T1.
            let t4 = ts(10_000 + i as u64 * 100 + rt as u64);
            agg.on_receive(seq, seq, t1, 0, t4);
        }
        let s = agg.finish_window(&good_clock());
        let expected = {
            let mut j = 0.0f64;
            for w in transits.windows(2) {
                let d = (w[1] - w[0]).abs();
                j += (d - j) / 16.0;
            }
            (j * 100.0).round() / 100.0
        };
        assert_eq!(s.jitter_ms, Some(expected));
        assert_eq!(s.jitter_ms, Some(0.69));
    }

    #[test]
    fn reorder_and_duplicate_math() {
        let mut agg = WindowAggregator::new();
        for seq in 1..=4u32 {
            let t1 = ts(1000 + seq as u64 * 100);
            agg.on_send(seq, 0x5000 + seq, t1);
        }
        // Arrival order: 1, 3, 2 (reordered, distance 1), 3 again (dup),
        // 2 with different nonce (corrupted), 4.
        agg.on_receive(1, 0x5001, ts(1050), 1000, ts(1101));
        agg.on_receive(3, 0x5003, ts(1250), 1000, ts(1301));
        agg.on_receive(2, 0x5002, ts(1150), 1000, ts(1310));
        agg.on_receive(3, 0x5003, ts(1250), 1000, ts(1315)); // exact dup
        agg.on_receive(2, 0x9999, ts(1150), 1000, ts(1320)); // same seq, other nonce
        agg.on_receive(4, 0x5004, ts(1350), 1000, ts(1401));
        let s = agg.finish_window(&good_clock());
        assert_eq!(s.received, 4); // dup/corrupt arrivals are not samples
        assert_eq!(s.reordered, 1);
        assert_eq!(s.max_reorder_distance, 1);
        assert_eq!(s.duplicated, 1);
        assert_eq!(s.corrupted, 1);
        assert_eq!(s.loss_pct, 0.0);
    }

    #[test]
    fn burst_loss_runs() {
        let mut agg = WindowAggregator::new();
        for seq in 1..=12u32 {
            agg.on_send(seq, seq, ts(1000 * seq as u64));
        }
        // Losses: 2 (isolated), 5-6-7 (burst of 3), 10-11 (burst of 2).
        for seq in [1u32, 3, 4, 8, 9, 12] {
            agg.on_receive(seq, seq, ts(1000 * seq as u64 + 40), 500, ts(1000 * seq as u64 + 81));
        }
        let s = agg.finish_window(&good_clock());
        assert_eq!(s.sent, 12);
        assert_eq!(s.received, 6);
        assert_eq!(s.loss_pct, 50.0);
        assert_eq!(s.burst_loss_count, 2); // runs >= 2
        assert_eq!(s.burst_max, 3);
    }

    #[test]
    fn owd_clock_gating() {
        // good
        let mut agg = WindowAggregator::new();
        reference_window(&mut agg);
        assert_eq!(
            agg.finish_window(&ClockEstimate {
                uncertainty_ms: Some(4.9),
                ..good_clock()
            })
            .clock_quality,
            ClockQuality::Good
        );
        // low: u_max < u <= 10*u_max
        let mut agg = WindowAggregator::new();
        reference_window(&mut agg);
        let s = agg.finish_window(&ClockEstimate {
            uncertainty_ms: Some(20.0),
            ..good_clock()
        });
        assert_eq!(s.clock_quality, ClockQuality::Low);
        assert!(s.owd_ms.is_some()); // stored but flagged
        // invalid: u > 10*u_max
        let mut agg = WindowAggregator::new();
        reference_window(&mut agg);
        let s = agg.finish_window(&ClockEstimate {
            uncertainty_ms: Some(51.0),
            ..good_clock()
        });
        assert_eq!(s.clock_quality, ClockQuality::Invalid);
        // invalid: unsynchronized
        let mut agg = WindowAggregator::new();
        reference_window(&mut agg);
        let s = agg.finish_window(&ClockEstimate {
            locked: false,
            uncertainty_ms: Some(1.0),
            ..good_clock()
        });
        assert_eq!(s.clock_quality, ClockQuality::Invalid);
        // unknown: no telemetry -> no OWD value at all
        let mut agg = WindowAggregator::new();
        reference_window(&mut agg);
        let s = agg.finish_window(&ClockEstimate::default());
        assert_eq!(s.clock_quality, ClockQuality::Unknown);
        assert_eq!(s.owd_ms, None);
    }

    #[test]
    fn percentile_nearest_rank_vector() {
        let v: Vec<f64> = (1..=100).map(f64::from).collect();
        assert_eq!(percentile_nearest_rank(&v, 95.0), 95.0);
        assert_eq!(percentile_nearest_rank(&v, 50.0), 50.0);
        assert_eq!(percentile_nearest_rank(&v, 100.0), 100.0);
        assert_eq!(percentile_nearest_rank(&v, 1.0), 1.0);
    }

    #[test]
    fn empty_window_is_zeroed_not_nan() {
        let mut agg = WindowAggregator::new();
        let s = agg.finish_window(&good_clock());
        assert_eq!(s.sent, 0);
        assert_eq!(s.loss_pct, 0.0);
        assert_eq!(s.rtt_avg_ms, None);
        assert_eq!(s.jitter_ms, None);
        assert_eq!(s.burst_max, 0);
    }
}
