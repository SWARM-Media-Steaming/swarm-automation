//! Identity providers: how a person proves who they are (issue #440, part of #413).
//!
//! The sign-in callback is written against [`IdentityProvider`], an OIDC-style
//! surface: where to send the browser, how to turn the returned code into a
//! token, and what the provider says about the person (a stable `subject` and a
//! profile). Only GitHub implements it (`GitHubIdentity` in `github.rs`); there
//! is no username, email or password login. A provider is added by implementing
//! the trait and registering it in [`IdentityProviders`]; the callback, the
//! store (`user_identities` rows keyed by provider and subject) and the personal
//! tenant it creates do not change.
//!
//! A token is used for the callback's reads and dropped: it is never stored,
//! logged or placed in an error. Errors carry a message about what failed, never
//! the request or the response body.

use std::collections::BTreeMap;
use std::fmt;
use std::sync::Arc;

use async_trait::async_trait;

use crate::model::{IdentityProfile, InstallationInfo};
use crate::secret::Secret;

#[derive(Debug)]
pub struct IdentityError(pub String);

impl fmt::Display for IdentityError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for IdentityError {}

#[async_trait]
pub trait IdentityProvider: Send + Sync {
    /// The provider's name in `user_identities.provider` and in the sign-in
    /// path (`/api/v1/auth/{id}/login`). Lowercase letters only.
    fn id(&self) -> &'static str;

    /// Where to send the browser. `state` binds the round trip to the browser and
    /// `code_challenge` is the PKCE S256 challenge of the verifier the callback
    /// presents to [`exchange_code`](Self::exchange_code).
    fn authorize_url(&self, redirect_uri: &str, state: &str, code_challenge: &str) -> String;

    async fn exchange_code(
        &self,
        code: &str,
        redirect_uri: &str,
        code_verifier: &str,
    ) -> Result<Secret, IdentityError>;

    /// The person the token belongs to.
    async fn profile(&self, token: &Secret) -> Result<IdentityProfile, IdentityError>;

    /// GitHub App installations the person can reach, with their role on each.
    /// Other providers have none.
    async fn installations(
        &self,
        _token: &Secret,
        _profile: &IdentityProfile,
    ) -> Result<Vec<InstallationInfo>, IdentityError> {
        Ok(Vec::new())
    }
}

/// The providers this deployment signs people in with, by [`IdentityProvider::id`].
#[derive(Clone, Default)]
pub struct IdentityProviders(BTreeMap<&'static str, Arc<dyn IdentityProvider>>);

impl IdentityProviders {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with(mut self, provider: Arc<dyn IdentityProvider>) -> Self {
        self.0.insert(provider.id(), provider);
        self
    }

    pub fn get(&self, id: &str) -> Option<Arc<dyn IdentityProvider>> {
        self.0.get(id).cloned()
    }

    pub fn ids(&self) -> Vec<&'static str> {
        self.0.keys().copied().collect()
    }
}

const MAX_LOGIN: usize = 100;
const MAX_SUBJECT: usize = 128;
const MAX_DISPLAY_NAME: usize = 255;
const MAX_AVATAR_URL: usize = 2048;

fn clipped(value: &str, max: usize) -> String {
    value.chars().take(max).collect()
}

/// What a provider returned, made safe to store. A missing subject or login is
/// an error (nobody can be registered without them). The display name is
/// trimmed and clipped, and an avatar that is not an `https` URL is dropped, so
/// nothing a provider sends can place a `javascript:` or oversized value in the
/// database.
pub fn clean_profile(profile: IdentityProfile) -> Result<IdentityProfile, IdentityError> {
    let subject = profile.subject.trim().to_string();
    let login = profile.login.trim().to_string();
    if subject.is_empty() || subject.len() > MAX_SUBJECT || login.is_empty() {
        return Err(IdentityError(
            "the provider sent an incomplete profile".into(),
        ));
    }
    let display_name = profile
        .display_name
        .map(|name| clipped(name.trim(), MAX_DISPLAY_NAME))
        .filter(|name| !name.is_empty());
    let avatar_url = profile
        .avatar_url
        .filter(|url| url.len() <= MAX_AVATAR_URL)
        .filter(|url| {
            url::Url::parse(url)
                .map(|parsed| parsed.scheme() == "https" && parsed.host_str().is_some())
                .unwrap_or(false)
        });
    Ok(IdentityProfile {
        provider: profile.provider,
        subject,
        login: clipped(&login, MAX_LOGIN),
        display_name,
        avatar_url,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile() -> IdentityProfile {
        IdentityProfile {
            provider: "github".into(),
            subject: "42".into(),
            login: "octocat".into(),
            display_name: Some("  The Octocat ".into()),
            avatar_url: Some("https://avatars.githubusercontent.com/u/42".into()),
        }
    }

    #[test]
    fn a_complete_profile_is_kept_and_trimmed() {
        let cleaned = clean_profile(profile()).unwrap();
        assert_eq!(cleaned.display_name.as_deref(), Some("The Octocat"));
        assert_eq!(
            cleaned.avatar_url.as_deref(),
            Some("https://avatars.githubusercontent.com/u/42")
        );
    }

    #[test]
    fn a_profile_without_a_subject_or_login_is_refused() {
        for (subject, login) in [("", "octocat"), (" ", "octocat"), ("42", ""), ("42", "  ")] {
            let mut p = profile();
            p.subject = subject.into();
            p.login = login.into();
            assert!(clean_profile(p).is_err(), "{subject:?} {login:?}");
        }
        let mut p = profile();
        p.subject = "9".repeat(MAX_SUBJECT + 1);
        assert!(clean_profile(p).is_err());
    }

    #[test]
    fn only_an_https_avatar_survives_and_the_name_is_bounded() {
        for url in [
            "javascript:alert(1)",
            "http://example.test/a.png",
            "data:image/png;base64,AAAA",
            "not a url",
            "https://",
        ] {
            let mut p = profile();
            p.avatar_url = Some(url.into());
            assert_eq!(clean_profile(p).unwrap().avatar_url, None, "{url}");
        }
        let mut p = profile();
        p.display_name = Some("n".repeat(MAX_DISPLAY_NAME * 2));
        p.login = "l".repeat(MAX_LOGIN * 2);
        let cleaned = clean_profile(p).unwrap();
        assert_eq!(
            cleaned.display_name.unwrap().chars().count(),
            MAX_DISPLAY_NAME
        );
        assert_eq!(cleaned.login.chars().count(), MAX_LOGIN);
        let mut p = profile();
        p.display_name = Some("   ".into());
        assert_eq!(clean_profile(p).unwrap().display_name, None);
    }

    #[test]
    fn the_registry_finds_a_provider_by_id() {
        let providers = IdentityProviders::new();
        assert!(providers.get("github").is_none());
        assert!(providers.ids().is_empty());
    }
}
