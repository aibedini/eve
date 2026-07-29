//! Host metrics reader (contract §2.4 `host` object).
//!
//! Linux: /proc + /sys + chronyc/ntpq when available. Other platforms:
//! zeros with `clock_source: "unknown"` so host tests compile and run.

use serde::Serialize;

use crate::window::ClockEstimate;

#[derive(Debug, Clone, Serialize)]
pub struct HostMetrics {
    pub cpu_pct: f64,
    pub load1: f64,
    pub mem_pct: f64,
    pub disk_pct: f64,
    pub rx_drops: u64,
    pub tx_drops: u64,
    /// RetransSegs delta since the previous sample (0 on the first sample).
    pub tcp_retrans: u64,
    pub clock_source: String,
    pub clock_offset_ms: Option<f64>,
    pub clock_uncertainty_ms: Option<f64>,
    #[serde(skip)]
    pub clock_locked: bool,
}

impl Default for HostMetrics {
    fn default() -> Self {
        HostMetrics {
            cpu_pct: 0.0,
            load1: 0.0,
            mem_pct: 0.0,
            disk_pct: 0.0,
            rx_drops: 0,
            tx_drops: 0,
            tcp_retrans: 0,
            clock_source: "unknown".into(),
            clock_offset_ms: None,
            clock_uncertainty_ms: None,
            clock_locked: false,
        }
    }
}

impl HostMetrics {
    /// Feed the window aggregator's OWD gating (PROTOCOL.md §6.2).
    pub fn clock_estimate(&self, u_max_ms: f64) -> ClockEstimate {
        ClockEstimate {
            offset_ms: self.clock_offset_ms.unwrap_or(0.0),
            uncertainty_ms: self.clock_uncertainty_ms,
            locked: self.clock_locked,
            u_max_ms,
        }
    }
}

/// Stateful reader: CPU% and retransmits are computed as deltas between
/// samples.
#[derive(Default)]
pub struct HostMetricsReader {
    prev_cpu: Option<(u64, u64)>, // (idle, total)
    prev_retrans: Option<u64>,
}

impl HostMetricsReader {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn sample(&mut self) -> HostMetrics {
        #[cfg(target_os = "linux")]
        {
            self.sample_linux()
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (self.prev_cpu, self.prev_retrans);
            HostMetrics::default()
        }
    }

    #[cfg(target_os = "linux")]
    fn sample_linux(&mut self) -> HostMetrics {
        let mut m = HostMetrics::default();

        // CPU: delta of /proc/stat aggregate line.
        if let Ok(stat) = std::fs::read_to_string("/proc/stat") {
            if let Some(line) = stat.lines().next() {
                let f: Vec<u64> = line
                    .split_whitespace()
                    .skip(1)
                    .filter_map(|s| s.parse().ok())
                    .collect();
                if f.len() >= 4 {
                    let idle = f[3] + f.get(4).copied().unwrap_or(0); // idle + iowait
                    let total: u64 = f.iter().sum();
                    if let Some((p_idle, p_total)) = self.prev_cpu {
                        let d_total = total.saturating_sub(p_total);
                        let d_idle = idle.saturating_sub(p_idle);
                        if d_total > 0 {
                            m.cpu_pct = round1(100.0 * (d_total - d_idle) as f64 / d_total as f64);
                        }
                    }
                    self.prev_cpu = Some((idle, total));
                }
            }
        }

        if let Ok(load) = std::fs::read_to_string("/proc/loadavg") {
            if let Some(l1) = load.split_whitespace().next().and_then(|s| s.parse().ok()) {
                m.load1 = l1;
            }
        }

        if let Ok(meminfo) = std::fs::read_to_string("/proc/meminfo") {
            let mut total = 0u64;
            let mut avail = 0u64;
            for line in meminfo.lines() {
                if let Some(v) = line.strip_prefix("MemTotal:") {
                    total = parse_kb(v);
                } else if let Some(v) = line.strip_prefix("MemAvailable:") {
                    avail = parse_kb(v);
                }
            }
            if total > 0 {
                m.mem_pct = round1(100.0 * (total - avail) as f64 / total as f64);
            }
        }

        m.disk_pct = disk_usage_pct(std::ffi::CString::new("/").unwrap());

        if let Ok(dev) = std::fs::read_to_string("/proc/net/dev") {
            for line in dev.lines().skip(2) {
                let Some((_, rest)) = line.split_once(':') else { continue };
                let cols: Vec<u64> = rest
                    .split_whitespace()
                    .filter_map(|s| s.parse().ok())
                    .collect();
                if cols.len() >= 12 {
                    m.rx_drops += cols[3];
                    m.tx_drops += cols[11];
                }
            }
        }

        if let Some(retrans) = read_tcp_retrans() {
            m.tcp_retrans = self
                .prev_retrans
                .map_or(0, |p| retrans.saturating_sub(p));
            self.prev_retrans = Some(retrans);
        }

        let (src, off, unc, locked) = read_clock();
        m.clock_source = src;
        m.clock_offset_ms = off;
        m.clock_uncertainty_ms = unc;
        m.clock_locked = locked;
        m
    }
}

