//! TCP/TLS connect prober for service targets (contract §2.2
//! `profile.service_targets`, §2.4 `service_probes[]`).

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

#[derive(Debug, Clone)]
pub struct ServiceTarget {
    pub name: String,
    pub host: String,
    pub port: u16,
    pub tls: bool,
    /// Issue a bare HTTP/1.1 GET after connect/TLS and capture the status.
    pub http_get: bool,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct ServiceProbeResult {
    pub ok: bool,
    pub tcp_ms: Option<f64>,
    pub tls_ms: Option<f64>,
    pub http_status: Option<u16>,
    /// timeout | refused | reset | tls_error | connect_error | http_error
    pub error_class: Option<String>,
}

impl ServiceProbeResult {
    fn failed(error_class: &str) -> Self {
        ServiceProbeResult {
            ok: false,
            tcp_ms: None,
            tls_ms: None,
            http_status: None,
            error_class: Some(error_class.to_string()),
        }
    }
}

fn ms(t: Instant) -> f64 {
    (t.elapsed().as_secs_f64() * 1000.0 * 100.0).round() / 100.0
}

fn classify_io(e: &std::io::Error) -> &'static str {
    use std::io::ErrorKind::*;
    match e.kind() {
        ConnectionRefused => "refused",
        ConnectionReset | ConnectionAborted => "reset",
        TimedOut => "timeout",
        _ => "connect_error",
    }
}

fn tls_connector() -> Arc<rustls::ClientConfig> {
    use std::sync::OnceLock;
    static CONNECTOR: OnceLock<Arc<rustls::ClientConfig>> = OnceLock::new();
    CONNECTOR
        .get_or_init(|| {
            let roots = rustls::RootCertStore::from_iter(
                webpki_roots::TLS_SERVER_ROOTS.iter().cloned(),
            );
            let cfg = rustls::ClientConfig::builder()
                .with_root_certificates(roots)
                .with_no_client_auth();
            Arc::new(cfg)
        })
        .clone()
}

/// Run one probe against `target`. `timeout` bounds each phase separately.
pub async fn probe(target: &ServiceTarget, timeout: Duration) -> ServiceProbeResult {
    let addr = format!("{}:{}", target.host, target.port);
    let t0 = Instant::now();
    let stream = match tokio::time::timeout(timeout, TcpStream::connect(&addr)).await {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => return ServiceProbeResult::failed(classify_io(&e)),
        Err(_) => return ServiceProbeResult::failed("timeout"),
    };
    let tcp_ms = ms(t0);

    if !target.tls {
        let mut result = ServiceProbeResult {
            ok: true,
            tcp_ms: Some(tcp_ms),
            tls_ms: None,
            http_status: None,
            error_class: None,
        };
        if target.http_get {
            match http_get_status(stream, &target.host, timeout).await {
                Ok(status) => result.http_status = Some(status),
                Err(e) => {
                    result.ok = false;
                    result.error_class = Some(e);
                }
            }
        }
        return result;
    }

    // TLS handshake phase.
    let t1 = Instant::now();
    let server_name = match rustls::pki_types::ServerName::try_from(target.host.clone()) {
        Ok(n) => n,
        Err(_) => return ServiceProbeResult::failed("tls_error"),
    };
    let connector = tokio_rustls::TlsConnector::from(tls_connector());
    let tls_stream = match tokio::time::timeout(
        timeout,
        connector.connect(server_name, stream),
    )
    .await
    {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => {
            let mut r = ServiceProbeResult::failed(if e.to_string().contains("tls")
                || e.kind() == std::io::ErrorKind::InvalidData
            {
                "tls_error"
            } else {
                classify_io(&e)
            });
            r.tcp_ms = Some(tcp_ms);
            return r;
        }
        Err(_) => {
            let mut r = ServiceProbeResult::failed("timeout");
            r.tcp_ms = Some(tcp_ms);
            return r;
        }
    };
    let tls_ms = ms(t1);

    let mut result = ServiceProbeResult {
        ok: true,
        tcp_ms: Some(tcp_ms),
        tls_ms: Some(tls_ms),
        http_status: None,
        error_class: None,
    };
    if target.http_get {
        match http_get_status(tls_stream, &target.host, timeout).await {
            Ok(status) => result.http_status = Some(status),
            Err(e) => {
                result.ok = false;
                result.error_class = Some(e);
            }
        }
    }
    result
}

