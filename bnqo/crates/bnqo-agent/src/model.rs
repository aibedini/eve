//! Data model shared across the agent: TOML config file, control-plane
//! config (EVE_API_CONTRACT.md §2.2), jobs (§2.3) and the report batch
//! (§2.4).

use serde::{Deserialize, Serialize};

// ---------- agent.toml ----------

#[derive(Debug, Clone, Deserialize)]
pub struct AgentToml {
    pub eve_url: String,
    pub name: String,
    #[serde(default = "default_state_dir")]
    pub state_dir: String,
    #[serde(default = "default_bind_port")]
    pub bind_port: u16,
    #[serde(default = "default_role")]
    pub role: String,
    /// Enroll token; the BNQO_ENROLL_TOKEN env var takes precedence.
    pub enroll_token: Option<String>,
    #[serde(default = "default_poll")]
    pub control_poll_interval_sec: u64,
    #[serde(default = "default_report_flush")]
    pub report_flush_interval_sec: u64,
    #[serde(default = "default_skew")]
    pub max_clock_skew_sec: u64,
    #[serde(default = "default_wal_quota")]
    pub wal_quota_bytes: u64,
    /// fsync every WAL append (crash-safe, slower) vs. on segment roll.
    #[serde(default = "default_true")]
    pub wal_fsync: bool,
}

fn default_state_dir() -> String {
    "/var/lib/bnqo".into()
}
fn default_bind_port() -> u16 {
    44818
}
fn default_role() -> String {
    "outside".into()
}
fn default_poll() -> u64 {
    10
}
fn default_report_flush() -> u64 {
    10
}
fn default_skew() -> u64 {
    300
}
fn default_wal_quota() -> u64 {
    64 * 1024 * 1024
}
fn default_true() -> bool {
    true
}

// ---------- control-plane config (§2.2) ----------

#[derive(Debug, Clone, Deserialize)]
pub struct CpConfig {
    pub config_version: u64,
    #[serde(default)]
    pub links: Vec<LinkConfig>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct LinkConfig {
    pub link_id: u64,
    #[serde(default)]
    pub name: String,
    pub peer: PeerInfo,
    /// This agent's sending direction on the link: "a_to_b" or "b_to_a".
    pub direction: String,
    pub session_seed: String, // 64 hex chars
    #[serde(default)]
    pub profile: Profile,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PeerInfo {
    #[serde(default)]
    pub name: String,
    pub address: String,
    #[serde(default = "default_bind_port")]
    pub port: u16,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Profile {
    #[serde(default = "default_interval")]
    pub interval_ms: u64,
    #[serde(default = "default_packet_size")]
    pub packet_size: usize,
    #[serde(default = "default_window")]
    pub window_sec: u64,
    #[serde(default)]
    pub icmp_enabled: bool,
    #[serde(default = "default_icmp_count")]
    pub icmp_count: u32,
    #[serde(default = "default_icmp_interval")]
    pub icmp_interval_sec: u64,
    #[serde(default)]
    pub service_targets: Vec<ServiceTargetCfg>,
}

impl Default for Profile {
    fn default() -> Self {
        Profile {
            interval_ms: default_interval(),
            packet_size: default_packet_size(),
            window_sec: default_window(),
            icmp_enabled: false,
            icmp_count: default_icmp_count(),
            icmp_interval_sec: default_icmp_interval(),
            service_targets: Vec::new(),
        }
    }
}

fn default_interval() -> u64 {
    200
}
fn default_packet_size() -> usize {
    256
}
fn default_window() -> u64 {
    30
}
fn default_icmp_count() -> u32 {
    5
}
fn default_icmp_interval() -> u64 {
    30
}

#[derive(Debug, Clone, Deserialize)]
pub struct ServiceTargetCfg {
    pub name: String,
    pub host: String,
    pub port: u16,
    #[serde(default)]
    pub tls: bool,
    #[serde(default = "default_icmp_interval")]
    pub interval_sec: u64,
}

// ---------- jobs (§2.3) ----------

pub const JOB_TYPE_WHITELIST: &[&str] = &[
    "RUN_MTR",
    "RUN_ICMP_PROBE",
    "RUN_TCP_PROBE",
    "COLLECT_HOST_SNAPSHOT",
];

#[derive(Debug, Clone)]
pub struct ValidatedJob {
    pub job_id: String,
    pub job_type: String,
    pub params: serde_json::Value,
}

// ---------- report batch (§2.4) ----------

#[derive(Debug, Clone, Serialize)]
pub struct MeasurementRec {
    pub link_id: u64,
    pub direction: String,
    pub window_start: String,
    pub window_end: String,
    pub sent: u64,
    pub received: u64,
    pub loss_pct: f64,
    pub rtt_min_ms: Option<f64>,
    pub rtt_avg_ms: Option<f64>,
    pub rtt_p95_ms: Option<f64>,
    pub rtt_max_ms: Option<f64>,
    pub owd_ms: Option<f64>,
    pub clock_quality: String,
    pub jitter_ms: Option<f64>,
    pub reordered: u32,
    pub duplicated: u32,
    pub corrupted: u32,
    pub burst_max: u32,
}

#[derive(Debug, Clone, Serialize)]
pub struct IcmpRec {
    pub link_id: u64,
    pub direction: String,
    pub sent: u32,
    pub received: u32,
    pub loss_pct: f64,
    pub rtt_avg_ms: Option<f64>,
    pub rtt_p95_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_class: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ServiceProbeRec {
    pub link_id: u64,
    pub target_name: String,
    pub ok: bool,
    pub tcp_ms: Option<f64>,
    pub tls_ms: Option<f64>,
    pub http_status: Option<u16>,
    pub error_class: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MtrResultRec {
    pub job_id: String,
    pub link_id: u64,
    pub direction: String,
    pub route_hash: String,
    pub destination_reached: bool,
    pub hops: Vec<MtrHop>,
}

#[derive(Debug, Clone, Serialize)]
pub struct MtrHop {
    pub hop: u32,
    pub address: String,
    pub loss_pct: f64,
    pub rtt_avg_ms: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct JobAckRec {
    pub job_id: String,
    pub status: String, // done | failed
    pub error_class: Option<String>,
}

/// Items the batcher accumulates between report flushes.
#[derive(Debug, Clone)]
pub enum TelemetryItem {
    Measurement(MeasurementRec),
    Icmp(IcmpRec),
    ServiceProbe(ServiceProbeRec),
    MtrResult(MtrResultRec),
    JobAck(JobAckRec),
}

/// UTC now formatted per contract: ISO-8601 with Z suffix, seconds precision.
pub fn utc_now_iso() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

/// Parse the contract's ISO-8601 UTC timestamps.
pub fn parse_iso(s: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    chrono::DateTime::parse_from_rfc3339(s)
        .ok()
        .map(|d| d.with_timezone(&chrono::Utc))
}
