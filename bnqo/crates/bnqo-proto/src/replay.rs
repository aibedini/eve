//! Receiver-side replay window (PROTOCOL.md §4.2) with the duplicate
//! reflection cap (§4.4).
//!
//! The window accepts late/reordered packets inside a 4096-bit bitmap, drops
//! stale packets, and routes exact replays down the DUPLICATE path instead of
//! rejecting them — duplicates are a measurement signal (§5 FR-MEASURE-002),
//! so they are accepted but reflected at most `MAX_DUP_REFLECTIONS` times per
//! sequence number.

use std::collections::HashMap;

pub const REPLAY_WINDOW_BITS: u32 = 4096;
pub const MAX_DUP_REFLECTIONS: u8 = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplayDecision {
    /// Forward progress (`s > H`): accept and reflect.
    New,
    /// Inside the window, not seen before: late/reordered arrival, accept.
    Late,
    /// Older than the window: drop as STALE (counted).
    Stale,
    /// Seen before. `allowed` is true while fewer than MAX_DUP_REFLECTIONS
    /// reflections have been emitted for this sequence number.
    Duplicate { allowed: bool },
}

impl ReplayDecision {
    pub fn should_reflect(self) -> bool {
        match self {
            ReplayDecision::New | ReplayDecision::Late => true,
            ReplayDecision::Stale => false,
            ReplayDecision::Duplicate { allowed } => allowed,
        }
    }
}

#[derive(Debug)]
pub struct ReplayWindow {
    /// Highest authenticated sequence number seen (`H`), None before the
    /// first packet.
    highest: Option<u32>,
    /// Bitmap of the last 4096 sequence numbers; bit i covers H - i.
    bitmap: [u64; (REPLAY_WINDOW_BITS / 64) as usize],
    /// Reflection counts per sequence number (1 after the first reflection;
    /// duplicates may push it up to MAX_DUP_REFLECTIONS).
    reflections: HashMap<u32, u8>,
}

impl Default for ReplayWindow {
    fn default() -> Self {
        ReplayWindow {
            highest: None,
            bitmap: [0u64; (REPLAY_WINDOW_BITS / 64) as usize],
            reflections: HashMap::new(),
        }
    }
}

impl ReplayWindow {
    pub fn new() -> Self {
        Self::default()
    }

    fn bit(&self, idx: u32) -> bool {
        let word = (idx / 64) as usize;
        let off = idx % 64;
        self.bitmap[word] & (1u64 << off) != 0
    }

    fn set_bit(&mut self, idx: u32) {
        let word = (idx / 64) as usize;
        let off = idx % 64;
        self.bitmap[word] |= 1u64 << off;
    }

    fn shift_right(&mut self, n: u32) {
        if n >= REPLAY_WINDOW_BITS {
            self.bitmap = [0u64; (REPLAY_WINDOW_BITS / 64) as usize];
            return;
        }
        let words = (n / 64) as usize;
        let bits = n % 64;
        let old = self.bitmap;
        for (w, slot) in self.bitmap.iter_mut().enumerate() {
            let hi = if w >= words { old[w - words] } else { 0 };
            let lo = if bits != 0 && w > words { old[w - words - 1] } else { 0 };
            *slot = if bits == 0 {
                hi
            } else {
                (hi << bits) | (lo >> (64 - bits))
            };
        }
    }

