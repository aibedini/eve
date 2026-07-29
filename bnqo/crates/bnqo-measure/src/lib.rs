//! Measurement math and active probers for the BNQO agent.
//!
//! - [`window`] — per-window aggregation: loss, RTT, RFC 3393 jitter,
//!   reordering, duplication, burst loss, clock-gated OWD (PROTOCOL.md §6).
//! - [`icmp`] — ICMP echo prober (Linux sockets, stub elsewhere).
//! - [`service`] — TCP/TLS connect prober with error classification.
//! - [`host`] — host metrics reader (Linux /proc + /sys, stub elsewhere).

pub mod host;
pub mod icmp;
pub mod service;
pub mod window;

pub use window::{ClockEstimate, WindowAggregator, WindowStats};
