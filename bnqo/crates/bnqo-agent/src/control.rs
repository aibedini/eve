//! Control-plane state: signed config application with anti-rollback
//! (EVE_API_CONTRACT.md §2.2, PROTOCOL.md §7.4) and job validation (§2.3).

use std::collections::{HashSet, VecDeque};
use std::path::Path;

use bnqo_proto::canonjson;
use serde_json::Value;

use crate::model::{parse_iso, CpConfig, ValidatedJob, JOB_TYPE_WHITELIST};

const EXECUTED_JOB_CACHE_CAP: usize = 10_000;

#[derive(Debug, thiserror::Error)]
pub enum ControlError {
    #[error("invalid config signature")]
    BadSignature,
    #[error("config rollback: version {got} <= applied {applied}")]
    Rollback { got: u64, applied: u64 },
    #[error("malformed config: {0}")]
    Malformed(String),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum JobRejection {
    #[error("job signature invalid")]
    BadSignature,
    #[error("job expired")]
    Expired,
    #[error("job type not whitelisted")]
    UnknownType,
    #[error("job already executed")]
    Duplicate,
    #[error("job malformed")]
    Malformed,
}

pub enum ConfigOutcome {
    Applied(CpConfig),
    Unchanged,
}

pub struct ControlState {
    pub applied_version: u64,
    pub config: Option<CpConfig>,
    executed_jobs: HashSet<String>,
    executed_order: VecDeque<String>,
}

impl ControlState {
    pub fn new() -> Self {
        ControlState {
            applied_version: 0,
            config: None,
            executed_jobs: HashSet::new(),
            executed_order: VecDeque::new(),
        }
    }

    pub fn executed_jobs(&self) -> &HashSet<String> {
        &self.executed_jobs
    }

    pub fn mark_executed(&mut self, job_id: &str) {
        if self.executed_jobs.insert(job_id.to_string()) {
            self.executed_order.push_back(job_id.to_string());
            while self.executed_order.len() > EXECUTED_JOB_CACHE_CAP {
                if let Some(old) = self.executed_order.pop_front() {
                    self.executed_jobs.remove(&old);
                }
            }
        }
    }

    /// Restore executed-job cache persisted across restarts (job anti-replay,
    /// PROTOCOL.md §8).
    pub fn load_executed(&mut self, state_dir: &Path) {
        let path = state_dir.join("executed_jobs.json");
        if let Ok(raw) = std::fs::read_to_string(&path) {
            if let Ok(ids) = serde_json::from_str::<Vec<String>>(&raw) {
                for id in ids {
                    self.mark_executed(&id);
                }
            }
        }
    }

