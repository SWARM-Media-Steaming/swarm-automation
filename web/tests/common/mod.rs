//! Shared harness: the real router over an in-memory store, a fake GitHub, a
//! manual clock and a log capture. Everything goes through HTTP the way a
//! browser or GitHub would.
#![allow(dead_code)]

use std::collections::HashMap;
use std::io::Write;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{header, HeaderMap, Method, Request, StatusCode};
use axum::Router;
use base64::Engine;
use http_body_util::BodyExt;
use serde_json::Value;
use swarm_web::auth::{csrf_cookie_name, session_cookie_name};
use swarm_web::clock::ManualClock;
use swarm_web::config::Config;
use swarm_web::github::{GitHubClient, GitHubError, GitHubIdentity, GitHubUser};
use swarm_web::identity::{IdentityProvider, IdentityProviders};
use swarm_web::memory::MemoryStore;
use swarm_web::model::{InstallationInfo, Role};
use swarm_web::secret::Secret;
use swarm_web::state::AppState;
use tower::ServiceExt;
use tracing_subscriber::fmt::MakeWriter;

pub const WEBHOOK_SECRET: &str = "whsec_test_webhook_secret_value";
pub const CLIENT_SECRET: &str = "ghcs_test_client_secret_value";
pub const INTERNAL_TOKEN: &str = "internal-token-0123456789abcdef";
pub const START: u64 = 1_767_225_600; // 2026-01-01T00:00:00Z

pub fn installation(id: u64, login: &str, kind: &str, role: Role) -> InstallationInfo {
    InstallationInfo {
        id,
        account_login: login.into(),
        account_type: kind.into(),
        viewer_role: role,
    }
}

/// Stands in for github.com: each OAuth `code` maps to a user and the
/// installations that user can see.
#[derive(Default)]
pub struct FakeGitHub {
    users: Mutex<HashMap<String, (GitHubUser, Vec<InstallationInfo>)>>,
    pub exchanges: Mutex<Vec<(String, String, String)>>,
}

impl FakeGitHub {
    pub fn add_user(&self, code: &str, id: u64, login: &str, installations: Vec<InstallationInfo>) {
        self.users.lock().unwrap().insert(
            code.into(),
            (
                GitHubUser {
                    id,
                    login: login.into(),
                    name: Some(format!("{login} Display")),
                    avatar_url: Some(format!("https://avatars.test/u/{id}")),
                },
                installations,
            ),
        );
    }

    pub fn set_installations(&self, code: &str, installations: Vec<InstallationInfo>) {
        self.users
            .lock()
            .unwrap()
            .get_mut(code)
            .expect("known code")
            .1 = installations;
    }

    fn by_token(&self, token: &Secret) -> Result<(GitHubUser, Vec<InstallationInfo>), GitHubError> {
        let code = token.expose().strip_prefix("gho_fake_").unwrap_or("");
        self.users
            .lock()
            .unwrap()
            .get(code)
            .cloned()
            .ok_or_else(|| GitHubError("bad token".into()))
    }
}

#[async_trait]
impl GitHubClient for FakeGitHub {
    async fn exchange_code(
        &self,
        code: &str,
        redirect_uri: &str,
        verifier: &str,
    ) -> Result<Secret, GitHubError> {
        self.exchanges
            .lock()
            .unwrap()
            .push((code.into(), redirect_uri.into(), verifier.into()));
        if self.users.lock().unwrap().contains_key(code) {
            Ok(Secret::new(format!("gho_fake_{code}")))
        } else {
            Err(GitHubError("bad_verification_code".into()))
        }
    }

    async fn user(&self, token: &Secret) -> Result<GitHubUser, GitHubError> {
        Ok(self.by_token(token)?.0)
    }

    async fn installations(
        &self,
        token: &Secret,
        _user: &GitHubUser,
    ) -> Result<Vec<InstallationInfo>, GitHubError> {
        Ok(self.by_token(token)?.1)
    }
}

/// Collects log output so a test can assert on what was (not) written.
#[derive(Clone, Default)]
pub struct LogCapture(Arc<Mutex<Vec<u8>>>);

