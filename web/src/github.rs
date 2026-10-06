//! GitHub App sign-in and webhook verification.
//!
//! Sign-in is the GitHub App's user-to-server OAuth flow. Tenants are the App's
//! installations (the same installations `issue_worker/github_app_auth.py`
//! mints installation tokens for). The user's OAuth token is used for the
//! callback's three reads and then dropped: it is never stored or returned.

use async_trait::async_trait;
use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::Sha256;

use crate::model::{InstallationInfo, Role};
use crate::secret::Secret;

#[derive(Debug)]
pub struct GitHubError(pub String);

impl std::fmt::Display for GitHubError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for GitHubError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GitHubUser {
    pub id: u64,
    pub login: String,
}

/// What the backend needs from GitHub. A trait so tests (and a future GitHub
/// Enterprise setup) can stand in for the real service.
#[async_trait]
pub trait GitHubClient: Send + Sync {
    async fn exchange_code(
        &self,
        code: &str,
        redirect_uri: &str,
        code_verifier: &str,
    ) -> Result<Secret, GitHubError>;
    async fn user(&self, token: &Secret) -> Result<GitHubUser, GitHubError>;
    /// The App's installations this user can access, each with the role GitHub
    /// grants the user on it ([`Role::Owner`] for their own account or an
    /// organization they administer).
    async fn installations(
        &self,
        token: &Secret,
        user: &GitHubUser,
    ) -> Result<Vec<InstallationInfo>, GitHubError>;
}

/// The authorize URL for the App's sign-in: `state` binds the round trip to the
/// browser, and the PKCE challenge binds the code to this client.
pub fn authorize_url(
    web_base: &str,
    client_id: &str,
    redirect_uri: &str,
    state: &str,
    code_challenge: &str,
) -> String {
    let mut url = url::Url::parse(&format!("{web_base}/login/oauth/authorize"))
        .expect("static base is a valid URL");
    url.query_pairs_mut()
        .append_pair("client_id", client_id)
        .append_pair("redirect_uri", redirect_uri)
        .append_pair("state", state)
        .append_pair("code_challenge", code_challenge)
        .append_pair("code_challenge_method", "S256");
    url.into()
}

/// PKCE S256: `base64url(sha256(verifier))`.
pub fn pkce_challenge(verifier: &str) -> String {
    use base64::Engine;
    use sha2::Digest;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()))
}

type HmacSha256 = Hmac<Sha256>;

/// Verify `X-Hub-Signature-256` (`sha256=<hex>`) over the raw body in constant
/// time. Anything malformed is simply "not valid".
pub fn verify_webhook_signature(secret: &[u8], body: &[u8], header: &str) -> bool {
    let Some(hex_digest) = header.trim().strip_prefix("sha256=") else {
        return false;
    };
    let Ok(expected) = hex::decode(hex_digest) else {
        return false;
    };
    let Ok(mut mac) = HmacSha256::new_from_slice(secret) else {
        return false;
    };
    mac.update(body);
    mac.verify_slice(&expected).is_ok()
}

/// The signature GitHub would send. Used by tests and operator tooling.
pub fn sign_webhook(secret: &[u8], body: &[u8]) -> String {
    let mut mac = HmacSha256::new_from_slice(secret).expect("HMAC accepts any key length");
    mac.update(body);
    format!("sha256={}", hex::encode(mac.finalize().into_bytes()))
}

/// The real client, talking to github.com (or a configured base).
pub struct HttpGitHub {
    client_id: String,
    client_secret: Secret,
    web_base: String,
    api_base: String,
    http: reqwest::Client,
}

impl HttpGitHub {
    pub fn new(
        client_id: String,
        client_secret: Secret,
        web_base: String,
        api_base: String,
    ) -> Result<Self, GitHubError> {
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(15))
            .redirect(reqwest::redirect::Policy::none())
            .user_agent("swarm-automation-web")
            .build()
            .map_err(|e| GitHubError(format!("could not build the HTTP client: {e}")))?;
        Ok(HttpGitHub {
            client_id,
            client_secret,
            web_base,
            api_base,
            http,
        })
    }

    async fn get<T: for<'de> Deserialize<'de>>(
        &self,
        token: &Secret,
        path: &str,
    ) -> Result<T, GitHubError> {
        let response = self
            .http
            .get(format!("{}{path}", self.api_base))
            .bearer_auth(token.expose())
            .header("Accept", "application/vnd.github+json")
            .header("X-GitHub-Api-Version", "2022-11-28")
            .send()
            .await
            .map_err(|e| GitHubError(format!("GitHub request failed: {e}")))?;
        if !response.status().is_success() {
            return Err(GitHubError(format!(
                "GitHub returned HTTP {} for {path}",
                response.status().as_u16()
            )));
        }
        response
            .json::<T>()
            .await
            .map_err(|e| GitHubError(format!("GitHub sent an unreadable response: {e}")))
    }
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: Option<String>,
    error: Option<String>,
}

