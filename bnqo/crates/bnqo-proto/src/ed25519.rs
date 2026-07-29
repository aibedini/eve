//! Ed25519 helpers for the eve control-plane signature scheme
//! (EVE_API_CONTRACT.md §1).
//!
//! - CP signs configs/jobs; the agent verifies with the pinned CP public key.
//! - The agent authenticates API calls by signing `"<unix_ts>\n" + body`
//!   with its own key (X-BNQO-Signature header).

use base64::Engine;
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use rand::rngs::OsRng;

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::STANDARD;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SignatureError {
    #[error("invalid public key bytes")]
    BadPublicKey,
    #[error("invalid signature bytes")]
    BadSignature,
    #[error("signature verification failed")]
    VerifyFailed,
    #[error("invalid base64: {0}")]
    BadBase64(String),
}

/// Generate a fresh agent keypair. Returns (signing key, raw 32-byte pubkey).
pub fn generate_keypair() -> (SigningKey, [u8; 32]) {
    let sk = SigningKey::generate(&mut OsRng);
    let pk = sk.verifying_key().to_bytes();
    (sk, pk)
}

/// Restore a signing key from its raw 32-byte seed.
pub fn signing_key_from_seed(seed: &[u8; 32]) -> SigningKey {
    SigningKey::from_bytes(seed)
}

pub fn sign(sk: &SigningKey, msg: &[u8]) -> [u8; 64] {
    sk.sign(msg).to_bytes()
}

pub fn verify(pk: &[u8; 32], msg: &[u8], sig: &[u8; 64]) -> Result<(), SignatureError> {
    let vk = VerifyingKey::from_bytes(pk).map_err(|_| SignatureError::BadPublicKey)?;
    let signature = Signature::from_bytes(sig);
    vk.verify(msg, &signature)
        .map_err(|_| SignatureError::VerifyFailed)
}

pub fn pubkey_to_b64(pk: &[u8; 32]) -> String {
    B64.encode(pk)
}

pub fn pubkey_from_b64(s: &str) -> Result<[u8; 32], SignatureError> {
    let raw = B64
        .decode(s.trim())
        .map_err(|e| SignatureError::BadBase64(e.to_string()))?;
    raw.try_into().map_err(|_| SignatureError::BadPublicKey)
}

pub fn sig_to_b64(sig: &[u8; 64]) -> String {
    B64.encode(sig)
}

pub fn sig_from_b64(s: &str) -> Result<[u8; 64], SignatureError> {
    let raw = B64
        .decode(s.trim())
        .map_err(|e| SignatureError::BadBase64(e.to_string()))?;
    raw.try_into().map_err(|_| SignatureError::BadSignature)
}

/// Build the message the agent signs for API authentication
/// (EVE_API_CONTRACT.md §1): `"<timestamp>\n" + raw body bytes`.
pub fn api_auth_message(timestamp_unix_secs: u64, body: &[u8]) -> Vec<u8> {
    let mut m = format!("{timestamp_unix_secs}\n").into_bytes();
    m.extend_from_slice(body);
    m
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sign_verify_roundtrip() {
        let (sk, pk) = generate_keypair();
        let msg = b"1753776000\n{\"agent_seq\":1}";
        let sig = sign(&sk, msg);
        assert!(verify(&pk, msg, &sig).is_ok());
        // Wrong message / wrong key / tampered sig all fail.
        assert!(verify(&pk, b"other", &sig).is_err());
        let (_, pk2) = generate_keypair();
        assert!(verify(&pk2, msg, &sig).is_err());
        let mut bad = sig;
        bad[0] ^= 1;
        assert!(verify(&pk, msg, &bad).is_err());
    }

    #[test]
    fn api_auth_message_layout() {
        assert_eq!(api_auth_message(42, b"abc"), b"42\nabc");
        assert_eq!(api_auth_message(42, b""), b"42\n");
    }

    /// Cross-implementation vector produced by Python `cryptography`
    /// (Ed25519, seed = bytes 32..64):
    ///   pub  = Kay64UG8yvCyLhqU000LxzYeUm0L/hLIl5S8kyKWbdc=
    ///   msg  = canonical JSON test vector (see canonjson tests)
    ///   sig  = MwlypngVefxnr3jt8ulvE5oWWHBu7uwi4o+rEx9V7NhPlSl7idCcs6+L5w2VDsoN1QpV3cnGOmY6xucn1xQWAA==
    #[test]
    fn verify_python_signature() {
        let sk = signing_key_from_seed(&core::array::from_fn(|i| (32 + i) as u8));
        let pk = sk.verifying_key().to_bytes();
        assert_eq!(
            pubkey_to_b64(&pk),
            "Kay64UG8yvCyLhqU000LxzYeUm0L/hLIl5S8kyKWbdc="
        );
        let msg = "{\"a\":{\"nested\":{\"x\":true,\"y\":false}},\"int\":42,\"num\":1.33,\"str\":\"IR-DE\",\"z\":[3,1.5,{\"a\":null,\"b\":\"ünicode\u{2713}\"}]}";
        let sig = sig_from_b64(
            "MwlypngVefxnr3jt8ulvE5oWWHBu7uwi4o+rEx9V7NhPlSl7idCcs6+L5w2VDsoN1QpV3cnGOmY6xucn1xQWAA==",
        )
        .unwrap();
        verify(&pk, msg.as_bytes(), &sig).unwrap();
    }
}
