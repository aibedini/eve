//! BNQO-UDP v1 packet layout (PROTOCOL.md §2).
//!
//! Fixed portion = 64 bytes = 48-byte authenticated header + 16-byte AEAD tag.
//! Bytes 0..48 are the AEAD AAD. All multi-byte integers are big-endian.

use thiserror::Error;

pub const MAGIC: u16 = 0x424E; // "BN"
pub const PROTOCOL_VERSION: u8 = 0x01;
pub const HEADER_LEN: usize = 48;
pub const TAG_LEN: usize = 16;
pub const MIN_PACKET_SIZE: usize = HEADER_LEN + TAG_LEN; // 64
pub const MAX_PACKET_SIZE: usize = 9000;

/// NTP era-0 offset: seconds between 1900-01-01 and 1970-01-01 (RFC 5905 §6).
pub const NTP_UNIX_EPOCH_OFFSET: u64 = 2_208_988_800;

/// Flag bits (PROTOCOL.md §2.3).
pub struct Flags;

impl Flags {
    pub const REFLECTED: u8 = 0x01;
    pub const CLOCK_QUALITY_MASK: u8 = 0x06;
    pub const MTU_PROBE: u8 = 0x08;
    pub const DF_REQUESTED: u8 = 0x10;
    pub const SESSION_TEARDOWN: u8 = 0x20;
    pub const RESERVED_MASK: u8 = 0xC0;

    pub fn clock_quality_to_bits(q: ClockQuality) -> u8 {
        (q as u8) << 1
    }

    pub fn clock_quality_from_bits(flags: u8) -> ClockQuality {
        match (flags & Self::CLOCK_QUALITY_MASK) >> 1 {
            0 => ClockQuality::Unknown,
            1 => ClockQuality::Good,
            2 => ClockQuality::Low,
            _ => ClockQuality::Invalid,
        }
    }
}

/// Clock-sync state of the writer of a packet (PROTOCOL.md §2.3, §6.2).
/// Values match the wire encoding in the CLOCK_QUALITY flag bits.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ClockQuality {
    Unknown = 0,
    Good = 1,
    Low = 2,
    Invalid = 3,
}

impl ClockQuality {
    /// String form used in the eve report schema (EVE_API_CONTRACT.md §2.4).
    pub fn as_str(self) -> &'static str {
        match self {
            ClockQuality::Unknown => "unknown",
            ClockQuality::Good => "good",
            ClockQuality::Low => "low",
            ClockQuality::Invalid => "invalid",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum PacketError {
    #[error("packet too short: {0} bytes (minimum 64)")]
    TooShort(usize),
    #[error("packet too large: {0} bytes (maximum 9000)")]
    TooLarge(usize),
    #[error("bad magic: {0:#06x}")]
    BadMagic(u16),
    #[error("unsupported protocol version: {0}")]
    BadVersion(u8),
    #[error("reserved flag bits set: {0:#04x}")]
    ReservedFlags(u8),
    #[error("payload_length field {0} != actual datagram length {1}")]
    LengthMismatch(u16, usize),
}

/// The 48-byte authenticated header, parsed (PROTOCOL.md §2.2).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Header {
    pub flags: u8,
    pub test_id: u32,
    pub session_id: u64,
    pub sequence_number: u32,
    pub sender_timestamp: u64,
    pub receive_timestamp: u64,
    pub reflector_turnaround_us: u32,
    pub nonce: u32,
    pub payload_length: u16,
    pub key_epoch: u16,
}

impl Header {
    /// Structural validation + parse (validation steps 1–2 of PROTOCOL.md §4.3).
    ///
    /// Checks length bounds, magic, version, reserved flag bits and the
    /// payload_length invariant. Does NOT check the AEAD tag.
    pub fn decode(datagram: &[u8]) -> Result<Header, PacketError> {
        if datagram.len() < MIN_PACKET_SIZE {
            return Err(PacketError::TooShort(datagram.len()));
        }
        if datagram.len() > MAX_PACKET_SIZE {
            return Err(PacketError::TooLarge(datagram.len()));
        }
        let magic = u16::from_be_bytes([datagram[0], datagram[1]]);
        if magic != MAGIC {
            return Err(PacketError::BadMagic(magic));
        }
        let version = datagram[2];
        if version != PROTOCOL_VERSION {
            return Err(PacketError::BadVersion(version));
        }
        let flags = datagram[3];
        if flags & Flags::RESERVED_MASK != 0 {
            return Err(PacketError::ReservedFlags(flags));
        }
        let payload_length = u16::from_be_bytes([datagram[44], datagram[45]]);
        if payload_length as usize != datagram.len() {
            return Err(PacketError::LengthMismatch(payload_length, datagram.len()));
        }
        Ok(Header {
            flags,
            test_id: u32::from_be_bytes(datagram[4..8].try_into().unwrap()),
            session_id: u64::from_be_bytes(datagram[8..16].try_into().unwrap()),
            sequence_number: u32::from_be_bytes(datagram[16..20].try_into().unwrap()),
            sender_timestamp: u64::from_be_bytes(datagram[20..28].try_into().unwrap()),
            receive_timestamp: u64::from_be_bytes(datagram[28..36].try_into().unwrap()),
            reflector_turnaround_us: u32::from_be_bytes(datagram[36..40].try_into().unwrap()),
            nonce: u32::from_be_bytes(datagram[40..44].try_into().unwrap()),
            payload_length,
            key_epoch: u16::from_be_bytes([datagram[46], datagram[47]]),
        })
    }

