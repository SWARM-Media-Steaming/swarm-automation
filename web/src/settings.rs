//! A tenant's settings: the app document and one document per repository.
//!
//! The layout is the one `issue_worker/desktop_import.py` writes into the
//! worker's `tenant_config` collection (`app`, and `repo-<id>` per repository),
//! so a hosted job and the importer read what this API saves. The desktop never
//! writes these. Credential-shaped keys and key blocks are dropped on the way
//! in (provider keys have their own write-only endpoints), and so are the
//! settings that only describe a desktop machine.

use std::collections::BTreeSet;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::error::ApiError;
use crate::model::TenantId;
use crate::redact::scrub_tokens;
use crate::store::Store;

pub const COLLECTION: &str = "tenant_config";
pub const APP_KEY: &str = "app";
const SCHEMA: u64 = 1;
const MAX_REPOSITORIES: usize = 200;
const MAX_DEPTH: usize = 12;
const MAX_FILTER: usize = 200;

/// Settings that only describe the desktop machine; they mean nothing hosted.
const LOCAL_ONLY_APP_KEYS: &[&str] = &[
    "workspace_root",
    "worker_state_dir",
    "gh_bin",
    "python_bin",
    "jev_bin",
    "terminal_automation_permission_primed",
    "repo_dir",
    "github_apps_config",
    "claude_bin",
    "codex_bin",
    "profile_name",
];
const LOCAL_ONLY_PROVIDER_KEYS: &[&str] = &["bin"];
const LOCAL_ONLY_REPO_KEYS: &[&str] = &["repo_dir", "github_apps_config"];

const SECRET_FRAGMENTS: &[&str] = &[
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "authorization",
];
const SECRET_NAMES: &[&str] = &[
    "token",
    "access_token",
    "auth_token",
    "refresh_token",
    "bearer",
    "pem",
];

fn credential_like(name: &str) -> bool {
    let lowered = name.trim().to_lowercase().replace('-', "_");
    SECRET_NAMES.contains(&lowered.as_str())
        || SECRET_FRAGMENTS.iter().any(|f| lowered.contains(f))
        || (lowered.ends_with("_token")
            && !lowered.ends_with("_limit")
            && !lowered.ends_with("_count"))
}

/// `value` without credential-shaped keys or key blocks, and the paths dropped
/// (names only; a dropped value is never reported).
pub fn strip_credentials(value: &Value, path: &str) -> (Value, Vec<String>) {
    let mut dropped = Vec::new();
    let clean = match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (key, item) in map {
                let where_ = if path.is_empty() {
                    key.clone()
                } else {
                    format!("{path}.{key}")
                };
                if credential_like(key) {
                    dropped.push(where_);
                    continue;
                }
                let (clean, inner) = strip_credentials(item, &where_);
                dropped.extend(inner);
                out.insert(key.clone(), clean);
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .enumerate()
                .map(|(index, item)| {
                    let (clean, inner) = strip_credentials(item, &format!("{path}[{index}]"));
                    dropped.extend(inner);
                    clean
                })
                .collect(),
        ),
        Value::String(text) => {
            if text.contains("-----BEGIN ") && text.contains("PRIVATE KEY") {
                dropped.push(path.to_string());
                Value::String(String::new())
            } else {
                Value::String(scrub_tokens(text))
            }
        }
        other => other.clone(),
    };
    (clean, dropped)
}

fn depth(value: &Value) -> usize {
    match value {
        Value::Object(map) => 1 + map.values().map(depth).max().unwrap_or(0),
        Value::Array(items) => 1 + items.iter().map(depth).max().unwrap_or(0),
        _ => 0,
    }
}

/// `owner/name` -> `owner__name`, the desktop's repository id.
pub fn repo_slug(repository: &str) -> String {
    repository
        .trim()
        .replace('/', "__")
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || "_-.".contains(c) {
                c
            } else {
                '-'
            }
        })
        .collect()
}

pub fn valid_repo_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 100
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "_-.".contains(c))
        && !id.starts_with('.')
}

/// The document key for a repository: `repo-<id>` when that is a legal storage
/// name, else a digest of the id (the importer's rule, so both agree).
pub fn repo_key(id: &str) -> String {
    let key = format!("repo-{id}");
    let legal = key.len() <= 128
        && key
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        && key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "_-.+".contains(c));
    if legal {
        key
    } else {
        format!("repo-{}", &hex::encode(Sha256::digest(id.as_bytes()))[..24])
    }
}