impl LogCapture {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.0.lock().unwrap()).into_owned()
    }
}

impl Write for LogCapture {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> MakeWriter<'a> for LogCapture {
    type Writer = LogCapture;

    fn make_writer(&'a self) -> LogCapture {
        self.clone()
    }
}

/// `tracing` caches, per callsite, whether any subscriber was interested. With
/// only thread-local subscribers, a parallel test that hits a callsite first can
/// cache "nobody" and silence another test's capture, which would make an
/// "it was never logged" assertion pass for the wrong reason. A process-wide
/// always-interested registry keeps every callsite live; each test's own
/// subscriber still decides what it records.
fn ensure_callsites_live() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        let _ = tracing::subscriber::set_global_default(tracing_subscriber::registry());
    });
}

/// Install a log subscriber for the current thread. `redact` chooses the
/// service's real subscriber (scrubbing writer) or a bare one, which shows what
/// the code *tries* to log before any scrubbing.
pub fn capture_logs(redact: bool) -> (LogCapture, tracing::subscriber::DefaultGuard) {
    ensure_callsites_live();
    let capture = LogCapture::default();
    let guard = if redact {
        tracing::subscriber::set_default(swarm_web::logging::subscriber(capture.clone()))
    } else {
        tracing::subscriber::set_default(
            tracing_subscriber::fmt()
                .json()
                .with_writer(capture.clone())
                .finish(),
        )
    };
    (capture, guard)
}

pub struct Reply {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub body: Vec<u8>,
}

impl Reply {
    pub fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or_else(|_| panic!("not JSON: {}", self.text()))
    }

    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    pub fn set_cookies(&self) -> Vec<String> {
        self.headers
            .get_all(header::SET_COOKIE)
            .iter()
            .map(|v| v.to_str().unwrap().to_string())
            .collect()
    }

    pub fn location(&self) -> String {
        self.headers
            .get(header::LOCATION)
            .map(|v| v.to_str().unwrap().to_string())
            .unwrap_or_default()
    }

    /// The `name=value` pair of a Set-Cookie header, by cookie name.
    pub fn cookie(&self, name: &str) -> Option<String> {
        self.set_cookies().iter().find_map(|c| {
            let pair = c.split(';').next()?;
            let (key, value) = pair.split_once('=')?;
            (key == name).then(|| value.to_string())
        })
    }

    pub fn cookie_header(&self, name: &str) -> Option<String> {
        self.set_cookies()
            .into_iter()
            .find(|c| c.starts_with(&format!("{name}=")))
    }
}

pub struct TestApp {
    pub router: Router,
    pub state: AppState,
    pub store: Arc<MemoryStore>,
    pub github: Arc<FakeGitHub>,
    pub clock: Arc<ManualClock>,
    pub secure: bool,
    pub ui: tempfile::TempDir,
}

impl TestApp {
    pub fn new() -> Self {
        Self::with_url("http://swarm.test")
    }

    pub fn with_url(public_url: &str) -> Self {
        Self::build(public_url, true)
    }

    /// No `SWARM_WEB_INTERNAL_TOKEN`: the operator-only API must be off.
    pub fn without_internal_api() -> Self {
        Self::build("http://swarm.test", false)
    }

    /// Extra environment, e.g. `SWARM_WEB_SSE_HEARTBEAT_SECS`.
    pub fn with_env(extra: &[(&str, &str)]) -> Self {
        Self::build_with("http://swarm.test", true, extra)
    }

    /// A second sign-in method beside GitHub.
    pub fn with_identity_provider(provider: Arc<dyn IdentityProvider>) -> Self {
        Self::build_providers("http://swarm.test", true, &[], vec![provider])
    }

    fn build(public_url: &str, internal_api: bool) -> Self {
        Self::build_with(public_url, internal_api, &[])
    }

    fn build_with(public_url: &str, internal_api: bool, extra: &[(&str, &str)]) -> Self {
        Self::build_providers(public_url, internal_api, extra, Vec::new())
    }