#[derive(Deserialize)]
struct UserResponse {
    id: u64,
    login: String,
}

#[derive(Deserialize)]
struct InstallationsResponse {
    installations: Vec<RawInstallation>,
}

#[derive(Deserialize)]
struct RawInstallation {
    id: u64,
    account: Option<RawAccount>,
    suspended_at: Option<String>,
}

#[derive(Deserialize)]
struct RawAccount {
    login: String,
    #[serde(rename = "type")]
    kind: String,
}

#[derive(Deserialize)]
struct OrgMembership {
    role: String,
    state: String,
}

#[async_trait]
impl GitHubClient for HttpGitHub {
    async fn exchange_code(
        &self,
        code: &str,
        redirect_uri: &str,
        code_verifier: &str,
    ) -> Result<Secret, GitHubError> {
        let response = self
            .http
            .post(format!("{}/login/oauth/access_token", self.web_base))
            .header("Accept", "application/json")
            .form(&[
                ("client_id", self.client_id.as_str()),
                ("client_secret", self.client_secret.expose()),
                ("code", code),
                ("redirect_uri", redirect_uri),
                ("code_verifier", code_verifier),
            ])
            .send()
            .await
            .map_err(|e| GitHubError(format!("token exchange failed: {e}")))?;
        let status = response.status();
        let body: TokenResponse = response.json().await.map_err(|e| {
            GitHubError(format!(
                "token exchange returned an unreadable response (HTTP {status}): {e}"
            ))
        })?;
        match body.access_token {
            Some(token) if !token.is_empty() => Ok(Secret::new(token)),
            _ => Err(GitHubError(format!(
                "token exchange was refused: {}",
                body.error.unwrap_or_else(|| "no access token".into())
            ))),
        }
    }

    async fn user(&self, token: &Secret) -> Result<GitHubUser, GitHubError> {
        let user: UserResponse = self.get(token, "/user").await?;
        Ok(GitHubUser {
            id: user.id,
            login: user.login,
        })
    }

    async fn installations(
        &self,
        token: &Secret,
        user: &GitHubUser,
    ) -> Result<Vec<InstallationInfo>, GitHubError> {
        let mut out = Vec::new();
        for page in 1..=5 {
            let batch: InstallationsResponse = self
                .get(
                    token,
                    &format!("/user/installations?per_page=100&page={page}"),
                )
                .await?;
            let count = batch.installations.len();
            for raw in batch.installations {
                let Some(account) = raw.account else { continue };
                if raw.suspended_at.is_some() {
                    continue;
                }
                let viewer_role = if account.kind == "User" {
                    if account.login.eq_ignore_ascii_case(&user.login) {
                        Role::Owner
                    } else {
                        Role::Member
                    }
                } else {
                    // Administering the organization is what grants ownership.
                    // Needs the App's "Organization members: read" permission;
                    // without it the call fails and the user is a member.
                    let membership: Result<OrgMembership, _> = self
                        .get(token, &format!("/user/memberships/orgs/{}", account.login))
                        .await;
                    match membership {
                        Ok(m) if m.state == "active" && m.role == "admin" => Role::Owner,
                        _ => Role::Member,
                    }
                };
                out.push(InstallationInfo {
                    id: raw.id,
                    account_login: account.login,
                    account_type: account.kind,
                    viewer_role,
                });
            }
            if count < 100 {
                break;
            }
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signature_round_trips_and_rejects_everything_else() {
        let secret = b"hook-secret";
        let body = br#"{"zen":"ok"}"#;
        let good = sign_webhook(secret, body);
        assert!(verify_webhook_signature(secret, body, &good));
        assert!(!verify_webhook_signature(b"other", body, &good));
        assert!(!verify_webhook_signature(
            secret,
            b"{\"zen\":\"no\"}",
            &good
        ));
        assert!(!verify_webhook_signature(secret, body, ""));
        assert!(!verify_webhook_signature(secret, body, "sha256="));
        assert!(!verify_webhook_signature(secret, body, "sha256=zz"));
        assert!(!verify_webhook_signature(
            secret,
            body,
            &good.replace("sha256=", "sha1=")
        ));
        assert!(!verify_webhook_signature(
            secret,
            body,
            &good[..good.len() - 2]
        ));
    }

    #[test]
    fn pkce_challenge_matches_the_rfc_vector() {
        // RFC 7636, appendix B.
        assert_eq!(
            pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
    }

    #[test]
    fn the_authorize_url_carries_state_and_pkce() {
        let url = authorize_url(
            "https://github.com",
            "Iv1.abc",
            "https://x.example/cb",
            "st",
            "ch",
        );
        assert!(url.starts_with("https://github.com/login/oauth/authorize?"));
        assert!(url.contains("client_id=Iv1.abc") && url.contains("state=st"));
        assert!(url.contains("code_challenge=ch") && url.contains("code_challenge_method=S256"));
        assert!(url.contains("redirect_uri=https%3A%2F%2Fx.example%2Fcb"));
    }
}