fn envelope(settings: Map<String, Value>) -> Value {
    json!({ "schema": SCHEMA, "source": "web", "settings": settings })
}

fn settings_of(document: &Value) -> Map<String, Value> {
    document
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn bad(message: &str) -> ApiError {
    ApiError::BadRequest(message.into())
}

/// The settings object the UI binds: the app document plus `repositories`.
pub async fn load(store: &dyn Store, tenant: &TenantId) -> Result<Value, ApiError> {
    let mut app = match store.document(tenant, COLLECTION, APP_KEY).await? {
        Some(document) => settings_of(&document),
        None => Map::new(),
    };
    let mut repositories = Vec::new();
    for (key, document) in store.documents(tenant, COLLECTION).await? {
        if key.starts_with("repo-") {
            repositories.push(Value::Object(settings_of(&document)));
        }
    }
    app.insert("repositories".into(), Value::Array(repositories));
    Ok(Value::Object(app))
}

pub struct Saved {
    pub config: Value,
    pub dropped: Vec<String>,
}

/// A repository's id, its cleaned settings and the credential paths dropped.
type CleanRepository = (String, Map<String, Value>, Vec<String>);

fn clean_repository(repo: &Value, index: usize) -> Result<CleanRepository, ApiError> {
    let Some(map) = repo.as_object() else {
        return Err(bad("Each repository must be an object."));
    };
    let repository = map
        .get("github_repository")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if !repository.is_empty() {
        let mut parts = repository.split('/');
        let ok = matches!(
            (parts.next(), parts.next(), parts.next()),
            (Some(owner), Some(name), None)
                if !owner.is_empty()
                    && !name.is_empty()
                    && repository
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || "_-./".contains(c))
        );
        if !ok {
            return Err(bad("github_repository must look like owner/name."));
        }
    }
    let id = match map.get("id").and_then(Value::as_str).map(str::trim) {
        Some(id) if !id.is_empty() => id.to_string(),
        _ if !repository.is_empty() => repo_slug(&repository),
        _ => {
            return Err(bad(&format!(
                "Repository {} has no id or github_repository.",
                index + 1
            )))
        }
    };
    if !valid_repo_id(&id) {
        return Err(bad(
            "A repository id may use letters, digits, '_', '-' and '.'.",
        ));
    }
    let local: BTreeSet<&str> = LOCAL_ONLY_REPO_KEYS.iter().copied().collect();
    let kept: Map<String, Value> = map
        .iter()
        .filter(|(key, _)| !local.contains(key.as_str()))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    let (clean, dropped) = strip_credentials(&Value::Object(kept), &format!("repositories.{id}"));
    let Value::Object(mut settings) = clean else {
        return Err(bad("Each repository must be an object."));
    };
    settings.insert("id".into(), Value::String(id.clone()));
    Ok((id, settings, dropped))
}

/// Replace the tenant's settings with `config` (the whole `AppConfig` shape).
pub async fn save(store: &dyn Store, tenant: &TenantId, config: &Value) -> Result<Saved, ApiError> {
    let Some(map) = config.as_object() else {
        return Err(bad("The configuration must be a JSON object."));
    };
    if depth(config) > MAX_DEPTH {
        return Err(bad("The configuration is nested too deeply."));
    }
    let mut dropped = Vec::new();
    let mut app = Map::new();
    for (key, value) in map {
        if key == "repositories" {
            continue;
        }
        if LOCAL_ONLY_APP_KEYS.contains(&key.as_str()) {
            continue;
        }
        let value = if key == "providers" {
            match value {
                Value::Array(providers) => Value::Array(
                    providers
                        .iter()
                        .map(|provider| match provider {
                            Value::Object(fields) => Value::Object(
                                fields
                                    .iter()
                                    .filter(|(k, _)| {
                                        !LOCAL_ONLY_PROVIDER_KEYS.contains(&k.as_str())
                                    })
                                    .map(|(k, v)| (k.clone(), v.clone()))
                                    .collect(),
                            ),
                            other => other.clone(),
                        })
                        .collect(),
                ),
                _ => return Err(bad("providers must be a list.")),
            }
        } else {
            value.clone()
        };
        app.insert(key.clone(), value);
    }
    let (app_clean, inner) = strip_credentials(&Value::Object(app), "");
    dropped.extend(inner);
    let Value::Object(app_settings) = app_clean else {
        return Err(bad("The configuration must be a JSON object."));
    };

    let repositories = match map.get("repositories") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items.clone(),
        Some(_) => return Err(bad("repositories must be a list.")),
    };
    if repositories.len() > MAX_REPOSITORIES {
        return Err(bad("Too many repositories."));
    }
    let mut documents: Vec<(String, Map<String, Value>)> = Vec::new();
    let mut seen = BTreeSet::new();
    for (index, repo) in repositories.iter().enumerate() {
        let (id, settings, inner) = clean_repository(repo, index)?;
        if !seen.insert(id.clone()) {
            return Err(bad("Two repositories share an id."));
        }
        dropped.extend(inner);
        documents.push((repo_key(&id), settings));
    }

    store
        .put_document(tenant, COLLECTION, APP_KEY, envelope(app_settings))
        .await?;
    let keep: BTreeSet<String> = documents.iter().map(|(key, _)| key.clone()).collect();
    for (key, settings) in documents {
        store
            .put_document(tenant, COLLECTION, &key, envelope(settings))
            .await?;
    }
    for (key, _) in store.documents(tenant, COLLECTION).await? {
        if key.starts_with("repo-") && !keep.contains(&key) {
            store.delete_document(tenant, COLLECTION, &key).await?;
        }
    }
    dropped.sort();
    Ok(Saved {
        config: load(store, tenant).await?,
        dropped,
    })
}

