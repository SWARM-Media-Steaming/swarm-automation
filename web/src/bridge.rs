//! The seam to the shared worker's read and compute logic.
//!
//! The history, usage, prompt-grade, Jev, architecture and routing-calculator
//! answers are produced by `issue_worker/` (the one shared worker), not
//! re-implemented here. [`Bridge`] is how the web backend asks for them:
//!
//! * [`ProcessBridge`] runs `issue_worker/web_bridge.py` once per call. The
//!   request is JSON on **stdin** (never argv), the environment is cleared down
//!   to `PATH` plus the `SWARM_STORAGE_*` settings of the hosted storage, and no
//!   provider key, GitHub credential or session value is ever passed.
//! * [`UnconfiguredBridge`] answers every call with a 503 so a deployment
//!   without a worker directory fails loudly rather than returning nothing.
//!
//! Tenant isolation is structural: the request names exactly one tenant, which
//! the HTTP layer took from `TenantAccess`, and the worker opens that tenant's
//! storage only.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::model::TenantId;
use crate::redact::redact_text;
use crate::secret::Secret;

const CALL_TIMEOUT: Duration = Duration::from_secs(120);
const MAX_OUTPUT: usize = 8 * 1024 * 1024;

#[derive(Debug)]
pub struct BridgeRequest {
    pub tenant: TenantId,
    pub op: &'static str,
    pub args: Value,
    /// The tenant's saved settings (`settings::load`), so an operation knows the
    /// repositories and providers without reading the store itself.
    pub config: Value,
}

#[derive(Debug)]
pub enum BridgeError {
    /// The worker has no implementation of this operation for the hosted
    /// deployment yet (maps to 501).
    NotAvailable(String),
    /// The arguments were refused (maps to 400).
    BadRequest(String),
    /// No bridge is configured (maps to 503).
    Unconfigured(String),
    /// The worker failed (maps to 500; the detail is logged redacted).
    Failed(String),
}

#[async_trait]
pub trait Bridge: Send + Sync {
    async fn call(&self, request: BridgeRequest) -> Result<Value, BridgeError>;
}

pub struct UnconfiguredBridge;

#[async_trait]
impl Bridge for UnconfiguredBridge {
    async fn call(&self, _request: BridgeRequest) -> Result<Value, BridgeError> {
        Err(BridgeError::Unconfigured(
            "The worker bridge is not configured on this deployment (SWARM_WEB_BRIDGE=python)."
                .into(),
        ))
    }
}

/// The `SWARM_STORAGE_*` names `storage_factory.open_storage("hosted")` reads.
/// The values never leave this process except into the bridge child.
pub const STORAGE_ENV: &[&str] = &[
    "SWARM_STORAGE_POSTGRES_DSN",
    "SWARM_STORAGE_POSTGRES_DRIVER",
    "SWARM_STORAGE_S3_ENDPOINT",
    "SWARM_STORAGE_S3_BUCKET",
    "SWARM_STORAGE_S3_ACCESS_KEY_ID",
    "SWARM_STORAGE_S3_SECRET_ACCESS_KEY",
    "SWARM_STORAGE_S3_SESSION_TOKEN",
    "SWARM_STORAGE_S3_REGION",
    "SWARM_STORAGE_S3_PREFIX",
    "SWARM_STORAGE_S3_ADDRESSING",
    "SWARM_STORAGE_S3_ALLOW_INSECURE_HTTP",
    "SWARM_STORAGE_SCRATCH_DIR",
];

pub struct ProcessBridge {
    python: PathBuf,
    script: PathBuf,
    env: BTreeMap<String, Secret>,
}

impl ProcessBridge {
    pub fn new(python: PathBuf, worker_dir: PathBuf, env: BTreeMap<String, Secret>) -> Self {
        ProcessBridge {
            python,
            script: worker_dir.join("web_bridge.py"),
            env,
        }
    }
}

#[async_trait]
impl Bridge for ProcessBridge {
    async fn call(&self, request: BridgeRequest) -> Result<Value, BridgeError> {
        let document = json!({
            "op": request.op,
            "tenant": request.tenant.as_str(),
            "args": request.args,
            "config": request.config,
        });
        let body = serde_json::to_vec(&document).map_err(|e| BridgeError::Failed(e.to_string()))?;
        let mut command = tokio::process::Command::new(&self.python);
        command
            .arg("-I")
            .arg(&self.script)
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        if let Some(path) = std::env::var_os("PATH") {
            command.env("PATH", path);
        }
        for (name, value) in &self.env {
            command.env(name, value.expose());
        }
        let mut child = command
            .spawn()
            .map_err(|e| BridgeError::Failed(format!("could not start the worker bridge: {e}")))?;
        if let Some(mut stdin) = child.stdin.take() {
            stdin
                .write_all(&body)
                .await
                .map_err(|e| BridgeError::Failed(format!("could not send the request: {e}")))?;
        }
        let mut stdout = child
            .stdout
            .take()
            .ok_or_else(|| BridgeError::Failed("no stdout".into()))?;
        let mut stderr = child
            .stderr
            .take()
            .ok_or_else(|| BridgeError::Failed("no stderr".into()))?;
        let run = async {
            let mut out = Vec::new();
            let mut err = Vec::new();
            let mut limited_out = (&mut stdout).take(MAX_OUTPUT as u64 + 1);
            let mut limited_err = (&mut stderr).take(64 * 1024);
            let (a, b) = tokio::join!(
                limited_out.read_to_end(&mut out),
                limited_err.read_to_end(&mut err)
            );
            a.and(b).map_err(|e| e.to_string())?;
            let status = child.wait().await.map_err(|e| e.to_string())?;
            Ok::<_, String>((status, out, err))
        };
        let (status, out, err) = tokio::time::timeout(CALL_TIMEOUT, run)
            .await
            .map_err(|_| BridgeError::Failed("the worker bridge timed out".into()))?
            .map_err(BridgeError::Failed)?;
        if out.len() > MAX_OUTPUT {
            return Err(BridgeError::Failed(
                "the worker bridge answer is too large".into(),
            ));
        }
        let parsed: Value = match serde_json::from_slice(&out) {
            Ok(value) => value,
            Err(_) => {
                let detail = redact_text(&String::from_utf8_lossy(&err));
                return Err(BridgeError::Failed(format!(
                    "the worker bridge exited with {status} and no answer: {}",
                    detail.chars().take(300).collect::<String>()
                )));
            }
        };
        if parsed.get("ok").and_then(Value::as_bool) == Some(true) {
            return Ok(parsed.get("result").cloned().unwrap_or(Value::Null));
        }
        let message = parsed
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("The worker bridge failed.")
            .to_string();
        Err(match parsed.get("code").and_then(Value::as_str) {
            Some("unavailable") => BridgeError::NotAvailable(message),
            Some("bad_request") => BridgeError::BadRequest(message),
            _ => BridgeError::Failed(message),
        })
    }
}
