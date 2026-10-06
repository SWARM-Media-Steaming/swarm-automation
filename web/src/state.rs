//! Shared application state.

use std::ops::Deref;
use std::sync::Arc;

use crate::clock::Clock;
use crate::config::Config;
use crate::crypto::KeyWrapper;
use crate::github::GitHubClient;
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
        }))
    }
}

impl Deref for AppState {
    type Target = Inner;

    fn deref(&self) -> &Inner {
        &self.0
    }
}
