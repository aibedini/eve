//! bnqo-agent: BNQO probe agent for the eve control plane.
//!
//! Loops:
//! - control loop (10 s + jitter): signed config with anti-rollback, signed
//!   job dispatch;
//! - report uploader: WAL-sealed batches with strictly increasing agent_seq,
//!   exponential backoff with jitter (max 5 min);
//! - socket demux: reflections -> prober, forward probes -> reflector;
//! - prober tasks: one per configured link;
//! - host sampler: 30 s.

mod config;
mod control;
mod http;
mod identity;
mod jobs;
mod model;
mod prober;
mod reflector;
mod wal;

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::Path;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use clap::Parser;
use rand::Rng;
use tokio::net::UdpSocket;
use tokio::sync::{mpsc, watch};

use crate::model::{JobAckRec, TelemetryItem};

#[derive(Parser)]
#[command(name = "bnqo-agent", about = "BNQO probe agent (docs/bnqo)")]
struct Cli {
    /// Path to agent.toml.
    #[arg(long, default_value = "/etc/bnqo/agent.toml")]
    config: String,
}

/// Shared view of the applied control-plane config.
struct Shared {
    state: RwLock<control::ControlState>,
    host: prober::SharedHost,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let cli = Cli::parse();
    let cfg = config::load(&cli.config)?;
    tracing::info!(eve_url = %cfg.eve_url, state_dir = %cfg.state_dir, "bnqo-agent starting");

    let id = identity::load_or_enroll(&cfg).await?;
    let http = http::CpHttp::new(&cfg.eve_url, &id.token, id.signing.clone())?;

    let state_dir = Path::new(&cfg.state_dir);
    std::fs::create_dir_all(state_dir)?;
    let wal = wal::Wal::open(state_dir, cfg.wal_quota_bytes, cfg.wal_fsync)?;
    tracing::info!(
        next_agent_seq = wal.next_agent_seq(),
        ack_watermark = wal.ack_watermark(),
        pending = wal.pending_records(),
        "WAL opened"
    );

    let mut ctrl = control::ControlState::new();
    ctrl.load_executed(state_dir);
    control::restore_config(&mut ctrl, state_dir, &id.cp_pubkey);

    let shared = Arc::new(Shared {
        state: RwLock::new(ctrl),
        host: Arc::new(RwLock::new(bnqo_measure::host::HostMetrics::default())),
    });

    // Telemetry batcher channel.
    let (sink, batcher_rx) = mpsc::channel::<TelemetryItem>(1024);

    // UDP socket + demux.
    let socket = Arc::new(UdpSocket::bind(("0.0.0.0", cfg.bind_port)).await?);

    // Link task registry: link_id -> (reflection channel, shutdown).
    let links: Arc<RwLock<HashMap<u64, LinkHandle>>> = Arc::new(RwLock::new(HashMap::new()));

    // Reflector ingress channel.
    let (reflector_tx, mut reflector_rx) = mpsc::channel::<(Vec<u8>, SocketAddr)>(4096);

