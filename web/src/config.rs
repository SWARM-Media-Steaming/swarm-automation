//! Process configuration, read from the environment (`SWARM_WEB_*`).

use std::net::SocketAddr;
use std::path::PathBuf;

use crate::crypto::LocalKeyWrapper;
use crate::model::PlanQuotas;
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
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
