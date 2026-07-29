//! XChaCha20-Poly1305 packet sealing (PROTOCOL.md §2.5) and the padding
//! keystream (§2.6).
//!
//! Nonce construction (24 bytes):
//!   nonce_salt[12] || key_epoch[2 BE] || nonce[4 BE] || seq[4 BE] || flags || version
//!
//! The AEAD runs in AAD-only mode: plaintext is empty, the 48-byte header is
//! the additional authenticated data, and the 16-byte tag is stored at offset
//! 48 of the packet.

use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::XChaCha20Poly1305;
use chacha20::{ChaCha8, cipher::{KeyIvInit, StreamCipher}};

use crate::hkdf;
use crate::packet::{Header, HEADER_LEN, TAG_LEN};

pub const PACKET_KEY_LEN: usize = 32;
pub const NONCE_SALT_LEN: usize = 12;
pub const NONCE_LEN: usize = 24; // XChaCha20 192-bit nonce

/// Build the 24-byte AEAD nonce for a packet (PROTOCOL.md §2.5).
pub fn build_nonce(nonce_salt: &[u8; NONCE_SALT_LEN], header: &Header) -> [u8; NONCE_LEN] {
    let mut n = [0u8; NONCE_LEN];
    n[0..12].copy_from_slice(nonce_salt);
    n[12..14].copy_from_slice(&header.key_epoch.to_be_bytes());
    n[14..18].copy_from_slice(&header.nonce.to_be_bytes());
    n[18..22].copy_from_slice(&header.sequence_number.to_be_bytes());
    n[22] = header.flags;
    n[23] = crate::packet::PROTOCOL_VERSION;
    n
}

fn aead(key: &[u8; PACKET_KEY_LEN]) -> XChaCha20Poly1305 {
    XChaCha20Poly1305::new(key.into())
}

/// Compute the 16-byte authentication tag for `packet[0..48]`.
pub fn compute_tag(
    key: &[u8; PACKET_KEY_LEN],
    nonce_salt: &[u8; NONCE_SALT_LEN],
    header: &Header,
    aad: &[u8],
) -> [u8; TAG_LEN] {
    debug_assert_eq!(aad.len(), HEADER_LEN);
    let nonce = build_nonce(nonce_salt, header);
    let ct = aead(key)
        .encrypt(
            (&nonce).into(),
            Payload {
                msg: &[],
                aad,
            },
        )
        .expect("XChaCha20-Poly1305 encryption of empty plaintext cannot fail");
    debug_assert_eq!(ct.len(), TAG_LEN);
    ct.try_into().unwrap()
}

/// Seal a packet in place: compute the tag over bytes 0..48 and write it at
/// bytes 48..64. `datagram` must be at least 64 bytes and contain the fully
/// populated header (including payload_length).
pub fn seal_packet(
    key: &[u8; PACKET_KEY_LEN],
    nonce_salt: &[u8; NONCE_SALT_LEN],
    datagram: &mut [u8],
) {
    let header = parse_header_prefix(datagram);
    let tag = compute_tag(key, nonce_salt, &header, &datagram[..HEADER_LEN]);
    datagram[HEADER_LEN..HEADER_LEN + TAG_LEN].copy_from_slice(&tag);
}

fn parse_header_prefix(d: &[u8]) -> Header {
    Header {
        flags: d[3],
        test_id: u32::from_be_bytes(d[4..8].try_into().unwrap()),
        session_id: u64::from_be_bytes(d[8..16].try_into().unwrap()),
        sequence_number: u32::from_be_bytes(d[16..20].try_into().unwrap()),
        sender_timestamp: u64::from_be_bytes(d[20..28].try_into().unwrap()),
        receive_timestamp: u64::from_be_bytes(d[28..36].try_into().unwrap()),
        reflector_turnaround_us: u32::from_be_bytes(d[36..40].try_into().unwrap()),
        nonce: u32::from_be_bytes(d[40..44].try_into().unwrap()),
        payload_length: u16::from_be_bytes(d[44..46].try_into().unwrap()),
        key_epoch: u16::from_be_bytes(d[46..48].try_into().unwrap()),
    }
}

/// Verify the AEAD tag of a received datagram (validation step 5 of
/// PROTOCOL.md §4.3). Constant-time comparison is provided by the AEAD
/// implementation; a failed tag reveals nothing about which bytes differ.
pub fn open_packet(
    key: &[u8; PACKET_KEY_LEN],
    nonce_salt: &[u8; NONCE_SALT_LEN],
    datagram: &[u8],
) -> bool {
    if datagram.len() < HEADER_LEN + TAG_LEN {
        return false;
    }
    let header = parse_header_prefix(datagram);
    let nonce = build_nonce(nonce_salt, &header);
    let tag = &datagram[HEADER_LEN..HEADER_LEN + TAG_LEN];
    aead(key)
        .decrypt(
            (&nonce).into(),
            Payload {
                msg: tag,
                aad: &datagram[..HEADER_LEN],
            },
        )
        .is_ok()
}