    // Socket demux task.
    {
        let socket = socket.clone();
        let links = links.clone();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 2048];
            loop {
                let (n, src) = match socket.recv_from(&mut buf).await {
                    Ok(x) => x,
                    Err(e) => {
                        tracing::warn!(error = %e, "udp recv failed");
                        tokio::time::sleep(Duration::from_millis(50)).await;
                        continue;
                    }
                };
                let data = buf[..n].to_vec();
                // Cheap flag peek: REFLECTED -> prober, else reflector.
                let reflected = n >= 4 && data[3] & 0x01 != 0;
                if reflected {
                    // Route by (session_id, src) -> link channel.
                    if n >= 16 {
                        let session_id = u64::from_be_bytes(data[8..16].try_into().unwrap());
                        let tx = {
                            let map = links.read().unwrap();
                            map.get(&session_id).map(|h| h.tx.clone())
                        };
                        if let Some(tx) = tx {
                            let _ = tx.try_send(data);
                        }
                    }
                } else {
                    let _ = reflector_tx.try_send((data, src));
                }
            }
        });
    }

    // Reflector task: session table rebuilt on config apply.
    {
        let socket = socket.clone();
        let shared = shared.clone();
        let cfg = cfg.clone();
        tokio::spawn(async move {
            let mut table: reflector::SessionTable = HashMap::new();
            let mut table_version = 0u64;
            while let Some((data, src)) = reflector_rx.recv().await {
                let (version, quality) = {
                    let st = shared.state.read().unwrap();
                    let v = st.applied_version;
                    drop(st);
                    let q = shared
                        .host
                        .read()
                        .map(|h| h.clock_estimate(5.0).quality())
                        .unwrap_or(bnqo_proto::ClockQuality::Unknown);
                    (v, q)
                };
                if version != table_version {
                    let links = {
                        let st = shared.state.read().unwrap();
                        st.config.as_ref().map(|c| c.links.clone()).unwrap_or_default()
                    };
                    table = reflector::build_session_table(&links, cfg.bind_port, 200.0, quality);
                    table_version = version;
                }
                let now_ns = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_nanos() as u64)
                    .unwrap_or(0);
                if let reflector::ReflectOutcome::Reflect(out) = reflector::handle_datagram(
                    &mut table,
                    &data,
                    src,
                    now_ns,
                    Duration::from_secs(cfg.max_clock_skew_sec),
                ) {
                    let _ = socket.send_to(&out, src).await;
                }
            }
        });
    }

    // Host sampler task (30 s).
    {
        let shared = shared.clone();
        tokio::spawn(async move {
            let mut reader = bnqo_measure::host::HostMetricsReader::new();
            loop {
                let host = shared.host.clone();
                let sampled = tokio::task::spawn_blocking(move || {
                    let m = reader.sample();
                    (m, reader)
                })
                .await;
                if let Ok((m, r)) = sampled {
                    reader = r;
                    if let Ok(mut slot) = host.write() {
                        *slot = m;
                    }
                } else {
                    break;
                }
                tokio::time::sleep(Duration::from_secs(30)).await;
            }
        });
    }

    // Control loop (config + jobs), every 10 s + jitter.
    {
        let shared = shared.clone();
        let http = http.clone();
        let cfg = cfg.clone();
        let links = links.clone();
        let socket = socket.clone();
        let sink = sink.clone();
        let id = id.clone();
        tokio::spawn(async move {
            let mut job_tx: Option<mpsc::Sender<model::ValidatedJob>> = None;
            loop {
                // ---- config ----
                match http.get_json("/api/bnqo/agent/config").await {
                    Ok(raw) => {
                        let outcome = {
                            let mut st = shared.state.write().unwrap();
                            control::apply_config(&mut st, &raw, &id.cp_pubkey)
                        };
                        match outcome {
                            Ok(control::ConfigOutcome::Applied(c)) => {
                                tracing::info!(version = c.config_version, "config applied");
                                let _ = control::persist_config(
                                    Path::new(&cfg.state_dir),
                                    &raw,
                                );
                                reconcile_links(&c.links, &links, &socket, &sink, &shared);
                            }
                            Ok(control::ConfigOutcome::Unchanged) => {}
                            Err(e) => {
                                tracing::warn!(error = %e, "config rejected, keeping last-good")
                            }
                        }
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "config fetch failed, measuring with last-good")
                    }
                }

                // ---- jobs ----
                match http.get_json("/api/bnqo/agent/jobs").await {
                    Ok(raw) => {
                        if let Some(jobs) = raw.get("jobs").and_then(|j| j.as_array()) {
                            for job_raw in jobs {
                                let now = chrono::Utc::now();
                                let validated = {
                                    let st = shared.state.read().unwrap();
                                    control::validate_job(&st, job_raw, &id.cp_pubkey, now)
                                };
                                match validated {
                                    Ok(job) => {
                                        {
                                            let mut st = shared.state.write().unwrap();
                                            st.mark_executed(&job.job_id);
                                            st.persist_executed(Path::new(&cfg.state_dir));
                                        }
                                        let tx = job_tx.get_or_insert_with(|| {
                                            let (tx, rx) = mpsc::channel(64);
                                            spawn_job_executor(rx, shared.clone(), sink.clone());
                                            tx
                                        });
                                        let _ = tx.send(job).await;
                                    }
                                    Err(e) => {
                                        tracing::debug!(error = %e, "job rejected");
                                    }
                                }
                            }
                        }
                    }
                    Err(e) => tracing::warn!(error = %e, "job fetch failed"),
                }

                let jitter = rand::thread_rng().gen_range(0.0..2.0);
                tokio::time::sleep(
                    Duration::from_secs(cfg.control_poll_interval_sec)
                        + Duration::from_secs_f64(jitter),
                )
                .await;
            }
        });
    }

    // Report batcher + uploader.
    run_report_loop(cfg.clone(), http.clone(), batcher_rx, wal, shared.host.clone()).await;

    Ok(())
}