    /// Apply the window algorithm (PROTOCOL.md §4.2). Only call this for
    /// packets that have already passed AEAD verification.
    pub fn check(&mut self, seq: u32) -> ReplayDecision {
        match self.highest {
            None => {
                self.highest = Some(seq);
                self.set_bit(0);
                self.reflections.insert(seq, 1);
                ReplayDecision::New
            }
            Some(h) if seq > h => {
                let delta = seq - h;
                self.shift_right(delta);
                self.set_bit(0);
                self.highest = Some(seq);
                self.gc(seq);
                self.reflections.insert(seq, 1);
                ReplayDecision::New
            }
            Some(h) => {
                let back = h - seq;
                if back >= REPLAY_WINDOW_BITS {
                    return ReplayDecision::Stale;
                }
                if back == 0 || self.bit(back) {
                    // Seen before (back == 0 means seq == H, which is always
                    // set). Duplicate path with the §4.4 cap.
                    let count = self.reflections.entry(seq).or_insert(0);
                    if *count < MAX_DUP_REFLECTIONS {
                        *count += 1;
                        ReplayDecision::Duplicate { allowed: true }
                    } else {
                        ReplayDecision::Duplicate { allowed: false }
                    }
                } else {
                    self.set_bit(back);
                    self.reflections.insert(seq, 1);
                    ReplayDecision::Late
                }
            }
        }
    }

    /// Drop reflection counters that have fallen out of the window.
    fn gc(&mut self, new_highest: u32) {
        let floor = new_highest.saturating_sub(REPLAY_WINDOW_BITS);
        self.reflections.retain(|&seq, _| seq >= floor);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_new_and_late_rejects_stale() {
        let mut w = ReplayWindow::new();
        assert_eq!(w.check(1), ReplayDecision::New);
        assert_eq!(w.check(2), ReplayDecision::New);
        assert_eq!(w.check(5), ReplayDecision::New);
        // Reordered arrivals inside the window are accepted once.
        assert_eq!(w.check(4), ReplayDecision::Late);
        assert_eq!(w.check(3), ReplayDecision::Late);
        // Way behind (>= REPLAY_WINDOW_BITS behind H): stale.
        assert_eq!(w.check(5 + REPLAY_WINDOW_BITS), ReplayDecision::New);
        assert_eq!(w.check(5), ReplayDecision::Stale);
        // Forward jump larger than the window resets cleanly.
        assert_eq!(w.check(10_000), ReplayDecision::New);
        assert_eq!(w.check(10_000 - 5), ReplayDecision::Late);
        assert_eq!(w.check(10_000 - REPLAY_WINDOW_BITS), ReplayDecision::Stale);
    }

    #[test]
    fn duplicates_capped_at_eight_reflections() {
        let mut w = ReplayWindow::new();
        assert_eq!(w.check(100), ReplayDecision::New);
        assert_eq!(w.check(101), ReplayDecision::New);
        // seq 100 has had 1 reflection; duplicates allowed until 8 total.
        let mut allowed = 0;
        let mut suppressed = 0;
        for _ in 0..20 {
            match w.check(100) {
                ReplayDecision::Duplicate { allowed: true } => allowed += 1,
                ReplayDecision::Duplicate { allowed: false } => suppressed += 1,
                other => panic!("unexpected decision {other:?}"),
            }
        }
        assert_eq!(allowed, MAX_DUP_REFLECTIONS as usize - 1);
        assert_eq!(suppressed, 20 - allowed);
    }

    #[test]
    fn duplicate_of_current_highest_is_detected() {
        let mut w = ReplayWindow::new();
        w.check(7);
        assert!(matches!(w.check(7), ReplayDecision::Duplicate { allowed: true }));
    }

    #[test]
    fn duplicate_counters_gc_after_window_advance() {
        let mut w = ReplayWindow::new();
        w.check(1);
        w.check(1); // duplicate -> count 2
        w.check(1 + REPLAY_WINDOW_BITS + 10);
        // seq 1 fell out of the window entirely.
        assert_eq!(w.check(1), ReplayDecision::Stale);
    }

    #[test]
    fn bitmap_shift_correctness() {
        let mut w = ReplayWindow::new();
        for s in [100u32, 101, 105, 200] {
            w.check(s);
        }
        // All earlier accepted seqs must read as duplicates now.
        for s in [100u32, 101, 105] {
            assert!(matches!(w.check(s), ReplayDecision::Duplicate { .. }), "seq {s}");
        }
        // Gap seqs not previously seen are late-accepted.
        assert_eq!(w.check(150), ReplayDecision::Late);
    }
}