/// One repository's settings, by id.
pub async fn load_repo(
    store: &dyn Store,
    tenant: &TenantId,
    id: &str,
) -> Result<Option<Value>, ApiError> {
    if !valid_repo_id(id) {
        return Err(bad("That is not a valid repository id."));
    }
    Ok(store
        .document(tenant, COLLECTION, &repo_key(id))
        .await?
        .map(|document| Value::Object(settings_of(&document))))
}

/// Replace one repository's settings. The id comes from the path, never the body.
pub async fn save_repo(
    store: &dyn Store,
    tenant: &TenantId,
    id: &str,
    settings: &Value,
) -> Result<Saved, ApiError> {
    if !valid_repo_id(id) {
        return Err(bad("That is not a valid repository id."));
    }
    let Some(map) = settings.as_object() else {
        return Err(bad("The repository settings must be a JSON object."));
    };
    if depth(settings) > MAX_DEPTH {
        return Err(bad("The configuration is nested too deeply."));
    }
    let mut body = map.clone();
    body.insert("id".into(), Value::String(id.to_string()));
    let (_, clean, dropped) = clean_repository(&Value::Object(body), 0)?;
    store
        .put_document(tenant, COLLECTION, &repo_key(id), envelope(clean.clone()))
        .await?;
    Ok(Saved {
        config: Value::Object(clean),
        dropped,
    })
}

/// The one app setting a member may change: which repositories Feedback shows.
pub async fn set_feedback_filter(
    store: &dyn Store,
    tenant: &TenantId,
    repo_ids: &Value,
) -> Result<Value, ApiError> {
    let Some(items) = repo_ids.as_array() else {
        return Err(bad("repoIds must be a list of repository ids."));
    };
    if items.len() > MAX_FILTER {
        return Err(bad("Too many repository ids."));
    }
    let mut ids = Vec::new();
    for item in items {
        match item.as_str() {
            Some(id) if valid_repo_id(id) => ids.push(Value::String(id.to_string())),
            _ => return Err(bad("repoIds must be a list of repository ids.")),
        }
    }
    let mut app = match store.document(tenant, COLLECTION, APP_KEY).await? {
        Some(document) => settings_of(&document),
        None => Map::new(),
    };
    app.insert("feedback_repo_filter".into(), Value::Array(ids));
    store
        .put_document(tenant, COLLECTION, APP_KEY, envelope(app))
        .await?;
    load(store, tenant).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_names_match_the_importer() {
        for name in [
            "api_key",
            "GitHub-Token",
            "client_secret",
            "access_token",
            "my_token",
            "PEM",
        ] {
            assert!(credential_like(name), "{name}");
        }
        for name in ["max_token_limit", "token_count", "model", "ready_label"] {
            assert!(!credential_like(name), "{name}");
        }
    }

    #[test]
    fn repo_key_follows_the_importer() {
        assert_eq!(repo_key("acme__demo"), "repo-acme__demo");
        assert!(repo_key("a b").starts_with("repo-") && repo_key("a b").len() == 29);
        assert_eq!(repo_slug("acme/demo repo"), "acme__demo-repo");
    }
}
