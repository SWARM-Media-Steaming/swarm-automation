//! Process configuration, read from the environment (`SWARM_WEB_*`).

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::path::PathBuf;

use crate::crypto::LocalKeyWrapper;
use crate::model::{PlanQuotas, Provider};
use crate::secret::Secret;

pub struct GitHubConfig {
    /// The GitHub App's OAuth client id and secret (sign-in uses the App, not a
    /// separate OAuth App).
    pub client_id: String,
    pub client_secret: Secret,
    pub webhook_secret: Secret,
    /// The App's slug, for the "install the app" link.
    pub app_slug: Option<String>,
    pub web_base: String,
    pub api_base: String,
}

pub struct Config {
    pub bind: SocketAddr,
    pub ui_dir: PathBuf,
    /// The externally visible origin, e.g. `https://swarm.example.com`. The
    /// OAuth redirect URI, the CSRF `Origin` check and the cookie `Secure`
    /// flag all derive from it.
    pub public_url: String,
    pub session_ttl_secs: u64,
    pub github: GitHubConfig,
    pub internal_token: Option<Secret>,
    pub default_plan: PlanQuotas,
    /// `None` unless `SWARM_WEB_JOB_RUNNER` is `docker` or `fargate`.
    pub jobs: Option<JobConfig>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum JobBackend {
    Docker,
    Fargate,
}

/// Runner configuration. Required only when the job runner is enabled, so a
/// process that only serves the API keeps the original environment.
pub struct JobConfig {
    pub backend: JobBackend,
    pub image: String,
    pub docker_bin: PathBuf,
    pub docker_network: String,
    pub ecs_endpoint: String,
    pub ecs_region: String,
    pub ecs_cluster: String,
    pub ecs_task_definition: String,
    pub ecs_subnets: Vec<String>,
    pub ecs_security_groups: Vec<String>,
    pub ecs_access_key: Option<Secret>,
    pub ecs_secret_key: Option<Secret>,
    pub cpu_millis: u64,
    pub memory_mib: u64,
    /// `None` means a job is not killed for running a long time.
    pub max_runtime_secs: Option<u64>,
    pub quota_resume_secs: u64,
    pub poll_secs: u64,
    pub provider: Provider,
    pub app_id: u64,
    pub private_key: Secret,
    pub python: PathBuf,
    pub worker_dir: PathBuf,
    pub trusted_authors: Vec<String>,
    pub plain_env: BTreeMap<String, String>,
    pub secret_env: BTreeMap<String, Secret>,
}

#[derive(Debug)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

/// Plan limits a tenant gets until an operator sets its own. Policy, not data.
pub const DEFAULT_MAX_CONCURRENT_JOBS: u32 = 2;
pub const DEFAULT_MONTHLY_SPEND_CAP_USD: f64 = 100.0;
pub const DEFAULT_SESSION_TTL_SECS: u64 = 8 * 60 * 60;

impl Config {
    pub fn cookie_secure(&self) -> bool {
        self.public_url.starts_with("https://")
    }

    /// `origin` of `public_url` (scheme and authority only).
    pub fn origin(&self) -> String {
        self.public_url.trim_end_matches('/').to_string()
    }

