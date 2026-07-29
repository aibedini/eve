//! BNQO-UDP v1 wire protocol primitives (docs/bnqo/PROTOCOL.md) plus the
//! eve control-plane signature/canonicalization rules
//! (docs/bnqo/EVE_API_CONTRACT.md).
//!
//! This crate is pure, platform-independent Rust: no I/O, no OS calls.

pub mod canonjson;
pub mod crypto;
pub mod ed25519;
pub mod hkdf;
pub mod packet;
pub mod replay;

pub use crypto::{DirectionKeys, SessionKeys};
pub use packet::{
    ClockQuality, Header, Flags, MAGIC, MAX_PACKET_SIZE, MIN_PACKET_SIZE, PROTOCOL_VERSION,
    TAG_LEN, HEADER_LEN, NTP_UNIX_EPOCH_OFFSET,
};
pub use replay::{ReplayDecision, ReplayWindow, REPLAY_WINDOW_BITS, MAX_DUP_REFLECTIONS};