struct LinkHandle {
    tx: mpsc::Sender<Vec<u8>>,
    shutdown: watch::Sender<bool>,
}

/// Start/stop prober tasks so they match the configured link set.
fn reconcile_links(
    configured: &[model::LinkConfig],
    links: &Arc<RwLock<HashMap<u64, LinkHandle>>>,
    socket: &Arc<UdpSocket>,
    sink: &mpsc::Sender<TelemetryItem>,
    shared: &Arc<Shared>,
) {
    let mut map = links.write().unwrap();
    // Stop removed links.
    let wanted: std::collections::HashSet<u64> = configured.iter().map(|l| l.link_id).collect();
    let stale: Vec<u64> = map.keys().filter(|id| !wanted.contains(id)).copied().collect();
    for id in stale {
        if let Some(h) = map.remove(&id) {
            let _ = h.shutdown.send(true);
        }
    }
    // Start new links. (Profile changes only take effect on link re-add or
    // agent restart — the CP bumps link identity via config changes.)
    for link in configured {
        if map.contains_key(&link.link_id) {
            continue;
        }
        let (tx, rx) = mpsc::channel(2048);
        let (sd_tx, sd_rx) = watch::channel(false);
        let link_id = link.link_id;
        let link = link.clone();
        let socket = socket.clone();
        let sink = sink.clone();
        let host = shared.host.clone();
        tokio::spawn(prober::run_link(link, socket, rx, sink, host, sd_rx));
        map.insert(link_id, LinkHandle { tx, shutdown: sd_tx });
    }
}

fn spawn_job_executor(
    mut rx: mpsc::Receiver<model::ValidatedJob>,
    shared: Arc<Shared>,
    sink: mpsc::Sender<TelemetryItem>,
) {
    tokio::spawn(async move {
        while let Some(job) = rx.recv().await {
            let links = {
                let st = shared.state.read().unwrap();
                st.config.as_ref().map(|c| c.links.clone()).unwrap_or_default()
            };
            let ctx = Arc::new(jobs::JobContext {
                links,
                sink: sink.clone(),
                latest_host: shared.host.clone(),
            });
            let job_id = job.job_id.clone();
            tokio::spawn(async move {
                jobs::execute(job, ctx).await;
                tracing::info!(job_id, "job executed");
            });
        }
    });
}