    pub fn persist_executed(&self, state_dir: &Path) {
        let ids: Vec<&String> = self.executed_order.iter().collect();
        if let Ok(raw) = serde_json::to_string(&ids) {
            let _ = std::fs::write(state_dir.join("executed_jobs.json"), raw);
        }
    }
}

impl Default for ControlState {
    fn default() -> Self {
        Self::new()
    }
}

/// Verify a fetched config body and apply it if it is newer than the applied
/// version. The previous config stays active on any failure (PROTOCOL.md
/// §7.4 "invalid config never replaces valid one").
pub fn apply_config(
    state: &mut ControlState,
    raw: &Value,
    cp_pubkey: &[u8; 32],
) -> Result<ConfigOutcome, ControlError> {
    canonjson::verify_signed_object(raw, cp_pubkey).map_err(|_| ControlError::BadSignature)?;
    let version = raw
        .get("config_version")
        .and_then(Value::as_u64)
        .ok_or_else(|| ControlError::Malformed("missing config_version".into()))?;
    if version < state.applied_version {
        return Err(ControlError::Rollback {
            got: version,
            applied: state.applied_version,
        });
    }
    if version == state.applied_version && state.config.is_some() {
        return Ok(ConfigOutcome::Unchanged);
    }
    let mut stripped = raw.clone();
    if let Value::Object(ref mut m) = stripped {
        m.remove("signature");
    }
    let config: CpConfig = serde_json::from_value(stripped)
        .map_err(|e| ControlError::Malformed(e.to_string()))?;
    state.applied_version = version;
    state.config = Some(config.clone());
    Ok(ConfigOutcome::Applied(config))
}

/// Persist the last-good config so the agent survives restarts during
/// control-plane outages (§2 "agents run autonomously with last valid
/// signed config").
pub fn persist_config(state_dir: &Path, raw: &Value) -> std::io::Result<()> {
    std::fs::create_dir_all(state_dir)?;
    std::fs::write(
        state_dir.join("last_config.json"),
        serde_json::to_vec(raw).unwrap_or_default(),
    )
}

/// Load and apply the persisted last-good config at startup.
pub fn restore_config(
    state: &mut ControlState,
    state_dir: &Path,
    cp_pubkey: &[u8; 32],
) {
    let path = state_dir.join("last_config.json");
    let Ok(raw) = std::fs::read(&path) else { return };
    let Ok(value) = serde_json::from_slice::<Value>(&raw) else {
        return;
    };
    match apply_config(state, &value, cp_pubkey) {
        Ok(ConfigOutcome::Applied(c)) => {
            tracing::info!(version = c.config_version, "restored last-good config")
        }
        Ok(ConfigOutcome::Unchanged) => {}
        Err(e) => tracing::warn!(error = %e, "stored config rejected, starting unconfigured"),
    }
}

/// Validate one job object (§2.3 + PROTOCOL.md §12 checks):
/// signature, expiry, type whitelist, duplicate.
pub fn validate_job(
    state: &ControlState,
    raw: &Value,
    cp_pubkey: &[u8; 32],
    now: chrono::DateTime<chrono::Utc>,
) -> Result<ValidatedJob, JobRejection> {
    canonjson::verify_signed_object(raw, cp_pubkey).map_err(|_| JobRejection::BadSignature)?;

    let job_id = raw
        .get("job_id")
        .and_then(Value::as_str)
        .ok_or(JobRejection::Malformed)?
        .to_string();
    let job_type = raw
        .get("type")
        .and_then(Value::as_str)
        .ok_or(JobRejection::Malformed)?
        .to_string();
    if !JOB_TYPE_WHITELIST.contains(&job_type.as_str()) {
        return Err(JobRejection::UnknownType);
    }
    let expires_at = raw
        .get("expires_at")
        .and_then(Value::as_str)
        .and_then(parse_iso)
        .ok_or(JobRejection::Malformed)?;
    if now > expires_at {
        return Err(JobRejection::Expired);
    }
    if state.executed_jobs.contains(&job_id) {
        return Err(JobRejection::Duplicate);
    }
    Ok(ValidatedJob {
        job_id,
        job_type,
        params: raw.get("params").cloned().unwrap_or(Value::Null),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use bnqo_proto::ed25519 as ed;
    use serde_json::json;

    fn test_key() -> (ed25519_dalek::SigningKey, [u8; 32]) {
        let sk = ed::signing_key_from_seed(&core::array::from_fn(|i| (32 + i) as u8));
        let pk = sk.verifying_key().to_bytes();
        (sk, pk)
    }

    fn sign_obj(sk: &ed25519_dalek::SigningKey, mut obj: Value) -> Value {
        let canon = canonjson::canonical_json(&obj);
        let sig = ed::sign(sk, canon.as_bytes());
        obj["signature"] = json!(ed::sig_to_b64(&sig));
        obj
    }

    fn config_body(version: u64) -> Value {
        json!({
            "config_version": version,
            "agent": {"name": "de-fra-1", "role": "outside"},
            "links": [{
                "link_id": 1,
                "name": "IR-DE",
                "peer": {"name": "ir-tb-1", "address": "198.51.100.5", "port": 44818},
                "direction": "b_to_a",
                "session_seed": "00".repeat(32),
                "profile": {"interval_ms": 200, "packet_size": 256, "window_sec": 30}
            }]
        })
    }

    #[test]
    fn config_accept_then_rollback_rejected() {
        let (sk, pk) = test_key();
        let mut state = ControlState::new();

        let v7 = sign_obj(&sk, config_body(7));
        match apply_config(&mut state, &v7, &pk).unwrap() {
            ConfigOutcome::Applied(c) => assert_eq!(c.config_version, 7),
            _ => panic!("expected applied"),
        }
        assert_eq!(state.applied_version, 7);
        assert_eq!(state.config.as_ref().unwrap().links.len(), 1);

        // Same version: unchanged, not an error.
        let v7again = sign_obj(&sk, config_body(7));
        assert!(matches!(
            apply_config(&mut state, &v7again, &pk).unwrap(),
            ConfigOutcome::Unchanged
        ));

        // Older version: rollback rejected, current config kept.
        let v6 = sign_obj(&sk, config_body(6));
        assert!(matches!(
            apply_config(&mut state, &v6, &pk),
            Err(ControlError::Rollback { got: 6, applied: 7 })
        ));
        assert_eq!(state.applied_version, 7);

        // Tampered body: signature failure, config kept.
        let mut tampered = v7.clone();
        tampered["links"] = json!([]);
        assert!(matches!(
            apply_config(&mut state, &tampered, &pk),
            Err(ControlError::BadSignature)
        ));
        assert_eq!(state.config.as_ref().unwrap().links.len(), 1);

        // Newer version applies.
        let v8 = sign_obj(&sk, config_body(8));
        assert!(matches!(
            apply_config(&mut state, &v8, &pk).unwrap(),
            ConfigOutcome::Applied(_)
        ));
        assert_eq!(state.applied_version, 8);
    }

    /// Cross-check with the Python-generated signed config vector (see
    /// bnqo-proto canonjson tests): seed = bytes 32..64.
    #[test]
    fn python_signed_config_accepted() {
        let (_sk, pk) = test_key();
        let signed = json!({
            "config_version": 7,
            "agent": {"name": "de-fra-1", "role": "outside"},
            "links": [],
            "signature": "rpplwJAOduZjhO4c+XdOa04Gv64DmQHzFPeoWxk63Bc6quO8drjfuRfyO5WxGPaA/xwwTynOTxIK3CtqSuASDA=="
        });
        let mut state = ControlState::new();
        assert!(matches!(
            apply_config(&mut state, &signed, &pk).unwrap(),
            ConfigOutcome::Applied(_)
        ));
    }

    #[test]
    fn job_validation_matrix() {
        let (sk, pk) = test_key();
        let state = ControlState::new();
        let now = chrono::Utc::now();
        let future = (now + chrono::Duration::hours(1))
            .format("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let past = (now - chrono::Duration::hours(1))
            .format("%Y-%m-%dT%H:%M:%SZ")
            .to_string();

        let good = sign_obj(
            &sk,
            json!({"job_id": "job_1", "type": "RUN_MTR",
                   "params": {"link_id": 1, "target": "198.51.100.5", "cycles": 10},
                   "expires_at": future, "config_version": 7}),
        );
        let job = validate_job(&state, &good, &pk, now).unwrap();
        assert_eq!(job.job_id, "job_1");
        assert_eq!(job.job_type, "RUN_MTR");

        // Expired.
        let expired = sign_obj(
            &sk,
            json!({"job_id": "job_2", "type": "RUN_MTR", "params": {}, "expires_at": past}),
        );
        assert_eq!(
            validate_job(&state, &expired, &pk, now).unwrap_err(),
            JobRejection::Expired
        );

        // Unknown type (closed set, §12).
        let unknown = sign_obj(
            &sk,
            json!({"job_id": "job_3", "type": "RUN_SHELL", "params": {}, "expires_at": future}),
        );
        assert_eq!(
            validate_job(&state, &unknown, &pk, now).unwrap_err(),
            JobRejection::UnknownType
        );

        // Unsigned / bad signature.
        let unsigned = json!({"job_id": "job_4", "type": "RUN_MTR", "params": {}, "expires_at": future});
        assert_eq!(
            validate_job(&state, &unsigned, &pk, now).unwrap_err(),
            JobRejection::BadSignature
        );

        // Duplicate.
        let mut state2 = ControlState::new();
        state2.mark_executed("job_1");
        assert_eq!(
            validate_job(&state2, &good, &pk, now).unwrap_err(),
            JobRejection::Duplicate
        );
    }

    #[test]
    fn executed_cache_caps_at_10k() {
        let mut state = ControlState::new();
        for i in 0..10_500 {
            state.mark_executed(&format!("job_{i}"));
        }
        assert_eq!(state.executed_jobs().len(), 10_000);
        assert!(!state.executed_jobs().contains("job_0"));
        assert!(state.executed_jobs().contains("job_10499"));
    }

    #[test]
    fn persist_restore_last_good_config() {
        let (sk, pk) = test_key();
        let tmp = tempfile::tempdir().unwrap();
        let v5 = sign_obj(&sk, config_body(5));

        let mut state = ControlState::new();
        assert!(matches!(
            apply_config(&mut state, &v5, &pk).unwrap(),
            ConfigOutcome::Applied(_)
        ));
        persist_config(tmp.path(), &v5).unwrap();

        let mut restored = ControlState::new();
        restore_config(&mut restored, tmp.path(), &pk);
        assert_eq!(restored.applied_version, 5);
        assert!(restored.config.is_some());
    }
}