    /// Serialize the 48-byte header into `out[0..48]` (AAD region).
    pub fn encode(&self, out: &mut [u8]) {
        assert!(out.len() >= HEADER_LEN);
        out[0..2].copy_from_slice(&MAGIC.to_be_bytes());
        out[2] = PROTOCOL_VERSION;
        out[3] = self.flags;
        out[4..8].copy_from_slice(&self.test_id.to_be_bytes());
        out[8..16].copy_from_slice(&self.session_id.to_be_bytes());
        out[16..20].copy_from_slice(&self.sequence_number.to_be_bytes());
        out[20..28].copy_from_slice(&self.sender_timestamp.to_be_bytes());
        out[28..36].copy_from_slice(&self.receive_timestamp.to_be_bytes());
        out[36..40].copy_from_slice(&self.reflector_turnaround_us.to_be_bytes());
        out[40..44].copy_from_slice(&self.nonce.to_be_bytes());
        out[44..46].copy_from_slice(&self.payload_length.to_be_bytes());
        out[46..48].copy_from_slice(&self.key_epoch.to_be_bytes());
    }

    pub fn is_reflected(&self) -> bool {
        self.flags & Flags::REFLECTED != 0
    }

    pub fn clock_quality(&self) -> ClockQuality {
        Flags::clock_quality_from_bits(self.flags)
    }