    /// Build the configuration and the key wrapper from an environment lookup.
    /// Taking the lookup as a parameter keeps this testable without touching
    /// the process environment.
    pub fn from_lookup(
        get: &dyn Fn(&str) -> Option<String>,
    ) -> Result<(Config, LocalKeyWrapper), ConfigError> {
        let require = |name: &str| -> Result<String, ConfigError> {
            get(name)
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
                .ok_or_else(|| ConfigError(format!("{name} is required")))
        };
        let public_url = require("SWARM_WEB_PUBLIC_URL")?
            .trim_end_matches('/')
            .to_string();
        let parsed = url::Url::parse(&public_url)
            .map_err(|_| ConfigError("SWARM_WEB_PUBLIC_URL must be an absolute URL".into()))?;
        if !matches!(parsed.scheme(), "http" | "https")
            || parsed.path() != "" && parsed.path() != "/"
        {
            return Err(ConfigError(
                "SWARM_WEB_PUBLIC_URL must be an origin such as https://host".into(),
            ));
        }
        let bind = get("SWARM_WEB_BIND").unwrap_or_else(|| "127.0.0.1:8080".into());
        let bind: SocketAddr = bind
            .parse()
            .map_err(|_| ConfigError("SWARM_WEB_BIND must be host:port".into()))?;

        let client_secret = Secret::new(require("SWARM_WEB_GITHUB_CLIENT_SECRET")?);
        let webhook_secret = Secret::new(require("SWARM_WEB_GITHUB_WEBHOOK_SECRET")?);
        let internal_token = get("SWARM_WEB_INTERNAL_TOKEN")
            .filter(|v| !v.trim().is_empty())
            .map(Secret::new);
        if let Some(token) = &internal_token {
            if token.expose().len() < 24 {
                return Err(ConfigError(
                    "SWARM_WEB_INTERNAL_TOKEN must be at least 24 characters".into(),
                ));
            }
        }

        let wrapper = if let Some(value) =
            get("SWARM_WEB_LOCAL_KEY").filter(|v| !v.trim().is_empty())
        {
            LocalKeyWrapper::from_base64(&value)
                .map_err(|e| ConfigError(format!("SWARM_WEB_LOCAL_KEY: {e}")))?
        } else if get("SWARM_WEB_KMS_KEY_ID").is_some() {
            return Err(ConfigError(
                "SWARM_WEB_KMS_KEY_ID is set but this build has no KMS key wrapper; use SWARM_WEB_LOCAL_KEY for development".into(),
            ));
        } else {
            return Err(ConfigError(
                "SWARM_WEB_LOCAL_KEY (a base64 32-byte key) is required".into(),
            ));
        };

        let session_ttl_secs = match get("SWARM_WEB_SESSION_TTL_SECS") {
            Some(v) => v
                .trim()
                .parse()
                .map_err(|_| ConfigError("SWARM_WEB_SESSION_TTL_SECS must be a number".into()))?,
            None => DEFAULT_SESSION_TTL_SECS,
        };

        let config = Config {
            bind,
            ui_dir: PathBuf::from(get("SWARM_WEB_UI_DIR").unwrap_or_else(|| "../ui".into())),
            public_url,
            session_ttl_secs,
            github: GitHubConfig {
                client_id: require("SWARM_WEB_GITHUB_CLIENT_ID")?,
                client_secret,
                webhook_secret,
                app_slug: get("SWARM_WEB_GITHUB_APP_SLUG").filter(|v| !v.trim().is_empty()),
                web_base: "https://github.com".into(),
                api_base: "https://api.github.com".into(),
            },
            internal_token,
            default_plan: PlanQuotas {
                max_concurrent_jobs: DEFAULT_MAX_CONCURRENT_JOBS,
                monthly_spend_cap_usd: Some(DEFAULT_MONTHLY_SPEND_CAP_USD),
            },
            jobs: load_jobs(get)?,
        };
        Ok((config, wrapper))
    }