    fn build_providers(
        public_url: &str,
        internal_api: bool,
        extra: &[(&str, &str)],
        providers: Vec<Arc<dyn IdentityProvider>>,
    ) -> Self {
        ensure_callsites_live();
        let ui = tempfile::tempdir().unwrap();
        std::fs::write(
            ui.path().join("index.html"),
            "<!doctype html><title>SWARM</title>",
        )
        .unwrap();
        std::fs::write(ui.path().join("app.js"), "// app").unwrap();
        std::fs::write(ui.path().join("app.test.js"), "// test").unwrap();
        std::fs::write(ui.path().join("notes.md"), "# notes").unwrap();
        std::fs::write(ui.path().join(".secret"), "hidden").unwrap();
        let key = base64::engine::general_purpose::STANDARD.encode([42u8; 32]);
        let mut env: HashMap<&str, String> = HashMap::from([
            ("SWARM_WEB_PUBLIC_URL", public_url.to_string()),
            ("SWARM_WEB_GITHUB_CLIENT_ID", "Iv1.testclient".to_string()),
            ("SWARM_WEB_GITHUB_CLIENT_SECRET", CLIENT_SECRET.to_string()),
            (
                "SWARM_WEB_GITHUB_WEBHOOK_SECRET",
                WEBHOOK_SECRET.to_string(),
            ),
            ("SWARM_WEB_GITHUB_APP_SLUG", "swarm-test".to_string()),
            ("SWARM_WEB_INTERNAL_TOKEN", INTERNAL_TOKEN.to_string()),
            ("SWARM_WEB_LOCAL_KEY", key),
            ("SWARM_WEB_UI_DIR", ui.path().to_string_lossy().into_owned()),
        ]);
        if !internal_api {
            env.remove("SWARM_WEB_INTERNAL_TOKEN");
        }
        for (name, value) in extra {
            env.insert(name, value.to_string());
        }
        let (config, wrapper) =
            Config::from_lookup(&|name| env.get(name).cloned()).expect("test config");
        let store = Arc::new(MemoryStore::new());
        let github = Arc::new(FakeGitHub::default());
        let clock = Arc::new(ManualClock::new(START));
        let secure = config.cookie_secure();
        let mut identity = IdentityProviders::new().with(Arc::new(GitHubIdentity::new(
            github.clone(),
            config.github.web_base.clone(),
            config.github.client_id.clone(),
        )));
        for provider in providers {
            identity = identity.with(provider);
        }
        let state = AppState::with_identity_providers(
            config,
            store.clone(),
            github.clone(),
            identity,
            Arc::new(wrapper),
            clock.clone(),
        );
        TestApp {
            router: swarm_web::routes::router(state.clone()),
            state,
            store,
            github,
            clock,
            secure,
            ui,
        }
    }

    pub async fn call(&self, request: Request<Body>) -> Reply {
        let response = self
            .router
            .clone()
            .oneshot(request)
            .await
            .expect("router is infallible");
        let (parts, body) = response.into_parts();
        let body = body.collect().await.unwrap().to_bytes().to_vec();
        Reply {
            status: parts.status,
            headers: parts.headers,
            body,
        }
    }

    pub async fn get(&self, path: &str) -> Reply {
        self.call(
            Request::builder()
                .method(Method::GET)
                .uri(path)
                .body(Body::empty())
                .unwrap(),
        )
        .await
    }