async fn http_get_status<S>(mut io: S, host: &str, timeout: Duration) -> Result<u16, String>
where
    S: AsyncReadExt + AsyncWriteExt + Unpin,
{
    let req = format!("GET / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: bnqo-agent\r\nConnection: close\r\n\r\n");
    tokio::time::timeout(timeout, io.write_all(req.as_bytes()))
        .await
        .map_err(|_| "timeout".to_string())?
        .map_err(|e| classify_io(&e).to_string())?;
    let mut buf = vec![0u8; 4096];
    let n = tokio::time::timeout(timeout, io.read(&mut buf))
        .await
        .map_err(|_| "timeout".to_string())?
        .map_err(|e| classify_io(&e).to_string())?;
    parse_http_status(&buf[..n]).ok_or_else(|| "http_error".to_string())
}

/// Parse "HTTP/1.1 200 OK" -> 200.
pub fn parse_http_status(head: &[u8]) -> Option<u16> {
    let head = std::str::from_utf8(head).ok()?;
    let line = head.lines().next()?;
    if !line.starts_with("HTTP/") {
        return None;
    }
    line.split_whitespace().nth(1)?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn http_status_parsing() {
        assert_eq!(parse_http_status(b"HTTP/1.1 200 OK\r\n"), Some(200));
        assert_eq!(parse_http_status(b"HTTP/2 404"), Some(404));
        assert_eq!(parse_http_status(b"garbage"), None);
        assert_eq!(parse_http_status(b""), None);
    }

    #[tokio::test]
    async fn tcp_probe_against_local_listener() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let accept = tokio::spawn(async move {
            let (mut s, _) = listener.accept().await.unwrap();
            let mut buf = [0u8; 1024];
            let _ = s.read(&mut buf).await;
            s.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .await
                .unwrap();
        });
        let target = ServiceTarget {
            name: "local".into(),
            host: "127.0.0.1".into(),
            port,
            tls: false,
            http_get: true,
        };
        let r = probe(&target, Duration::from_secs(2)).await;
        assert!(r.ok, "unexpected error: {:?}", r.error_class);
        assert!(r.tcp_ms.is_some());
        assert_eq!(r.http_status, Some(200));
        accept.await.unwrap();
    }

    #[tokio::test]
    async fn refused_is_classified() {
        // Deterministic core: the io-error mapping itself.
        let e = std::io::Error::from(std::io::ErrorKind::ConnectionRefused);
        assert_eq!(classify_io(&e), "refused");

        // Live connect to a closed port. Most platforms RST immediately
        // ("refused"); Windows hosts behind a filtering firewall (incl. this
        // dev box) silently drop the SYN, surfacing as "timeout" instead.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let target = ServiceTarget {
            name: "closed".into(),
            host: "127.0.0.1".into(),
            port,
            tls: false,
            http_get: false,
        };
        let r = probe(&target, Duration::from_millis(500)).await;
        assert!(!r.ok);
        assert!(
            matches!(r.error_class.as_deref(), Some("refused") | Some("timeout")),
            "unexpected error_class: {:?}",
            r.error_class
        );
    }

    #[tokio::test]
    async fn timeout_is_classified() {
        // 10.255.255.1 is non-routable; connect should hang -> timeout.
        let target = ServiceTarget {
            name: "blackhole".into(),
            host: "10.255.255.1".into(),
            port: 65000,
            tls: false,
            http_get: false,
        };
        let r = probe(&target, Duration::from_millis(300)).await;
        assert!(!r.ok);
        assert_eq!(r.error_class.as_deref(), Some("timeout"));
    }
}
