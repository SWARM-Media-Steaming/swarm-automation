//! Shared application state.

use std::ops::Deref;
use std::sync::{Arc, Mutex};

use crate::clock::Clock;
use crate::config::Config;
use crate::crypto::KeyWrapper;
use crate::github::GitHubClient;
use crate::orchestrator::Orchestrator;
use crate::store::Store;
use crate::usage::Accounting;
use crate::vault::Vault;

pub struct Inner {
    pub config: Config,
    pub store: Arc<dyn Store>,
    pub github: Arc<dyn GitHubClient>,
    pub vault: Vault,
    pub accounting: Accounting,
    pub clock: Arc<dyn Clock>,
    /// Attached after construction so the router and the process can share it.
    /// `None` until a job runner is configured; webhook deliveries stay recorded.
    pub jobs: Mutex<Option<Arc<Orchestrator>>>,
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
        let vault = Vault::new(store.clone(), wrapper, clock.clone());
        let accounting = Accounting::new(store.clone(), clock.clone(), config.default_plan);
        AppState(Arc::new(Inner {
            config,
            store,
            github,
            vault,
            accounting,
            clock,
            jobs: Mutex::new(None),
        }))
    }

    pub fn set_orchestrator(&self, orchestrator: Arc<Orchestrator>) {
        if let Ok(mut jobs) = self.jobs.lock() {
            *jobs = Some(orchestrator);
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