#[cfg(target_os = "linux")]
fn parse_kb(v: &str) -> u64 {
    v.split_whitespace()
        .next()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0)
}

#[cfg(target_os = "linux")]
fn disk_usage_pct(path: std::ffi::CString) -> f64 {
    unsafe {
        let mut st: libc::statvfs = std::mem::zeroed();
        if libc::statvfs(path.as_ptr(), &mut st) != 0 || st.f_blocks == 0 {
            return 0.0;
        }
        let used = st.f_blocks - st.f_bfree;
        round1(100.0 * used as f64 / st.f_blocks as f64)
    }
}

#[cfg(target_os = "linux")]
fn read_tcp_retrans() -> Option<u64> {
    let snmp = std::fs::read_to_string("/proc/net/snmp").ok()?;
    let mut lines = snmp.lines();
    while let Some(header) = lines.next() {
        if header.starts_with("Tcp:") && header.contains("RetransSegs") {
            let keys: Vec<&str> = header.split_whitespace().collect();
            let values = lines.next()?;
            let vals: Vec<&str> = values.split_whitespace().collect();
            let idx = keys.iter().position(|k| *k == "RetransSegs")?;
            return vals.get(idx)?.parse().ok();
        }
    }
    None
}

/// Clock telemetry: chronyc first, ntpq fallback, else unknown.
/// Returns (source, offset_ms, uncertainty_ms, locked).
#[cfg(target_os = "linux")]
fn read_clock() -> (String, Option<f64>, Option<f64>, bool) {
    if let Ok(out) = std::process::Command::new("chronyc")
        .arg("tracking")
        .output()
    {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout);
            let mut offset_s = None;
            let mut sign = 1.0f64;
            let mut root_delay = 0.0f64;
            let mut root_disp = 0.0f64;
            let mut locked = false;
            for line in text.lines() {
                if let Some(v) = line.strip_prefix("System time") {
                    if let Some(num) = v.split_whitespace().find_map(|t| t.parse::<f64>().ok()) {
                        offset_s = Some(num.abs());
                        sign = if v.contains("slow") { -1.0 } else { 1.0 };
                    }
                } else if let Some(v) = line.strip_prefix("Root delay") {
                    root_delay = v
                        .split_whitespace()
                        .find_map(|t| t.parse::<f64>().ok())
                        .unwrap_or(0.0);
                } else if let Some(v) = line.strip_prefix("Root dispersion") {
                    root_disp = v
                        .split_whitespace()
                        .find_map(|t| t.parse::<f64>().ok())
                        .unwrap_or(0.0);
                } else if let Some(v) = line.strip_prefix("Leap status") {
                    locked = v.contains("Normal");
                }
            }
            if let Some(off) = offset_s {
                let uncertainty = (root_delay / 2.0 + root_disp) * 1000.0;
                return (
                    "chrony".into(),
                    Some(round3(sign * off * 1000.0)),
                    Some(round3(uncertainty)),
                    locked,
                );
            }
        }
    }
    if let Ok(out) = std::process::Command::new("ntpq").arg("-p").output() {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout);
            for line in text.lines() {
                if line.starts_with('*') {
                    let cols: Vec<&str> = line.split_whitespace().collect();
                    if let Some(off) = cols.get(8).and_then(|s| s.parse::<f64>().ok()) {
                        return ("ntp".into(), Some(off), Some(off.abs() + 1.0), true);
                    }
                }
            }
        }
    }
    ("unknown".into(), None, None, false)
}

#[cfg(target_os = "linux")]
fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}
#[cfg(target_os = "linux")]
fn round3(x: f64) -> f64 {
    (x * 1000.0).round() / 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sample_returns_sane_defaults_everywhere() {
        let mut r = HostMetricsReader::new();
        let m = r.sample();
        assert!(m.cpu_pct >= 0.0);
        assert!(!m.clock_source.is_empty());
        if !cfg!(target_os = "linux") {
            assert_eq!(m.clock_source, "unknown");
            assert_eq!(m.cpu_pct, 0.0);
            assert_eq!(m.rx_drops, 0);
        }
    }

    #[test]
    fn clock_estimate_gating_bridge() {
        let m = HostMetrics {
            clock_offset_ms: Some(0.8),
            clock_uncertainty_ms: Some(1.2),
            clock_locked: true,
            clock_source: "chrony".into(),
            ..Default::default()
        };
        let est = m.clock_estimate(5.0);
        assert_eq!(est.quality(), bnqo_proto::ClockQuality::Good);
    }
}
