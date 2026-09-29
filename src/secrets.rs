//! Credentials the app keeps for third-party data sources.
//!
//! The Artificial Analysis API key is optional: with it, model-data refreshes
//! also collect benchmark scores, speed and latency. It lives in the macOS
//! Keychain — never in `config.json`, on a command line, or in a log. The
//! refresh hands it to the Python process through its environment only.

const SERVICE: &str = "app.swarm.automation";
const ARTIFICIAL_ANALYSIS_ACCOUNT: &str = "artificial-analysis-api-key";
const MAX_KEY_LENGTH: usize = 256;

/// Environment variable `issue_worker/model_data_sources.py` reads the key from.
pub const ARTIFICIAL_ANALYSIS_ENV: &str = "ARTIFICIAL_ANALYSIS_API_KEY";

/// A key as entered by a person: trimmed, non-empty, printable, no whitespace.
pub fn validate_key(raw: &str) -> Result<String, String> {
    let key = raw.trim();
    if key.is_empty() {
        return Err("Enter an API key to save.".into());
    }
    if key.len() > MAX_KEY_LENGTH {
        return Err("That API key is too long.".into());
    }
    if key
        .chars()
        .any(|character| character.is_whitespace() || character.is_control())
    {
        return Err("An API key cannot contain spaces or control characters.".into());
    }
    Ok(key.to_string())
}

#[cfg(target_os = "macos")]
mod store {
    use super::{ARTIFICIAL_ANALYSIS_ACCOUNT, SERVICE};

    fn entry() -> Result<keyring::Entry, String> {
        keyring::Entry::new(SERVICE, ARTIFICIAL_ANALYSIS_ACCOUNT)
            .map_err(|error| format!("Keychain is unavailable: {error}"))
    }

    pub fn get() -> Result<Option<String>, String> {
        match entry()?.get_password() {
            Ok(key) => Ok(Some(key).filter(|key| !key.is_empty())),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(error) => Err(format!("Could not read the Keychain: {error}")),
        }
    }

    pub fn set(key: &str) -> Result<(), String> {
        entry()?
            .set_password(key)
            .map_err(|error| format!("Could not save to the Keychain: {error}"))
    }

    pub fn delete() -> Result<(), String> {
        match entry()?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(format!("Could not remove the Keychain entry: {error}")),
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod store {
    const UNSUPPORTED: &str = "Secure key storage is only available on macOS.";

    pub fn get() -> Result<Option<String>, String> {
        Ok(None)
    }

    pub fn set(_key: &str) -> Result<(), String> {
        Err(UNSUPPORTED.into())
    }

    pub fn delete() -> Result<(), String> {
        Ok(())
    }
}

/// The saved key, if any. A Keychain that cannot be read counts as "no key":
/// refreshes then simply run without benchmarks.
pub fn artificial_analysis_key() -> Option<String> {
    store::get().ok().flatten()
}

pub fn store_artificial_analysis_key(raw: &str) -> Result<(), String> {
    store::set(&validate_key(raw)?)
}

pub fn clear_artificial_analysis_key() -> Result<(), String> {
    store::delete()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_are_trimmed_and_validated() {
        assert_eq!(validate_key("  aa_abc123  ").unwrap(), "aa_abc123");
        assert!(validate_key("   ").is_err());
        assert!(validate_key("has space").is_err());
        assert!(validate_key("line\nbreak").is_err());
        assert!(validate_key(&"k".repeat(MAX_KEY_LENGTH + 1)).is_err());
    }

    #[test]
    fn a_rejected_key_is_never_echoed_in_the_error() {
        let secret = "sk-secret value";
        let message = validate_key(secret).unwrap_err();
        assert!(!message.contains("sk-secret"));
    }
}
