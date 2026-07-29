//! Job executor: RUN_MTR, RUN_ICMP_PROBE, RUN_TCP_PROBE,
//! COLLECT_HOST_SNAPSHOT (contract §2.3 whitelist). Results flow back via
//! the report batch (mtr_results / icmp / service_probes + job_acks).

use std::sync::{Arc, RwLock};
use std::time::Duration;

use bnqo_measure::host::{HostMetrics, HostMetricsReader};
use bnqo_measure::icmp;
use bnqo_measure::service::{self, ServiceTarget};
use sha2::Digest;
use tokio::sync::mpsc;

use crate::model::{
    IcmpRec, JobAckRec, LinkConfig, MtrHop, MtrResultRec, ServiceProbeRec, TelemetryItem,
    ValidatedJob,
};

pub struct JobContext {
    pub links: Vec<LinkConfig>,
    pub sink: mpsc::Sender<TelemetryItem>,
    pub latest_host: Arc<RwLock<HostMetrics>>,
}

fn link_for(ctx: &JobContext, link_id: u64) -> Option<&LinkConfig> {
    ctx.links.iter().find(|l| l.link_id == link_id)
}

async fn emit(ctx: &JobContext, item: TelemetryItem) {
    let _ = ctx.sink.send(item).await;
}

async fn ack(ctx: &JobContext, job_id: &str, status: &str, error_class: Option<String>) {
    emit(
        ctx,
        TelemetryItem::JobAck(JobAckRec {
            job_id: job_id.to_string(),
            status: status.to_string(),
            error_class,
        }),
    )
    .await;
}

pub async fn execute(job: ValidatedJob, ctx: Arc<JobContext>) {
    match job.job_type.as_str() {
        "RUN_MTR" => run_mtr(job, ctx).await,
        "RUN_ICMP_PROBE" => run_icmp(job, ctx).await,
        "RUN_TCP_PROBE" => run_tcp(job, ctx).await,
        "COLLECT_HOST_SNAPSHOT" => {
            let mut reader = HostMetricsReader::new();
            // Two quick samples so cpu_pct/retrans deltas are meaningful.
            let _ = reader.sample();
            tokio::time::sleep(Duration::from_secs(1)).await;
            let m = reader.sample();
            if let Ok(mut slot) = ctx.latest_host.write() {
                *slot = m;
            }
            ack(&ctx, &job.job_id, "done", None).await;
        }
        other => {
            // Should be unreachable: validate_job enforces the whitelist.
            ack(&ctx, &job.job_id, "failed", Some(format!("unknown-type:{other}"))).await;
        }
    }
}

fn param_str(params: &serde_json::Value, key: &str) -> Option<String> {
    params.get(key).and_then(|v| {
        v.as_str()
            .map(str::to_string)
            .or_else(|| v.as_u64().map(|n| n.to_string()))
    })
}

fn param_u64(params: &serde_json::Value, key: &str, default: u64) -> u64 {
    params.get(key).and_then(|v| v.as_u64()).unwrap_or(default)
}

async fn run_icmp(job: ValidatedJob, ctx: Arc<JobContext>) {
    let Some(target) = param_str(&job.params, "target") else {
        ack(&ctx, &job.job_id, "failed", Some("bad-params".into())).await;
        return;
    };
    let link_id = param_u64(&job.params, "link_id", 0);
    let count = param_u64(&job.params, "count", 5) as u32;
    let direction = link_for(&ctx, link_id)
        .map(|l| l.direction.clone())
        .unwrap_or_else(|| "a_to_b".into());
    let Ok(addr) = target.parse::<std::net::IpAddr>() else {
        ack(&ctx, &job.job_id, "failed", Some("resolve-failed".into())).await;
        return;
    };
    let result = icmp::ping_cycle(
        addr,
        count,
        Duration::from_millis(200),
        Duration::from_secs(1),
    )
    .await;
    let err = result.error_class.clone();
    emit(
        &ctx,
        TelemetryItem::Icmp(IcmpRec {
            link_id,
            direction,
            sent: result.sent,
            received: result.received,
            loss_pct: result.loss_pct,
            rtt_avg_ms: result.rtt_avg_ms,
            rtt_p95_ms: result.rtt_p95_ms,
            error_class: err.clone(),
        }),
    )
    .await;
    match err {
        None => ack(&ctx, &job.job_id, "done", None).await,
        Some(e) => ack(&ctx, &job.job_id, "failed", Some(e)).await,
    }
}

async fn run_tcp(job: ValidatedJob, ctx: Arc<JobContext>) {
    let Some(host) = param_str(&job.params, "target") else {
        ack(&ctx, &job.job_id, "failed", Some("bad-params".into())).await;
        return;
    };
    let port = param_u64(&job.params, "port", 443) as u16;
    let tls = job
        .params
        .get("tls")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let link_id = param_u64(&job.params, "link_id", 0);
    let target = ServiceTarget {
        name: format!("job:{}", job.job_id),
        host,
        port,
        tls,
        http_get: tls,
    };
    let r = service::probe(&target, Duration::from_secs(5)).await;
    let err = r.error_class.clone();
    emit(
        &ctx,
        TelemetryItem::ServiceProbe(ServiceProbeRec {
            link_id,
            target_name: target.name.clone(),
            ok: r.ok,
            tcp_ms: r.tcp_ms,
            tls_ms: r.tls_ms,
            http_status: r.http_status,
            error_class: err.clone(),
        }),
    )
    .await;
    match err {
        None => ack(&ctx, &job.job_id, "done", None).await,
        Some(e) => ack(&ctx, &job.job_id, "failed", Some(e)).await,
    }
}

