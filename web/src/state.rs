//! Shared application state.

use std::ops::Deref;
use std::sync::{Arc, Mutex};

use crate::bridge::{Bridge, UnconfiguredBridge};
use crate::clock::Clock;
use crate::config::Config;
use crate::crypto::KeyWrapper;
use crate::events::EventHub;
use crate::github::{GitHubClient, GitHubIdentity};
use crate::identity::IdentityProviders;
use crate::orchestrator::Orchestrator;
use crate::store::Store;
use crate::usage::Accounting;
use crate::vault::Vault;

pub struct Inner {
    pub config: Config,
    pub store: Arc<dyn Store>,
    pub github: Arc<dyn GitHubClient>,
    /// The sign-in methods: GitHub today (`identity.rs`).
    pub identity: IdentityProviders,
    pub vault: Vault,
    pub accounting: Accounting,
    pub clock: Arc<dyn Clock>,
    /// Attached after construction so the router and the process can share it.
    /// `None` until a job runner is configured; webhook deliveries stay recorded.
    pub jobs: Mutex<Option<Arc<Orchestrator>>>,
    /// Live updates: replay rings and the SSE streams (`events.rs`).
    pub events: Arc<EventHub>,
    bridge: Mutex<Arc<dyn Bridge>>,
}

#[derive(Clone)]
pub struct AppState(Arc<Inner>);

impl AppState {
    pub fn new(
        config: Config,
        store: Arc<dyn Store>,
        github: Arc<dyn GitHubClient>,
        wrapper: Arc<dyn KeyWrapper>,
        clock: Arc<dyn Clock>,
    ) -> Self {
        let identity = IdentityProviders::new().with(Arc::new(GitHubIdentity::new(
            github.clone(),
            config.github.web_base.clone(),
            config.github.client_id.clone(),
        )));
        Self::with_identity_providers(config, store, github, identity, wrapper, clock)
    }

    /// Like [`AppState::new`] with the sign-in methods named by the caller, for
    /// a deployment (or a test) that adds a provider beside GitHub.
    pub fn with_identity_providers(
        config: Config,
        store: Arc<dyn Store>,
        github: Arc<dyn GitHubClient>,
        identity: IdentityProviders,
        wrapper: Arc<dyn KeyWrapper>,
        clock: Arc<dyn Clock>,
    ) -> Self {
        let vault = Vault::new(store.clone(), wrapper, clock.clone());
        let accounting = Accounting::new(store.clone(), clock.clone(), config.default_plan);
        let events = Arc::new(EventHub::new(config.sse_heartbeat_secs));
        AppState(Arc::new(Inner {
            config,
            store,
            github,
            identity,
            vault,
            accounting,
            clock,
            jobs: Mutex::new(None),
            events,
            bridge: Mutex::new(Arc::new(UnconfiguredBridge)),
        }))
    }

    /// Attach the orchestrator and mirror its log lines into the event hub, where
    /// they are redacted, kept for replay and served on the SSE streams.
    pub fn set_orchestrator(&self, orchestrator: Arc<Orchestrator>) {
        let mut lines = orchestrator.subscribe();
        let hub = self.events.clone();
        let clock = self.clock.clone();
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                loop {
                    match lines.recv().await {
                        Ok(log) => {
                            hub.publish_log(
                                &log.tenant,
                                &log.repository,
                                log.issue,
                                &log.line,
                                clock.now_secs(),
                            );
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                        Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                    }
                }
            });
        }
        if let Ok(mut jobs) = self.jobs.lock() {
            *jobs = Some(orchestrator);
        }
    }

    pub fn set_bridge(&self, bridge: Arc<dyn Bridge>) {
        if let Ok(mut slot) = self.bridge.lock() {
            *slot = bridge;
        }
    }

    pub fn bridge(&self) -> Arc<dyn Bridge> {
        match self.bridge.lock() {
            Ok(slot) => slot.clone(),
            Err(_) => Arc::new(UnconfiguredBridge),
        }
    }

    pub fn orchestrator(&self) -> Option<Arc<Orchestrator>> {
        self.jobs.lock().ok().and_then(|jobs| jobs.clone())
    }
}

impl Deref for AppState {
    type Target = Inner;

    fn deref(&self) -> &Inner {
        &self.0
    }
}