    pub fn with_clock_quality(mut self, q: ClockQuality) -> Self {
        self.flags = (self.flags & !Flags::CLOCK_QUALITY_MASK) | Flags::clock_quality_to_bits(q);
        self
    }
}

/// Convert Unix nanoseconds to an NTP-64 timestamp (PROTOCOL.md §2.4).
pub fn ntp64_from_unix_nanos(t_ns: u64) -> u64 {
    let secs = t_ns / 1_000_000_000 + NTP_UNIX_EPOCH_OFFSET;
    let sub_ns = t_ns % 1_000_000_000;
    // round((sub_ns / 1e9) * 2^32) computed in integers to avoid float drift.
    let frac = (sub_ns as u128 * (1u128 << 32) + 500_000_000) / 1_000_000_000;
    (secs << 32) | (frac as u64 & 0xFFFF_FFFF)
}

/// Convert an NTP-64 timestamp back to Unix nanoseconds (truncating).
pub fn unix_nanos_from_ntp64(ts: u64) -> u64 {
    let secs = (ts >> 32).saturating_sub(NTP_UNIX_EPOCH_OFFSET);
    let frac = ts & 0xFFFF_FFFF;
    secs * 1_000_000_000 + (frac as u128 * 1_000_000_000 / (1u128 << 32)) as u64
}

/// Signed difference of two NTP-64 timestamps in fractional milliseconds
/// (`b - a`, may be negative under clock offset).
pub fn ntp64_diff_ms(b: u64, a: u64) -> f64 {
    (unix_nanos_from_ntp64(b) as i128 - unix_nanos_from_ntp64(a) as i128) as f64 / 1.0e6
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_header() -> Header {
        Header {
            flags: Flags::MTU_PROBE | Flags::clock_quality_to_bits(ClockQuality::Good),
            test_id: 7,
            session_id: 0x0102_0304_0506_0708,
            sequence_number: 42,
            sender_timestamp: ntp64_from_unix_nanos(1_753_776_000_123_456_789),
            receive_timestamp: 0,
            reflector_turnaround_us: 0,
            nonce: 0xDEAD_BEEF,
            payload_length: MIN_PACKET_SIZE as u16,
            key_epoch: 3,
        }
    }

    #[test]
    fn header_roundtrip() {
        let h = sample_header();
        let mut buf = [0u8; MIN_PACKET_SIZE];
        h.encode(&mut buf);
        let decoded = Header::decode(&buf).unwrap();
        assert_eq!(h, decoded);
        assert!(!decoded.is_reflected());
        assert_eq!(decoded.clock_quality(), ClockQuality::Good);
    }

    #[test]
    fn ntp64_roundtrip() {
        // Values within NTP era 0 (1900 + 2^32 s => up to 2036-02-07).
        for t_ns in [0u64, 1, 999_999_999, 1_753_776_000_123_456_789, 2_000_000_000_999_999_999]
        {
            let ts = ntp64_from_unix_nanos(t_ns);
            let back = unix_nanos_from_ntp64(ts);
            // Conversion truncates at ~233 ps resolution; allow 1 ns of slop.
            assert!((t_ns as i128 - back as i128).abs() <= 1, "{t_ns} vs {back}");
        }
    }

    #[test]
    fn ntp64_known_vector() {
        // 1970-01-01T00:00:00Z -> seconds field exactly the era offset, frac 0.
        assert_eq!(ntp64_from_unix_nanos(0), NTP_UNIX_EPOCH_OFFSET << 32);
        // Half a second -> fraction 0x8000_0000.
        assert_eq!(
            ntp64_from_unix_nanos(500_000_000),
            (NTP_UNIX_EPOCH_OFFSET << 32) | 0x8000_0000
        );
    }

    #[test]
    fn decode_rejects_bad_structure() {
        let h = sample_header();
        let mut buf = [0u8; MIN_PACKET_SIZE];
        h.encode(&mut buf);

        assert!(matches!(Header::decode(&buf[..63]), Err(PacketError::TooShort(63))));

        let mut bad = buf;
        bad[0] = 0xFF;
        assert!(matches!(Header::decode(&bad), Err(PacketError::BadMagic(_))));

        let mut bad = buf;
        bad[2] = 0x02;
        assert!(matches!(Header::decode(&bad), Err(PacketError::BadVersion(2))));

        let mut bad = buf;
        bad[3] |= 0x80;
        assert!(matches!(Header::decode(&bad), Err(PacketError::ReservedFlags(_))));

        // payload_length must equal actual size.
        let mut big = vec![0u8; 128];
        h.encode(&mut big[..MIN_PACKET_SIZE]);
        assert!(matches!(
            Header::decode(&big),
            Err(PacketError::LengthMismatch(64, 128))
        ));
    }
}
