//! GitHub sign-in through the OAuth device flow.
//!
//! Only the public client ID is used. The device flow never needs the client
//! secret, so none is read, stored, or sent. The token it returns is handed to
//! `gh auth login --with-token`, so `gh` stays the single store of GitHub
//! credentials and every existing `gh`/`git` call keeps working unchanged.

use serde::{Deserialize, Serialize};
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::tools::enhanced_path;

/// Public client ID of the SWARM Automation OAuth App (device flow enabled).
pub const GITHUB_OAUTH_CLIENT_ID: &str = "Ov23lik7lokRZzMmxb6w";
const DEVICE_CODE_URL: &str = "https://github.com/login/device/code";
const TOKEN_URL: &str = "https://github.com/login/oauth/access_token";
const DEVICE_GRANT: &str = "urn:ietf:params:oauth:grant-type:device_code";
const SCOPES: &str = "repo workflow";
const EXPIRED_MESSAGE: &str = "The GitHub sign-in code expired. Start sign-in again.";

/// What the user needs to see to finish approving the sign-in on GitHub.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeviceCodePrompt {
    pub user_code: String,
    pub verification_uri: String,
}

#[derive(Debug, Deserialize)]
struct DeviceCodeResponse {
    device_code: String,
    user_code: String,
    verification_uri: String,
    expires_in: u64,
    interval: u64,
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    access_token: Option<String>,
    error: Option<String>,
    error_description: Option<String>,
    interval: Option<u64>,
}

#[derive(Debug, PartialEq, Eq)]
enum TokenPoll {
    Pending,
    SlowDown(u64),
    Token(String),
    Denied,
    Expired,
    Failed(String),
}

/// Run the device flow end to end and return the signed-in GitHub login.
///
/// `announce` is called once with the code the user must enter on GitHub.
/// Blocks until the user approves, denies, or the code expires, so call it
/// from a blocking thread.
pub fn sign_in(gh: &Path, announce: impl FnOnce(&DeviceCodePrompt)) -> Result<String, String> {
    let device = request_device_code()?;
    announce(&DeviceCodePrompt {
        user_code: device.user_code.clone(),
        verification_uri: device.verification_uri.clone(),
    });

    let started = Instant::now();
    let deadline = Duration::from_secs(device.expires_in);
    let mut interval = device.interval.max(5);
    loop {
        std::thread::sleep(Duration::from_secs(interval));
        if started.elapsed() >= deadline {
            return Err(EXPIRED_MESSAGE.into());
        }
        match poll_token(&device.device_code)? {
            TokenPoll::Pending => {}
            TokenPoll::SlowDown(next) => interval = next.max(interval + 5),
            TokenPoll::Token(token) => {
                store_token(gh, &token)?;
                return verified_login(gh);
            }
            TokenPoll::Denied => return Err("Sign-in was cancelled on GitHub.".into()),
            TokenPoll::Expired => return Err(EXPIRED_MESSAGE.into()),
            TokenPoll::Failed(message) => return Err(message),
        }
    }
}

fn request_device_code() -> Result<DeviceCodeResponse, String> {
    let body = post_form(
        DEVICE_CODE_URL,
        &[("client_id", GITHUB_OAUTH_CLIENT_ID), ("scope", SCOPES)],
    )?;
    serde_json::from_str::<DeviceCodeResponse>(&body).map_err(|_| {
        format!("GitHub did not start a sign-in: {}", describe_error(&body))
    })
}

fn poll_token(device_code: &str) -> Result<TokenPoll, String> {
    let body = post_form(
        TOKEN_URL,
        &[
            ("client_id", GITHUB_OAUTH_CLIENT_ID),
            ("device_code", device_code),
            ("grant_type", DEVICE_GRANT),
        ],
    )?;
    Ok(parse_token_response(&body))
}

