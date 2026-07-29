//! Agent identity: Ed25519 keypair, enrollment (EVE_API_CONTRACT.md §2.1),
//! and persistence of the identity file (mode 0600).

use std::path::{Path, PathBuf};

use bnqo_proto::ed25519;
use ed25519_dalek::SigningKey;
use serde::{Deserialize, Serialize};

use crate::http::{CpHttp, HttpError};
use crate::model::AgentToml;

#[derive(Debug, thiserror::Error)]
pub enum IdentityError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("http: {0}")]
    Http(#[from] HttpError),
    #[error("no enroll token: set BNQO_ENROLL_TOKEN or enroll_token in the config")]
    NoEnrollToken,
    #[error("enroll response missing field: {0}")]
    BadResponse(&'static str),
    #[error("bad key material: {0}")]
    BadKey(String),
}

#[derive(Clone)]
pub struct Identity {
    pub agent_id: u64,
    pub token: String,
    pub cp_pubkey: [u8; 32],
    pub signing: SigningKey,
}

#[derive(Serialize, Deserialize)]
struct IdentityFile {
    agent_id: u64,
    agent_token: String,
    cp_pubkey_b64: String,
    seed_b64: String,
}

pub fn identity_path(state_dir: &Path) -> PathBuf {
    state_dir.join("identity.json")
}

pub fn load(state_dir: &Path) -> Result<Option<Identity>, IdentityError> {
    let path = identity_path(state_dir);
    if !path.exists() {
        return Ok(None);
    }
    let raw = std::fs::read_to_string(&path)?;
    let f: IdentityFile = serde_json::from_str(&raw)?;
    let cp_pubkey = ed25519::pubkey_from_b64(&f.cp_pubkey_b64)
        .map_err(|e| IdentityError::BadKey(e.to_string()))?;
    let seed: [u8; 32] = base64::Engine::decode(
        &base64::engine::general_purpose::STANDARD,
        f.seed_b64.trim(),
    )
    .map_err(|e| IdentityError::BadKey(e.to_string()))?
    .try_into()
    .map_err(|_| IdentityError::BadKey("seed must be 32 bytes".into()))?;
    Ok(Some(Identity {
        agent_id: f.agent_id,
        token: f.agent_token,
        cp_pubkey,
        signing: ed25519::signing_key_from_seed(&seed),
    }))
}

fn persist(state_dir: &Path, id: &Identity) -> Result<(), IdentityError> {
    std::fs::create_dir_all(state_dir)?;
    let f = IdentityFile {
        agent_id: id.agent_id,
        agent_token: id.token.clone(),
        cp_pubkey_b64: ed25519::pubkey_to_b64(&id.cp_pubkey),
        seed_b64: base64::Engine::encode(
            &base64::engine::general_purpose::STANDARD,
            id.signing.to_bytes(),
        ),
    };
    let path = identity_path(state_dir);
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(&f)?)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    }
    std::fs::rename(&tmp, &path)?;
    Ok(())
}

/// Load the identity, enrolling first when no identity file exists.
pub async fn load_or_enroll(cfg: &AgentToml) -> Result<Identity, IdentityError> {
    let state_dir = Path::new(&cfg.state_dir);
    if let Some(id) = load(state_dir)? {
        return Ok(id);
    }

    let token = std::env::var("BNQO_ENROLL_TOKEN")
        .ok()
        .filter(|t| !t.is_empty())
        .or_else(|| cfg.enroll_token.clone())
        .ok_or(IdentityError::NoEnrollToken)?;

    let (signing, pubkey) = ed25519::generate_keypair();
    let body = serde_json::json!({
        "enroll_token": token,
        "name": cfg.name,
        "role": cfg.role,
        "pubkey": ed25519::pubkey_to_b64(&pubkey),
        "port": cfg.bind_port,
        "version": env!("CARGO_PKG_VERSION"),
    });

    let http = CpHttp::unauthenticated(&cfg.eve_url)?;
    let resp = http
        .post_json("/api/bnqo/agent/enroll", &body)
        .await?;

    let agent_id = resp
        .get("agent_id")
        .and_then(|v| v.as_u64())
        .ok_or(IdentityError::BadResponse("agent_id"))?;
    let agent_token = resp
        .get("agent_token")
        .and_then(|v| v.as_str())
        .ok_or(IdentityError::BadResponse("agent_token"))?
        .to_string();
    let cp_pubkey = ed25519::pubkey_from_b64(
        resp.get("cp_pubkey")
            .and_then(|v| v.as_str())
            .ok_or(IdentityError::BadResponse("cp_pubkey"))?,
    )
    .map_err(|e| IdentityError::BadKey(e.to_string()))?;

    let id = Identity {
        agent_id,
        token: agent_token,
        cp_pubkey,
        signing,
    };
    persist(state_dir, &id)?;
    tracing::info!(agent_id, "enrolled and identity persisted (0600)");
    Ok(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn persist_and_reload_roundtrip() {
        let tmp = tempfile::tempdir().unwrap();
        let (sk, pk) = ed25519::generate_keypair();
        let id = Identity {
            agent_id: 7,
            token: "deadbeef".into(),
            cp_pubkey: pk,
            signing: sk,
        };
        persist(tmp.path(), &id).unwrap();
        let loaded = load(tmp.path()).unwrap().unwrap();
        assert_eq!(loaded.agent_id, 7);
        assert_eq!(loaded.token, "deadbeef");
        assert_eq!(loaded.cp_pubkey, pk);
        assert_eq!(
            loaded.signing.verifying_key().to_bytes(),
            id.signing.verifying_key().to_bytes()
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(identity_path(tmp.path()))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o600);
        }
    }

    #[test]
    fn load_missing_returns_none() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(load(tmp.path()).unwrap().is_none());
    }
}