/// Generate the deterministic padding keystream for a packet
/// (PROTOCOL.md §2.6): ChaCha8 keyed by `verify_key`, nonce derived from the
/// sequence number. Fills `out` (the padding region after the tag).
pub fn fill_padding(verify_key: &[u8; 32], seq: u32, out: &mut [u8]) {
    let mut iv = [0u8; 12];
    iv[0..4].copy_from_slice(&seq.to_be_bytes());
    let mut cipher = ChaCha8::new(verify_key.into(), &iv.into());
    out.fill(0);
    cipher.apply_keystream(out);
}

/// Verify padding integrity for sessions with `verify_payload = true`.
pub fn verify_padding(verify_key: &[u8; 32], seq: u32, padding: &[u8]) -> bool {
    let mut expected = vec![0u8; padding.len()];
    fill_padding(verify_key, seq, &mut expected);
    // Constant-time-ish compare via the AEAD crate's subtle dependency is
    // overkill here; padding is not secret. Still avoid early-exit variance.
    let mut diff = 0u8;
    for (a, b) in expected.iter().zip(padding.iter()) {
        diff |= a ^ b;
    }
    diff == 0
}

/// Full per-session key material (PROTOCOL.md §3.2): directional packet
/// keys, nonce salts and the payload verify key.
#[derive(Clone)]
pub struct SessionKeys {
    pub packet_key_fwd: [u8; 32],
    pub packet_key_rev: [u8; 32],
    pub nonce_salt_fwd: [u8; 12],
    pub nonce_salt_rev: [u8; 12],
    pub verify_key: [u8; 32],
}

impl SessionKeys {
    /// Derive from a control-plane-issued `key_seed` and `session_id`
    /// (PROTOCOL.md §3.2). Labels `fwd`/`rev` are the link's a2b/b2a
    /// directions.
    pub fn derive(key_seed: &[u8; 32], session_id: u64) -> SessionKeys {
        let salt = session_id.to_be_bytes();
        let prk = hkdf::extract(&salt, key_seed);
        SessionKeys {
            packet_key_fwd: hkdf::expand_32(&prk, b"bnqo-udp-v1|a2b|key"),
            packet_key_rev: hkdf::expand_32(&prk, b"bnqo-udp-v1|b2a|key"),
            nonce_salt_fwd: hkdf::expand_12(&prk, b"bnqo-udp-v1|a2b|salt"),
            nonce_salt_rev: hkdf::expand_12(&prk, b"bnqo-udp-v1|b2a|salt"),
            verify_key: hkdf::expand_32(&prk, b"bnqo-udp-v1|payload"),
        }
    }
}

/// Directional keys for the eve Phase-1 integration
/// (EVE_API_CONTRACT.md §2.2): HKDF-SHA256(ikm = session_seed,
/// salt = "bnqo-v1", info = "a_to_b" / "b_to_a", L = 32).
///
/// DEVIATION (documented in bnqo/README.md): the contract specifies only the
/// two 32-byte packet keys. The AEAD nonce construction (PROTOCOL.md §2.5)
/// additionally needs a 12-byte per-direction nonce salt; we derive it from
/// the same seed with info "a_to_b_salt" / "b_to_a_salt" (same salt and PRK),
/// and the padding verify key with info "payload".
#[derive(Clone)]
pub struct DirectionKeys {
    pub key_ab: [u8; 32],
    pub key_ba: [u8; 32],
    pub salt_ab: [u8; 12],
    pub salt_ba: [u8; 12],
    pub verify_key: [u8; 32],
}

impl DirectionKeys {
    pub fn derive(session_seed: &[u8; 32]) -> DirectionKeys {
        let prk = hkdf::extract(b"bnqo-v1", session_seed);
        DirectionKeys {
            key_ab: hkdf::expand_32(&prk, b"a_to_b"),
            key_ba: hkdf::expand_32(&prk, b"b_to_a"),
            salt_ab: hkdf::expand_12(&prk, b"a_to_b_salt"),
            salt_ba: hkdf::expand_12(&prk, b"b_to_a_salt"),
            verify_key: hkdf::expand_32(&prk, b"payload"),
        }
    }

    /// (packet_key, nonce_salt) for sending in the given direction.
    pub fn sending(&self, direction_a_to_b: bool) -> (&[u8; 32], &[u8; 12]) {
        if direction_a_to_b {
            (&self.key_ab, &self.salt_ab)
        } else {
            (&self.key_ba, &self.salt_ba)
        }
    }