/// Batcher + uploader: accumulate telemetry, seal WAL batches with strictly
/// increasing agent_seq, POST in order, delete on ACK, backoff on failure.
async fn run_report_loop(
    cfg: model::AgentToml,
    http: http::CpHttp,
    mut rx: mpsc::Receiver<TelemetryItem>,
    mut wal: wal::Wal,
    host: prober::SharedHost,
) {
    let mut backoff = Duration::from_secs(1);
    let max_backoff = Duration::from_secs(300);
    let mut flush_tick = tokio::time::interval(Duration::from_secs(cfg.report_flush_interval_sec));
    let mut pending_items: Vec<TelemetryItem> = Vec::new();

    loop {
        tokio::select! {
            item = rx.recv() => {
                match item {
                    Some(i) => pending_items.push(i),
                    None => break,
                }
                // Don't hold more than one flush worth.
                if pending_items.len() < 200 {
                    continue;
                }
            }
            _ = flush_tick.tick() => {}
        }

    // Seal pending items into a WAL batch (agent_seq assigned by the WAL).
        if !pending_items.is_empty() {
            let host_sample = host.read().map(|h| h.clone()).unwrap_or_default();
            let batch = build_batch(&pending_items, &host_sample, wal.next_agent_seq());
            pending_items.clear();
            match wal.append_batch(&batch) {
                Ok(seq) => tracing::debug!(agent_seq = seq, "batch sealed into WAL"),
                Err(e) => tracing::error!(error = %e, "WAL append failed, items dropped"),
            }
        }

        // Upload pending batches in agent_seq order.
        for (seq, payload) in wal.pending() {
            match http.post_raw("/api/bnqo/agent/report", payload).await {
                Ok(resp) => {
                    let accepted = resp.get("accepted").and_then(|a| a.as_bool()).unwrap_or(false);
                    if accepted {
                        let wm = resp.get("agent_seq").and_then(|s| s.as_u64()).unwrap_or(seq);
                        if let Err(e) = wal.ack(wm) {
                            tracing::error!(error = %e, "WAL ack failed");
                        }
                        backoff = Duration::from_secs(1);
                    } else {
                        tracing::warn!(agent_seq = seq, "report not accepted, will retry");
                        break;
                    }
                }
                Err(e) => {
                    let jitter = rand::thread_rng().gen_range(0.0..1.0);
                    let wait = backoff + Duration::from_secs_f64(jitter);
                    tracing::warn!(error = %e, wait_s = wait.as_secs(), "report upload failed, backing off");
                    tokio::time::sleep(wait).await;
                    backoff = (backoff * 2).min(max_backoff);
                    break;
                }
            }
        }
    }
}

fn build_batch(
    items: &[TelemetryItem],
    host: &bnqo_measure::host::HostMetrics,
    agent_seq: u64,
) -> Vec<u8> {
    let mut measurements = Vec::new();
    let mut icmp = Vec::new();
    let mut service_probes = Vec::new();
    let mut mtr_results = Vec::new();
    let mut job_acks = Vec::new();
    for item in items {
        match item {
            TelemetryItem::Measurement(m) => measurements.push(m),
            TelemetryItem::Icmp(i) => icmp.push(i),
            TelemetryItem::ServiceProbe(s) => service_probes.push(s),
            TelemetryItem::MtrResult(m) => mtr_results.push(m),
            TelemetryItem::JobAck(a) => job_acks.push(a),
        }
    }
    let batch = serde_json::json!({
        "agent_seq": agent_seq,
        "sent_at": model::utc_now_iso(),
        "measurements": measurements,
        "icmp": icmp,
        "service_probes": service_probes,
        "host": host,
        "mtr_results": mtr_results,
        "job_acks": job_acks,
    });
    serde_json::to_vec(&batch).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn batch_json_matches_contract_shape() {
        let items = vec![
            TelemetryItem::Measurement(model::MeasurementRec {
                link_id: 1,
                direction: "a_to_b".into(),
                window_start: "2026-07-29T00:59:00Z".into(),
                window_end: "2026-07-29T00:59:30Z".into(),
                sent: 150,
                received: 148,
                loss_pct: 1.33,
                rtt_min_ms: Some(71.2),
                rtt_avg_ms: Some(83.5),
                rtt_p95_ms: Some(120.4),
                rtt_max_ms: Some(210.0),
                owd_ms: Some(41.0),
                clock_quality: "good".into(),
                jitter_ms: Some(6.2),
                reordered: 0,
                duplicated: 1,
                corrupted: 0,
                burst_max: 2,
            }),
            TelemetryItem::JobAck(JobAckRec {
                job_id: "job_01J".into(),
                status: "done".into(),
                error_class: None,
            }),
        ];
        let raw = build_batch(&items, &Default::default(), 1);
        let v: serde_json::Value = serde_json::from_slice(&raw).unwrap();
        for key in [
            "agent_seq",
            "sent_at",
            "measurements",
            "icmp",
            "service_probes",
            "host",
            "mtr_results",
            "job_acks",
        ] {
            assert!(v.get(key).is_some(), "missing {key}");
        }
        assert_eq!(v["measurements"][0]["loss_pct"], 1.33);
        assert_eq!(v["measurements"][0]["clock_quality"], "good");
        assert_eq!(v["job_acks"][0]["status"], "done");
    }
}