/// mtr --json emits numbers as strings ("Loss%": "0.00"); accept both
/// string and numeric JSON values.
fn json_f64(v: Option<&serde_json::Value>) -> f64 {
    match v {
        Some(serde_json::Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn json_u64(v: Option<&serde_json::Value>) -> u64 {
    match v {
        Some(serde_json::Value::Number(n)) => n.as_u64().unwrap_or(0),
        Some(serde_json::Value::String(s)) => s.parse().unwrap_or(0),
        _ => 0,
    }
}

/// Parse `mtr --json` output into hops. Separated for testability.
pub fn parse_mtr_json(raw: &[u8], target: &str) -> Option<(Vec<MtrHop>, bool)> {
    let v: serde_json::Value = serde_json::from_slice(raw).ok()?;
    let hubs = v.get("report")?.get("hubs")?.as_array()?;
    let mut hops = Vec::new();
    let mut reached = false;
    for hub in hubs {
        let hop = json_u64(hub.get("count")) as u32;
        let address = hub
            .get("host")
            .and_then(|h| h.as_str())
            .unwrap_or("???")
            .to_string();
        let loss_pct = json_f64(hub.get("Loss%"));
        let rtt_avg_ms = json_f64(hub.get("Avg"));
        if address == target {
            reached = true;
        }
        hops.push(MtrHop {
            hop,
            address,
            loss_pct,
            rtt_avg_ms,
        });
    }
    Some((hops, reached))
}

/// route_hash = first 16 hex chars of SHA-256 over the ordered hop
/// address list (task spec; 8 bytes, matching the §9 sketch's 16-byte
/// route_hash truncated to its contract string form).
pub fn route_hash(hops: &[MtrHop]) -> String {
    let joined = hops
        .iter()
        .map(|h| h.address.as_str())
        .collect::<Vec<_>>()
        .join(",");
    let digest = sha2::Sha256::digest(joined.as_bytes());
    hex::encode(&digest[..8])
}

async fn run_mtr(job: ValidatedJob, ctx: Arc<JobContext>) {
    let Some(target) = param_str(&job.params, "target") else {
        ack(&ctx, &job.job_id, "failed", Some("bad-params".into())).await;
        return;
    };
    let cycles = param_u64(&job.params, "cycles", 10).clamp(1, 100);
    let link_id = param_u64(&job.params, "link_id", 0);
    let direction = link_for(&ctx, link_id)
        .map(|l| l.direction.clone())
        .unwrap_or_else(|| "a_to_b".into());

    let output = tokio::time::timeout(
        Duration::from_secs(30 + cycles * 2),
        tokio::process::Command::new("mtr")
            .arg("--json")
            .arg("--report")
            .arg("--report-cycles")
            .arg(cycles.to_string())
            .arg(&target)
            .output(),
    )
    .await;

    let output = match output {
        Ok(Ok(o)) => o,
        Ok(Err(e)) if e.kind() == std::io::ErrorKind::NotFound => {
            ack(&ctx, &job.job_id, "failed", Some("mtr-unavailable".into())).await;
            return;
        }
        Ok(Err(e)) => {
            ack(&ctx, &job.job_id, "failed", Some(format!("mtr-spawn:{e}"))).await;
            return;
        }
        Err(_) => {
            ack(&ctx, &job.job_id, "failed", Some("timeout".into())).await;
            return;
        }
    };
    if !output.status.success() {
        ack(&ctx, &job.job_id, "failed", Some("mtr-failed".into())).await;
        return;
    }
    match parse_mtr_json(&output.stdout, &target) {
        Some((hops, reached)) => {
            let hash = route_hash(&hops);
            emit(
                &ctx,
                TelemetryItem::MtrResult(MtrResultRec {
                    job_id: job.job_id.clone(),
                    link_id,
                    direction,
                    route_hash: hash,
                    destination_reached: reached,
                    hops,
                }),
            )
            .await;
            ack(&ctx, &job.job_id, "done", None).await;
        }
        None => ack(&ctx, &job.job_id, "failed", Some("mtr-parse".into())).await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_realistic_mtr_json() {
        let raw = br#"{
          "report": {
            "mtr": {"src": "agent", "dst": "198.51.100.5", "tos": "0x0", "tests": "10"},
            "hubs": [
              {"count": "1", "host": "10.0.0.1", "Loss%": "0.00", "Snt": "10", "Avg": "0.42", "Best": "0.31", "Wrst": "1.02", "StDev": "0.21"},
              {"count": "2", "host": "172.16.0.1", "Loss%": "10.00", "Snt": "10", "Avg": "12.50", "Best": "10.1", "Wrst": "30.2", "StDev": "5.1"},
              {"count": "3", "host": "198.51.100.5", "Loss%": "0.00", "Snt": "10", "Avg": "82.10", "Best": "79.3", "Wrst": "95.3", "StDev": "4.2"}
            ]
          }
        }"#;
        let (hops, reached) = parse_mtr_json(raw, "198.51.100.5").unwrap();
        assert_eq!(hops.len(), 3);
        assert!(reached);
        assert_eq!(hops[0].address, "10.0.0.1");
        assert_eq!(hops[2].hop, 3);
        assert_eq!(hops[1].loss_pct, 10.0);
        assert!((hops[2].rtt_avg_ms - 82.1).abs() < 1e-9);

        // Hash stability: same hops -> same hash; different -> different.
        let h1 = route_hash(&hops);
        let h2 = route_hash(&hops);
        assert_eq!(h1, h2);
        assert_eq!(h1.len(), 16);
        let mut other = hops.clone();
        other[1].address = "172.16.0.2".into();
        assert_ne!(h1, route_hash(&other));
    }

    #[test]
    fn parse_garbage_is_none() {
        assert!(parse_mtr_json(b"not json", "x").is_none());
        assert!(parse_mtr_json(b"{}", "x").is_none());
    }
}