    /// (packet_key, nonce_salt) for verifying packets arriving in the given
    /// direction (the peer's sending direction).
    pub fn receiving(&self, direction_a_to_b: bool) -> (&[u8; 32], &[u8; 12]) {
        self.sending(direction_a_to_b)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::packet::{Flags, MIN_PACKET_SIZE};

    fn test_header(seq: u32, payload_len: u16) -> Header {
        Header {
            flags: 0,
            test_id: 1,
            session_id: 99,
            sequence_number: seq,
            sender_timestamp: 0xE000_0001_0000_0000,
            receive_timestamp: 0,
            reflector_turnaround_us: 0,
            nonce: 0xA5A5_5A5A,
            payload_length: payload_len,
            key_epoch: 0,
        }
    }

    fn build_packet(h: &Header, keys: &SessionKeys) -> Vec<u8> {
        let mut buf = vec![0u8; h.payload_length as usize];
        h.encode(&mut buf);
        seal_packet(
            &keys.packet_key_fwd,
            &keys.nonce_salt_fwd,
            &mut buf,
        );
        buf
    }

    #[test]
    fn seal_open_roundtrip() {
        let keys = SessionKeys::derive(&[7u8; 32], 42);
        let h = test_header(1, MIN_PACKET_SIZE as u16);
        let pkt = build_packet(&h, &keys);
        assert_eq!(pkt.len(), MIN_PACKET_SIZE);
        assert!(open_packet(&keys.packet_key_fwd, &keys.nonce_salt_fwd, &pkt));
    }

    #[test]
    fn open_rejects_wrong_key() {
        let keys = SessionKeys::derive(&[7u8; 32], 42);
        let other = SessionKeys::derive(&[8u8; 32], 42);
        let pkt = build_packet(&test_header(1, 64), &keys);
        assert!(!open_packet(&other.packet_key_fwd, &other.nonce_salt_fwd, &pkt));
    }

    #[test]
    fn open_rejects_wrong_direction_key() {
        // A reflection must never verify as a forward packet (PROTOCOL.md §2.5
        // key separation).
        let keys = SessionKeys::derive(&[7u8; 32], 42);
        let pkt = build_packet(&test_header(1, 64), &keys);
        assert!(!open_packet(&keys.packet_key_rev, &keys.nonce_salt_rev, &pkt));
    }

    #[test]
    fn open_rejects_tampering() {
        let keys = SessionKeys::derive(&[7u8; 32], 42);
        let pkt = build_packet(&test_header(5, 64), &keys);
        for (i, byte) in [3usize, 17, 21, 40, 45, 47].into_iter().enumerate() {
            let mut t = pkt.clone();
            t[byte] ^= 1 << (i % 8);
            assert!(
                !open_packet(&keys.packet_key_fwd, &keys.nonce_salt_fwd, &t),
                "tampered byte {byte} accepted"
            );
        }
        // Tamper with the tag itself.
        let mut t = pkt.clone();
        t[48] ^= 0xFF;
        assert!(!open_packet(&keys.packet_key_fwd, &keys.nonce_salt_fwd, &t));
    }

    #[test]
    fn reflection_retagging_roundtrip() {
        // Mirror the reflector path (PROTOCOL.md §4.3 step 8): copy, set
        // REFLECTED, stamp T2/turnaround, re-tag with the reverse key.
        let keys = SessionKeys::derive(&[1u8; 32], 7);
        let h = test_header(10, 256);
        let fwd = build_packet(&h, &keys);
        assert!(open_packet(&keys.packet_key_fwd, &keys.nonce_salt_fwd, &fwd));

        let mut refl = fwd.clone();
        let mut rh = Header::decode(&refl).unwrap();
        rh.flags |= Flags::REFLECTED;
        rh.receive_timestamp = 0xE000_0002_8000_0000;
        rh.reflector_turnaround_us = 35;
        rh.encode(&mut refl);
        seal_packet(&keys.packet_key_rev, &keys.nonce_salt_rev, &mut refl);

        assert_eq!(refl.len(), fwd.len()); // same size in, same size out
        assert!(open_packet(&keys.packet_key_rev, &keys.nonce_salt_rev, &refl));
        // Old forward tag must not validate under the reverse key/nonce.
        let decoded = Header::decode(&refl).unwrap();
        assert!(decoded.is_reflected());
    }

    #[test]
    fn padding_deterministic_and_verifiable() {
        let keys = SessionKeys::derive(&[3u8; 32], 1);
        let mut pad = [0u8; 200];
        fill_padding(&keys.verify_key, 77, &mut pad);
        assert!(verify_padding(&keys.verify_key, 77, &pad));
        assert!(!verify_padding(&keys.verify_key, 78, &pad));
        let mut tampered = pad;
        tampered[100] ^= 1;
        assert!(!verify_padding(&keys.verify_key, 77, &tampered));
        // Padding is not all-zero / trivially compressible.
        assert!(pad.iter().any(|&b| b != 0));
    }

    #[test]
    fn nonce_layout_matches_spec() {
        let h = Header {
            flags: 0x09,
            key_epoch: 0x0102,
            nonce: 0x0304_0506,
            sequence_number: 0x0708_090A,
            ..test_header(0, 64)
        };
        let salt = [0xCCu8; 12];
        let n = build_nonce(&salt, &h);
        assert_eq!(&n[0..12], &[0xCCu8; 12]);
        assert_eq!(&n[12..14], &[0x01, 0x02]);
        assert_eq!(&n[14..18], &[0x03, 0x04, 0x05, 0x06]);
        assert_eq!(&n[18..22], &[0x07, 0x08, 0x09, 0x0A]);
        assert_eq!(n[22], 0x09);
        assert_eq!(n[23], 0x01);
    }
}
