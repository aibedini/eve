//! Control-plane HTTP client (EVE_API_CONTRACT.md §1, §2).
//!
//! All requests go through reqwest with rustls (no native-tls/openssl).
//! Authentication headers:
//!   Authorization: Bearer <agent_token>
//!   X-BNQO-Timestamp: <unix seconds>
//!   X-BNQO-Signature: base64 Ed25519 over "<ts>\n" + raw body bytes

use bnqo_proto::ed25519;
use ed25519_dalek::SigningKey;

#[derive(Debug, thiserror::Error)]
pub enum HttpError {
    #[error("request failed: {0}")]
    Transport(#[from] reqwest::Error),
    #[error("server returned {status}: {body}")]
    Status { status: u16, body: String },
    #[error("bad json: {0}")]
    Json(#[from] serde_json::Error),
}

#[derive(Clone)]
pub struct CpHttp {
    client: reqwest::Client,
    base: String,
    token: String,
    signing: SigningKey,
}

impl CpHttp {
    pub fn new(
        eve_url: &str,
        token: &str,
        signing: SigningKey,
    ) -> Result<Self, HttpError> {
        let client = reqwest::Client::builder()
            .use_rustls_tls()
            .timeout(std::time::Duration::from_secs(15))
            .build()?;
        Ok(CpHttp {
            client,
            base: eve_url.trim_end_matches('/').to_string(),
            token: token.to_string(),
            signing,
        })
    }

    /// Client without auth material, for enrollment only.
    pub fn unauthenticated(eve_url: &str) -> Result<Self, HttpError> {
        Self::new(eve_url, "", SigningKey::from_bytes(&[0u8; 32]))
    }

    fn auth_headers(&self, body: &[u8]) -> [(String, String); 3] {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let msg = ed25519::api_auth_message(ts, body);
        let sig = ed25519::sign(&self.signing, &msg);
        [
            ("Authorization".into(), format!("Bearer {}", self.token)),
            ("X-BNQO-Timestamp".into(), ts.to_string()),
            ("X-BNQO-Signature".into(), ed25519::sig_to_b64(&sig)),
        ]
    }

    pub async fn get_json(&self, path: &str) -> Result<serde_json::Value, HttpError> {
        let url = format!("{}{}", self.base, path);
        let headers = self.auth_headers(b"");
        let resp = self
            .client
            .get(&url)
            .header(&headers[0].0, &headers[0].1)
            .header(&headers[1].0, &headers[1].1)
            .header(&headers[2].0, &headers[2].1)
            .send()
            .await?;
        let status = resp.status();
        let body = resp.bytes().await?;
        if !status.is_success() {
            return Err(HttpError::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&body).into_owned(),
            });
        }
        Ok(serde_json::from_slice(&body)?)
    }

    pub async fn post_json(
        &self,
        path: &str,
        body: &serde_json::Value,
    ) -> Result<serde_json::Value, HttpError> {
        let raw = serde_json::to_vec(body)?;
        self.post_raw(path, raw).await
    }

    pub async fn post_raw(
        &self,
        path: &str,
        raw: Vec<u8>,
    ) -> Result<serde_json::Value, HttpError> {
        let url = format!("{}{}", self.base, path);
        let headers = self.auth_headers(&raw);
        let resp = self
            .client
            .post(&url)
            .header("Content-Type", "application/json")
            .header(&headers[0].0, &headers[0].1)
            .header(&headers[1].0, &headers[1].1)
            .header(&headers[2].0, &headers[2].1)
            .body(raw)
            .send()
            .await?;
        let status = resp.status();
        let body = resp.bytes().await?;
        if !status.is_success() {
            return Err(HttpError::Status {
                status: status.as_u16(),
                body: String::from_utf8_lossy(&body).into_owned(),
            });
        }
        Ok(serde_json::from_slice(&body)?)
    }
}