    /// Run the real sign-in round trip for the OAuth `code` the fake knows.
    pub async fn sign_in(&self, code: &str) -> Client<'_> {
        self.sign_in_with("github", code).await
    }

    /// The login redirect and the callback for `provider`; returns the
    /// callback's reply whatever it was.
    pub async fn callback_for(&self, provider: &str, code: &str) -> Reply {
        let login = self.get(&format!("/api/v1/auth/{provider}/login")).await;
        assert_eq!(login.status, StatusCode::FOUND, "login redirects");
        let url = url::Url::parse(&login.location()).unwrap();
        let state = url
            .query_pairs()
            .find(|(k, _)| k == "state")
            .unwrap()
            .1
            .to_string();
        let oauth_name = if self.secure {
            "__Secure-swarm_oauth"
        } else {
            "swarm_oauth"
        };
        let oauth = login.cookie(oauth_name).expect("oauth cookie");
        self.call(
            Request::builder()
                .uri(format!(
                    "/api/v1/auth/{provider}/callback?code={code}&state={state}"
                ))
                .header(header::COOKIE, format!("{oauth_name}={oauth}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
    }

    pub async fn sign_in_with(&self, provider: &str, code: &str) -> Client<'_> {
        let callback = self.callback_for(provider, code).await;
        assert_eq!(
            callback.status,
            StatusCode::FOUND,
            "callback: {}",
            callback.text()
        );
        let session = callback
            .cookie(&session_cookie_name(self.secure))
            .expect("session cookie");
        let csrf = callback
            .cookie(&csrf_cookie_name(self.secure))
            .expect("csrf cookie");
        Client {
            app: self,
            session,
            csrf,
        }
    }
}

/// A signed-in browser.
pub struct Client<'a> {
    pub app: &'a TestApp,
    pub session: String,
    pub csrf: String,
}

impl Client<'_> {
    fn builder(&self, method: Method, path: &str) -> axum::http::request::Builder {
        Request::builder().method(method).uri(path).header(
            header::COOKIE,
            format!("{}={}", session_cookie_name(self.app.secure), self.session),
        )
    }

    pub async fn get(&self, path: &str) -> Reply {
        self.app
            .call(self.builder(Method::GET, path).body(Body::empty()).unwrap())
            .await
    }

    /// A state-changing request the way `ui/api.js` sends it: same-origin,
    /// with the CSRF token.
    pub async fn send(&self, method: Method, path: &str, body: Option<Value>) -> Reply {
        let builder = self
            .builder(method, path)
            .header(header::ORIGIN, self.app.state.config.origin())
            .header("x-csrf-token", &self.csrf)
            .header(header::CONTENT_TYPE, "application/json");
        let body = body
            .map(|v| Body::from(v.to_string()))
            .unwrap_or_else(Body::empty);
        self.app.call(builder.body(body).unwrap()).await
    }

    pub async fn put(&self, path: &str, body: Value) -> Reply {
        self.send(Method::PUT, path, Some(body)).await
    }

    pub async fn delete(&self, path: &str) -> Reply {
        self.send(Method::DELETE, path, None).await
    }

    /// The GitHub App installation tenant this user sees for an account login
    /// (not their personal tenant, which carries the same login).
    pub async fn tenant_for(&self, account: &str) -> String {
        let reply = self.get("/api/v1/tenants").await;
        reply.json()["tenants"]
            .as_array()
            .unwrap()
            .iter()
            .find(|t| t["account_login"] == account && t["account_type"] != "Personal")
            .unwrap_or_else(|| panic!("no tenant for {account}: {}", reply.text()))["id"]
            .as_str()
            .unwrap()
            .to_string()
    }
}

pub fn internal(method: Method, path: &str, body: Option<Value>) -> Request<Body> {
    let builder = Request::builder()
        .method(method)
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {INTERNAL_TOKEN}"))
        .header(header::CONTENT_TYPE, "application/json");
    builder
        .body(
            body.map(|v| Body::from(v.to_string()))
                .unwrap_or_else(Body::empty),
        )
        .unwrap()
}

pub fn signed_webhook(event: &str, delivery: &str, body: &str) -> Request<Body> {
    Request::builder()
        .method(Method::POST)
        .uri("/api/v1/webhooks/github")
        .header("x-github-event", event)
        .header("x-github-delivery", delivery)
        .header(
            "x-hub-signature-256",
            swarm_web::github::sign_webhook(WEBHOOK_SECRET.as_bytes(), body.as_bytes()),
        )
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body.to_string()))
        .unwrap()
}

impl Client<'_> {
    /// The tenant created for this user at first sign-in.
    pub async fn personal_tenant(&self) -> String {
        let reply = self.get("/api/v1/tenants").await;
        let personal: Vec<_> = reply.json()["tenants"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|t| t["account_type"] == "Personal")
            .map(|t| t["id"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(
            personal.len(),
            1,
            "exactly one personal tenant: {}",
            reply.text()
        );
        personal[0].clone()
    }
}