fn parse_token_response(body: &str) -> TokenPoll {
    let Ok(response) = serde_json::from_str::<TokenResponse>(body) else {
        return TokenPoll::Failed(format!("Unexpected GitHub response: {}", describe_error(body)));
    };
    if let Some(token) = response.access_token.filter(|token| !token.is_empty()) {
        return TokenPoll::Token(token);
    }
    match response.error.as_deref() {
        Some("authorization_pending") => TokenPoll::Pending,
        Some("slow_down") => TokenPoll::SlowDown(response.interval.unwrap_or(5)),
        Some("access_denied") => TokenPoll::Denied,
        Some("expired_token") => TokenPoll::Expired,
        Some(code) => TokenPoll::Failed(
            response
                .error_description
                .filter(|text| !text.is_empty())
                .unwrap_or_else(|| format!("GitHub sign-in failed: {code}.")),
        ),
        None => TokenPoll::Failed("GitHub sign-in returned no token.".into()),
    }
}

/// Hand the token to `gh`. The token is written to stdin and never placed in
/// arguments, the environment, or any error message.
fn store_token(gh: &Path, token: &str) -> Result<(), String> {
    let mut child = Command::new(gh)
        .args(["auth", "login", "--hostname", "github.com", "--with-token"])
        .env("PATH", enhanced_path())
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("Could not start gh: {error}"))?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(token.as_bytes())
            .map_err(|error| format!("Could not pass the token to gh: {error}"))?;
    }
    let output = child
        .wait_with_output()
        .map_err(|error| format!("gh did not finish sign-in: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "gh could not store the GitHub token: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    // Make git's HTTPS pushes use the same credentials; best effort because
    // the sign-in itself already succeeded.
    let _ = Command::new(gh)
        .args(["auth", "setup-git", "--hostname", "github.com"])
        .env("PATH", enhanced_path())
        .output();
    Ok(())
}

fn verified_login(gh: &Path) -> Result<String, String> {
    let output = Command::new(gh)
        .args(["api", "user", "--jq", ".login"])
        .env("PATH", enhanced_path())
        .output()
        .map_err(|error| format!("Could not run gh: {error}"))?;
    let login = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if output.status.success() && !login.is_empty() {
        Ok(login)
    } else {
        Err("Signed in, but gh could not confirm the GitHub account.".into())
    }
}

/// POST a form with `curl`, which ships with macOS, so no HTTP crate is needed.
/// Returns the response body. Form values are passed as `--data-urlencode`
/// arguments; the client secret is never among them.
fn post_form(url: &str, fields: &[(&str, &str)]) -> Result<String, String> {
    let mut command = Command::new("curl");
    command.args(["-sS", "-X", "POST", "-H", "Accept: application/json"]);
    for (name, value) in fields {
        command.arg("--data-urlencode").arg(format!("{name}={value}"));
    }
    command.arg(url);
    let output = command
        .output()
        .map_err(|error| format!("Could not reach GitHub: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "Could not reach GitHub: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn describe_error(body: &str) -> String {
    serde_json::from_str::<TokenResponse>(body)
        .ok()
        .and_then(|response| response.error_description.or(response.error))
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| "no details returned".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pending_and_slow_down_keep_polling() {
        assert_eq!(
            parse_token_response(r#"{"error":"authorization_pending"}"#),
            TokenPoll::Pending
        );
        assert_eq!(
            parse_token_response(r#"{"error":"slow_down","interval":10}"#),
            TokenPoll::SlowDown(10)
        );
        assert_eq!(
            parse_token_response(r#"{"error":"slow_down"}"#),
            TokenPoll::SlowDown(5)
        );
    }

    #[test]
    fn token_response_yields_the_token() {
        assert_eq!(
            parse_token_response(r#"{"access_token":"gho_example","token_type":"bearer"}"#),
            TokenPoll::Token("gho_example".into())
        );
    }

    #[test]
    fn terminal_errors_stop_polling_with_a_clear_outcome() {
        assert_eq!(
            parse_token_response(r#"{"error":"access_denied"}"#),
            TokenPoll::Denied
        );
        assert_eq!(
            parse_token_response(r#"{"error":"expired_token"}"#),
            TokenPoll::Expired
        );
        assert_eq!(
            parse_token_response(
                r#"{"error":"incorrect_client_credentials","error_description":"Bad client."}"#
            ),
            TokenPoll::Failed("Bad client.".into())
        );
    }

    #[test]
    fn unparseable_body_is_a_failure_not_a_panic() {
        assert!(matches!(
            parse_token_response("<html>502</html>"),
            TokenPoll::Failed(_)
        ));
    }
}
