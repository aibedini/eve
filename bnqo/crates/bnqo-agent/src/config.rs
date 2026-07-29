//! agent.toml loading.

use crate::model::AgentToml;

pub fn load(path: &str) -> anyhow::Result<AgentToml> {
    let raw = std::fs::read_to_string(path)
        .map_err(|e| anyhow::anyhow!("cannot read {path}: {e}"))?;
    let cfg: AgentToml =
        toml::from_str(&raw).map_err(|e| anyhow::anyhow!("cannot parse {path}: {e}"))?;
    Ok(cfg)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn parses_minimal_config_with_defaults() {
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        writeln!(
            tmp,
            r#"
eve_url = "https://panel.example.com"
name = "de-fra-1"
"#
        )
        .unwrap();
        let cfg = load(tmp.path().to_str().unwrap()).unwrap();
        assert_eq!(cfg.eve_url, "https://panel.example.com");
        assert_eq!(cfg.bind_port, 44818);
        assert_eq!(cfg.state_dir, "/var/lib/bnqo");
        assert_eq!(cfg.control_poll_interval_sec, 10);
        assert_eq!(cfg.max_clock_skew_sec, 300);
        assert_eq!(cfg.wal_quota_bytes, 64 * 1024 * 1024);
        assert!(cfg.wal_fsync);
    }

    #[test]
    fn parses_full_config() {
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        writeln!(
            tmp,
            r#"
eve_url = "https://panel.example.com"
name = "ir-tb-1"
state_dir = "/tmp/bnqo-test"
bind_port = 45000
role = "iran"
enroll_token = "tok123"
control_poll_interval_sec = 15
report_flush_interval_sec = 20
wal_fsync = false
"#
        )
        .unwrap();
        let cfg = load(tmp.path().to_str().unwrap()).unwrap();
        assert_eq!(cfg.bind_port, 45000);
        assert_eq!(cfg.role, "iran");
        assert_eq!(cfg.enroll_token.as_deref(), Some("tok123"));
        assert_eq!(cfg.control_poll_interval_sec, 15);
        assert!(!cfg.wal_fsync);
    }
}
