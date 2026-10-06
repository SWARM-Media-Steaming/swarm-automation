//! Per-tenant provider API keys: write-only, envelope-encrypted, injected into
//! a job only for that job's provider.
//!
//! No method here (and no HTTP route) returns a stored key to a caller. The one
//! place plaintext leaves is [`Vault::job_environment`], which the job runner
//! calls in-process to build the environment of a single container.

use std::sync::Arc;

use crate::clock::Clock;
use crate::crypto::{open, seal, secret_aad, KeyWrapper};
use crate::error::ApiError;
use crate::model::{KeyMeta, Provider, TenantId};
use crate::secret::Secret;
use crate::store::Store;

const MIN_KEY_LEN: usize = 8;
const MAX_KEY_LEN: usize = 4096;

/// The environment for one job: only the variables that job may see.
pub struct JobEnvironment {
    vars: Vec<(&'static str, Secret)>,
}

impl JobEnvironment {
    /// `(NAME, value)` pairs for the container. The only reader of the values.
    pub fn expose(&self) -> Vec<(&'static str, &str)> {
        self.vars
            .iter()
            .map(|(name, value)| (*name, value.expose()))
            .collect()
    }

    pub fn names(&self) -> Vec<&'static str> {
        self.vars.iter().map(|(name, _)| *name).collect()
    }
}

impl std::fmt::Debug for JobEnvironment {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "JobEnvironment({:?})", self.names())
    }
}

#[derive(Clone)]
pub struct Vault {
    store: Arc<dyn Store>,
    wrapper: Arc<dyn KeyWrapper>,
    clock: Arc<dyn Clock>,
}

/// Check a submitted key's shape without echoing it anywhere. Provider keys are
/// printable ASCII with no whitespace; anything else is a paste error.
pub fn validate_key(key: &str) -> Result<(), ApiError> {
    if key.len() < MIN_KEY_LEN {
        return Err(ApiError::BadRequest("The key is too short.".into()));
    }
    if key.len() > MAX_KEY_LEN {
        return Err(ApiError::BadRequest("The key is too long.".into()));
    }
    if !key.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(ApiError::BadRequest(
            "The key must be printable text without spaces or line breaks.".into(),
        ));
    }
    Ok(())
}

impl Vault {
    pub fn new(store: Arc<dyn Store>, wrapper: Arc<dyn KeyWrapper>, clock: Arc<dyn Clock>) -> Self {
        Vault {
            store,
            wrapper,
            clock,
        }
    }

    /// Encrypt and store a key, replacing any earlier one. Returns metadata only.
    pub async fn put(
        &self,
        tenant: &TenantId,
        provider: Provider,
        key: &Secret,
        updated_by: &str,
    ) -> Result<(), ApiError> {
        let aad = secret_aad(tenant.as_str(), provider.as_str());
        let sealed = seal(self.wrapper.as_ref(), key.expose().as_bytes(), &aad).await?;
        self.store
            .put_provider_key(tenant, provider, sealed, updated_by, self.clock.now_secs())
            .await?;
        Ok(())
    }

    pub async fn delete(&self, tenant: &TenantId, provider: Provider) -> Result<bool, ApiError> {
        Ok(self.store.delete_provider_key(tenant, provider).await?)
    }

    /// Which keys are configured, never the keys.
    pub async fn list(&self, tenant: &TenantId) -> Result<Vec<KeyMeta>, ApiError> {
        Ok(self.store.provider_key_meta(tenant).await?)
    }

    pub async fn is_configured(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> Result<bool, ApiError> {
        Ok(self.store.provider_key(tenant, provider).await?.is_some())
    }

    async fn reveal(
        &self,
        tenant: &TenantId,
        provider: Provider,
    ) -> Result<Option<Secret>, ApiError> {
        let Some(stored) = self.store.provider_key(tenant, provider).await? else {
            return Ok(None);
        };
        let aad = secret_aad(tenant.as_str(), provider.as_str());
        let plaintext = open(self.wrapper.as_ref(), &stored.sealed, &aad).await?;
        let text = String::from_utf8(plaintext.to_vec())
            .map_err(|_| ApiError::Internal("a stored provider key was not valid text".into()))?;
        Ok(Some(Secret::new(text)))
    }

    /// The environment for one job of `tenant` running on `provider`: exactly
    /// that provider's key, plus the model-data key when the job fetches
    /// benchmark data. Another provider's key is never included.
    pub async fn job_environment(
        &self,
        tenant: &TenantId,
        provider: Provider,
        include_model_data: bool,
    ) -> Result<JobEnvironment, ApiError> {
        let mut wanted = vec![provider];
        if include_model_data && provider != Provider::ModelData {
            wanted.push(Provider::ModelData);
        }
        let mut vars = Vec::new();
        for needed in wanted {
            match self.reveal(tenant, needed).await? {
                Some(secret) => vars.push((needed.env_var(), secret)),
                None if needed == provider => {
                    return Err(ApiError::Conflict(format!(
                        "No {} key is configured for this tenant.",
                        needed.as_str()
                    )))
                }
                None => {}
            }
        }
        Ok(JobEnvironment { vars })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_validation_rejects_paste_errors_without_echoing() {
        assert!(validate_key("sk-ant-api03-abcdefgh").is_ok());
        for bad in [
            "",
            "ab12",
            "has space inside-the-key",
            "line\nbreak-in-key-1",
            "ünïcode-key-1234567",
        ] {
            let error = validate_key(bad).expect_err(bad);
            assert!(bad.is_empty() || !format!("{error:?}").contains(bad));
        }
        assert!(validate_key(&"k".repeat(MAX_KEY_LEN + 1)).is_err());
    }
}