    /// Register every configured secret with the log scrubber.
    pub fn register_secrets(&self) {
        crate::redact::register_secret(self.github.client_secret.expose());
        crate::redact::register_secret(self.github.webhook_secret.expose());
        if let Some(token) = &self.internal_token {
            crate::redact::register_secret(token.expose());
        }
        if let Some(jobs) = &self.jobs {
            crate::redact::register_secret(jobs.private_key.expose());
            for secret in jobs.secret_env.values() {
                crate::redact::register_secret(secret.expose());
            }
            if let Some(secret) = &jobs.ecs_secret_key {
                crate::redact::register_secret(secret.expose());
            }
        }
    }
}

fn optional(get: &dyn Fn(&str) -> Option<String>, name: &str) -> Option<String> {
    get(name)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn parse_u64(value: &str, name: &str) -> Result<u64, ConfigError> {
    value
        .trim()
        .parse()
        .map_err(|_| ConfigError(format!("{name} must be a number")))
}

fn load_jobs(get: &dyn Fn(&str) -> Option<String>) -> Result<Option<JobConfig>, ConfigError> {
    let Some(backend_name) = optional(get, "SWARM_WEB_JOB_RUNNER") else {
        return Ok(None);
    };
    let backend = match backend_name.as_str() {
        "docker" => JobBackend::Docker,
        "fargate" => JobBackend::Fargate,
        _ => {
            return Err(ConfigError(
                "SWARM_WEB_JOB_RUNNER must be docker or fargate".into(),
            ))
        }
    };
    let image = optional(get, "SWARM_WEB_WORKER_IMAGE").ok_or_else(|| {
        ConfigError("SWARM_WEB_WORKER_IMAGE is required when the job runner is enabled".into())
    })?;
    let app_id = parse_u64(
        &optional(get, "SWARM_WEB_GITHUB_APP_ID").ok_or_else(|| {
            ConfigError("SWARM_WEB_GITHUB_APP_ID is required when the job runner is enabled".into())
        })?,
        "SWARM_WEB_GITHUB_APP_ID",
    )?;
    let private_key = Secret::new(
        optional(get, "SWARM_WEB_GITHUB_APP_PRIVATE_KEY").ok_or_else(|| {
            ConfigError(
                "SWARM_WEB_GITHUB_APP_PRIVATE_KEY is required when the job runner is enabled"
                    .into(),
            )
        })?,
    );
    if !private_key.expose().contains("BEGIN") {
        return Err(ConfigError(
            "SWARM_WEB_GITHUB_APP_PRIVATE_KEY must be a PEM private key".into(),
        ));
    }
    let provider = Provider::parse(
        &optional(get, "SWARM_WEB_JOB_PROVIDER").unwrap_or_else(|| "claude".into()),
    )
    .filter(|provider| provider.runs_jobs())
    .ok_or_else(|| ConfigError("SWARM_WEB_JOB_PROVIDER must be claude, codex or grok".into()))?;
    let cpu_millis = match optional(get, "SWARM_WEB_JOB_CPU_MILLIS") {
        Some(value) => parse_u64(&value, "SWARM_WEB_JOB_CPU_MILLIS")?,
        None => 1000,
    };
    let memory_mib = match optional(get, "SWARM_WEB_JOB_MEMORY_MIB") {
        Some(value) => parse_u64(&value, "SWARM_WEB_JOB_MEMORY_MIB")?,
        None => 2048,
    };
    if cpu_millis == 0 || memory_mib == 0 {
        return Err(ConfigError(
            "job cpu and memory limits must be positive".into(),
        ));
    }
    let max_runtime_secs = match optional(get, "SWARM_WEB_JOB_MAX_RUNTIME_SECS") {
        Some(value) => Some(parse_u64(&value, "SWARM_WEB_JOB_MAX_RUNTIME_SECS")?),
        None => None,
    };
    let quota_resume_secs = match optional(get, "SWARM_WEB_QUOTA_RESUME_SECS") {
        Some(value) => parse_u64(&value, "SWARM_WEB_QUOTA_RESUME_SECS")?,
        None => 60,
    };
    let poll_secs = match optional(get, "SWARM_WEB_POLL_SECS") {
        Some(value) => parse_u64(&value, "SWARM_WEB_POLL_SECS")?,
        None => 60,
    };
    let region = optional(get, "SWARM_WEB_ECS_REGION").unwrap_or_else(|| "us-east-1".into());
    let endpoint = optional(get, "SWARM_WEB_ECS_ENDPOINT")
        .unwrap_or_else(|| format!("https://ecs.{region}.amazonaws.com"));
    let subnets: Vec<String> = optional(get, "SWARM_WEB_ECS_SUBNETS")
        .map(|value| {
            value
                .split(',')
                .map(|part| part.trim().to_string())
                .filter(|part| !part.is_empty())
                .collect()
        })
        .unwrap_or_default();
    if backend == JobBackend::Fargate && subnets.is_empty() {
        return Err(ConfigError(
            "SWARM_WEB_ECS_SUBNETS is required for the fargate runner".into(),
        ));
    }
    let security_groups: Vec<String> = optional(get, "SWARM_WEB_ECS_SECURITY_GROUPS")
        .map(|value| {
            value
                .split(',')
                .map(|part| part.trim().to_string())
                .filter(|part| !part.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let trusted_authors = optional(get, "SWARM_WEB_TRUSTED_AUTHORS")
        .map(|value| {
            value
                .split(',')
                .map(|part| part.trim().to_string())
                .filter(|part| !part.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let mut plain_env = BTreeMap::new();
    let mut secret_env = BTreeMap::new();
    for name in [
        "SWARM_STORAGE_BACKEND",
        "SWARM_STORAGE_POSTGRES_DRIVER",
        "SWARM_STORAGE_S3_ENDPOINT",
        "SWARM_STORAGE_S3_BUCKET",
        "SWARM_STORAGE_S3_REGION",
        "SWARM_JOB_STORAGE",
    ] {
        if let Some(value) = optional(get, name) {
            plain_env.insert(name.to_string(), value);
        }
    }
    for name in [
        "SWARM_STORAGE_POSTGRES_DSN",
        "SWARM_STORAGE_S3_ACCESS_KEY",
        "SWARM_STORAGE_S3_SECRET_KEY",
    ] {
        if let Some(value) = optional(get, name) {
            secret_env.insert(name.to_string(), Secret::new(value));
        }
    }
    Ok(Some(JobConfig {
        backend,
        image,
        docker_bin: PathBuf::from(
            optional(get, "SWARM_WEB_DOCKER_BIN").unwrap_or_else(|| "docker".into()),
        ),
        docker_network: optional(get, "SWARM_WEB_DOCKER_NETWORK")
            .unwrap_or_else(|| "bridge".into()),
        ecs_endpoint: endpoint,
        ecs_region: region,
        ecs_cluster: optional(get, "SWARM_WEB_ECS_CLUSTER").unwrap_or_else(|| "swarm".into()),
        ecs_task_definition: optional(get, "SWARM_WEB_ECS_TASK_DEFINITION")
            .unwrap_or_else(|| "swarm-worker".into()),
        ecs_subnets: subnets,
        ecs_security_groups: security_groups,
        ecs_access_key: optional(get, "SWARM_WEB_ECS_ACCESS_KEY_ID").map(Secret::new),
        ecs_secret_key: optional(get, "SWARM_WEB_ECS_SECRET_ACCESS_KEY").map(Secret::new),
        cpu_millis,
        memory_mib,
        max_runtime_secs,
        quota_resume_secs,
        poll_secs,
        provider,
        app_id,
        private_key,
        python: PathBuf::from(
            optional(get, "SWARM_WEB_PYTHON").unwrap_or_else(|| "python3".into()),
        ),
        worker_dir: PathBuf::from(
            optional(get, "SWARM_WEB_WORKER_DIR").unwrap_or_else(|| "../issue_worker".into()),
        ),
        trusted_authors,
        plain_env,
        secret_env,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Provider;
    use base64::Engine;
    use std::collections::HashMap;

    fn env(extra: &[(&str, &str)]) -> HashMap<String, String> {
        let key = base64::engine::general_purpose::STANDARD.encode([3u8; 32]);
        let mut map: HashMap<String, String> = [
            ("SWARM_WEB_PUBLIC_URL", "https://swarm.example.com/"),
            ("SWARM_WEB_GITHUB_CLIENT_ID", "Iv1.abc"),
            ("SWARM_WEB_GITHUB_CLIENT_SECRET", "client-secret-value"),
            ("SWARM_WEB_GITHUB_WEBHOOK_SECRET", "webhook-secret-value"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        map.insert("SWARM_WEB_LOCAL_KEY".into(), key);
        for (k, v) in extra {
            map.insert(k.to_string(), v.to_string());
        }
        map
    }

    fn load(map: &HashMap<String, String>) -> Result<(Config, LocalKeyWrapper), ConfigError> {
        Config::from_lookup(&|name| map.get(name).cloned())
    }

    #[test]
    fn loads_defaults_and_derives_the_origin() {
        let (config, _) = load(&env(&[])).unwrap();
        assert_eq!(config.origin(), "https://swarm.example.com");
        assert!(config.cookie_secure());
        assert_eq!(config.default_plan.max_concurrent_jobs, 2);
        assert!(!format!("{:?}", config.github.client_secret).contains("client-secret-value"));
        assert!(config.jobs.is_none());
    }

    #[test]
    fn the_job_runner_is_optional_and_has_no_default_deadline() {
        let error = match load(&env(&[("SWARM_WEB_JOB_RUNNER", "docker")])) {
            Err(error) => error,
            Ok(_) => panic!("enabling the runner without an image must fail"),
        };
        assert!(error.0.contains("SWARM_WEB_WORKER_IMAGE"));
        let (config, _) = load(&env(&[
            ("SWARM_WEB_JOB_RUNNER", "docker"),
            ("SWARM_WEB_WORKER_IMAGE", "swarm-automation-worker"),
            ("SWARM_WEB_GITHUB_APP_ID", "123"),
            (
                "SWARM_WEB_GITHUB_APP_PRIVATE_KEY",
                "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----",
            ),
        ]))
        .unwrap();
        let jobs = config.jobs.expect("runner enabled");
        assert!(jobs.max_runtime_secs.is_none());
        assert_eq!(jobs.quota_resume_secs, 60);
        assert_eq!(jobs.provider, Provider::Claude);
        assert!(!format!("{:?}", jobs.private_key).contains("BEGIN"));
    }

    #[test]
    fn missing_or_invalid_settings_fail_loudly() {
        for name in [
            "SWARM_WEB_PUBLIC_URL",
            "SWARM_WEB_GITHUB_CLIENT_ID",
            "SWARM_WEB_GITHUB_CLIENT_SECRET",
            "SWARM_WEB_GITHUB_WEBHOOK_SECRET",
            "SWARM_WEB_LOCAL_KEY",
        ] {
            let mut map = env(&[]);
            map.remove(name);
            assert!(load(&map).is_err(), "{name} must be required");
        }
        assert!(load(&env(&[("SWARM_WEB_PUBLIC_URL", "https://x.example/app")])).is_err());
        assert!(load(&env(&[("SWARM_WEB_INTERNAL_TOKEN", "short")])).is_err());
        assert!(
            load(&env(&[("SWARM_WEB_KMS_KEY_ID", "arn:aws:kms:x")])).is_ok(),
            "a local key wins in development"
        );
    }

    #[test]
    fn a_kms_key_without_a_wrapper_is_refused_not_ignored() {
        let mut map = env(&[("SWARM_WEB_KMS_KEY_ID", "arn:aws:kms:x")]);
        map.remove("SWARM_WEB_LOCAL_KEY");
        let error = load(&map).err().expect("must fail");
        assert!(error.0.contains("KMS"));
    }
}
