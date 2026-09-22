use crate::config::AppConfig;
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelInfo {
    pub value: String,
    pub label: String,
    pub efforts: Vec<String>,
    pub default_effort: String,
    /// True when this model draws on a separate usage-credit balance instead
    /// of the account's normal plan allowance (e.g. Claude's Fable models).
    /// Set by a `parse_*` function when the CLI's own catalog says so
    /// ([`mentions_usage_credits`]), and again in [`provider_models`] for any
    /// model in a family [`requires_usage_credits`] knows about.
    #[serde(default)]
    pub requires_usage_credits: bool,
}

/// Model families known to draw on a separate usage-credit balance rather
/// than the account's normal plan allowance. A catalog that says so itself
/// (see [`mentions_usage_credits`]) is always preferred; this hand-maintained
/// list is the backstop for a catalog that carries no such marker — e.g. the
/// aliases parsed out of `claude --help` when no model-catalog cache exists
/// yet. Named by family, so both the alias (`fable`) and every full model
/// name in that family (`claude-fable-5-1`) match.
const USAGE_CREDIT_MODELS: &[&str] = &["fable"];

fn requires_usage_credits(value: &str) -> bool {
    value
        .split(['-', '_'])
        .any(|part| USAGE_CREDIT_MODELS.contains(&part))
}

/// True when a catalog entry itself says the model bills against usage
/// credits, the way Claude Code's own model picker labels them.
fn mentions_usage_credits(model: &serde_json::Value) -> bool {
    [
        model["badge"]["message"].as_str(),
        model["tooltip"]["content"].as_str(),
    ]
    .into_iter()
    .flatten()
    .any(|text| text.to_lowercase().contains("usage credits"))
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ToolInfo {
    pub id: String,
    pub label: String,
    pub required: bool,
    pub installed: bool,
    pub path: String,
    pub version: String,
    pub authenticated: Option<bool>,
    pub status: String,
    pub installable: bool,
    /// Models and reasoning levels for this provider; never empty for a known
    /// provider so the UI can always offer a dropdown.
    pub models: Vec<ModelInfo>,
    /// True when `models` was read from the installed CLI, false when it is the
    /// built-in fallback used because the CLI is missing or exposes no catalog.
    pub models_detected: bool,
}

pub fn enhanced_path() -> String {
    let mut values = Vec::<String>::new();
    if let Ok(output) = Command::new("/bin/zsh")
        .args(["-lic", "printf '%s' \"$PATH\""])
        .output()
    {
        if output.status.success() {
            values.extend(
                String::from_utf8_lossy(&output.stdout)
                    .split(':')
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                    .map(str::to_string),
            );
        }
    }
    if let Ok(current) = std::env::var("PATH") {
        values.extend(
            current
                .split(':')
                .filter(|value| !value.is_empty())
                .map(str::to_string),
        );
    }
    if let Some(home) = std::env::var_os("HOME").map(PathBuf::from) {
        for suffix in [".local/bin", ".npm-global/bin", ".cargo/bin"] {
            values.push(home.join(suffix).to_string_lossy().into_owned());
        }
    }
    values.extend(
        ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
            .into_iter()
            .map(str::to_string),
    );
    values.dedup();
    values.join(":")
}

pub fn find_executable(name: &str, configured: &str) -> Option<PathBuf> {
    if !configured.trim().is_empty() {
        let candidate = PathBuf::from(configured);
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    enhanced_path()
        .split(':')
        .map(|directory| Path::new(directory).join(name))
        .find(|candidate| is_executable(candidate))
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .map(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(path: &Path) -> bool {
    path.is_file()
}

fn command_output(program: &Path, arguments: &[&str]) -> (bool, String) {
    let output = Command::new(program)
        .args(arguments)
        .env("PATH", enhanced_path())
        .output();
    match output {
        Ok(output) => {
            let text = if output.stdout.is_empty() {
                String::from_utf8_lossy(&output.stderr).trim().to_string()
            } else {
                String::from_utf8_lossy(&output.stdout).trim().to_string()
            };
            (output.status.success(), text)
        }
        Err(error) => (false, error.to_string()),
    }
}

fn version(program: &Path, arguments: &[&str]) -> String {
    command_output(program, arguments)
        .1
        .lines()
        .next()
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn basic_tool(id: &str, label: &str, name: &str, required: bool, configured: &str) -> ToolInfo {
    let executable = find_executable(name, configured);
    let version_arguments: &[&str] = match id {
        "python" => &["--version"],
        _ => &["--version"],
    };
    let version_text = executable
        .as_deref()
        .map(|path| version(path, version_arguments))
        .unwrap_or_default();
    ToolInfo {
        id: id.into(),
        label: label.into(),
        required,
        installed: executable.is_some(),
        path: executable
            .as_deref()
            .map(|path| path.to_string_lossy().into_owned())
            .unwrap_or_default(),
        version: version_text,
        authenticated: None,
        status: if executable.is_some() {
            "Ready"
        } else {
            "Not installed"
        }
        .into(),
        installable: false,
        models: Vec::new(),
        models_detected: false,
    }
}

fn display_model_name(value: &str) -> String {
    value
        .split(['-', '_'])
        .filter(|part| !part.is_empty())
        .map(|part| match part.to_ascii_lowercase().as_str() {
            "gpt" => "GPT".into(),
            "claude" => "Claude".into(),
            "grok" => "Grok".into(),
            _ if part
                .chars()
                .all(|character| character.is_ascii_digit() || character == '.') =>
            {
                part.into()
            }
            _ => {
                let mut characters = part.chars();
                characters
                    .next()
                    .map(|first| first.to_uppercase().collect::<String>() + characters.as_str())
                    .unwrap_or_default()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// The help text for one CLI option: its own line plus the deeper-indented
/// lines that wrap its description, joined into a single line.
fn option_help(help: &str, option: &str) -> Option<String> {
    let indent = |line: &str| line.len() - line.trim_start().len();
    let mut lines = help.lines().skip_while(|line| !line.contains(option));
    let first = lines.next()?;
    let base = indent(first);
    let mut block = vec![first.trim()];
    block.extend(
        lines
            .take_while(|line| !line.trim().is_empty() && indent(line) > base)
            .map(str::trim),
    );
    Some(block.join(" "))
}

/// The first parenthesised list of plain words, e.g. `(low, medium, high)`.
fn parenthesised_values(text: &str) -> Vec<String> {
    let group = regex::Regex::new(r"\(([^()]*)\)").expect("valid group regex");
    let values = group
        .captures_iter(text)
        .map(|capture| {
            capture[1]
                .trim_start_matches("choices:")
                .split(',')
                .map(|value| value.trim().trim_matches(['\'', '"', '`']))
                .filter(|value| {
                    !value.is_empty()
                        && value
                            .chars()
                            .all(|character| character.is_ascii_alphanumeric() || character == '-')
                })
                .map(str::to_string)
                .collect::<Vec<_>>()
        })
        .find(|values| values.len() >= 2)
        .unwrap_or_default();
    values
}

/// A model name without the dated snapshot suffix some catalog ids carry
/// (`claude-haiku-4-5-20251001` → `claude-haiku-4-5`). The CLI accepts
/// either, and the shorter name is the one this app's own defaults and the
/// router's model descriptions use, so a saved selection keeps matching the
/// catalog when the service publishes a new snapshot of the same model.
fn without_snapshot_date(id: &str) -> String {
    let snapshot = regex::Regex::new(r"-\d{8}$").expect("valid snapshot regex");
    snapshot.replace(id, "").into_owned()
}

/// Models exactly as Claude Code's own picker lists them, read from the
/// catalog the CLI caches after asking the service which models the signed-in
/// account may use. `claude --help` only ever names two or three aliases as
/// examples of the `--model` syntax, so this cache is the only complete and
/// account-accurate source of the list.
fn parse_claude_catalog(cache: &str) -> Vec<ModelInfo> {
    let Ok(cache) = serde_json::from_str::<serde_json::Value>(cache) else {
        return Vec::new();
    };
    cache["catalog"]["config"]["models"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|model| !model["hidden"].as_bool().unwrap_or(false))
        .filter_map(|model| {
            let value = without_snapshot_date(model["id"].as_str()?);
            // A model with no reasoning control (Haiku today) reports no
            // effort options; `provider_models` fills those in.
            let levels = model["thinking"]["effort_options"].as_array();
            Some(ModelInfo {
                label: model["name"]
                    .as_str()
                    .map(|name| {
                        if name.starts_with("Claude") {
                            name.to_string()
                        } else {
                            format!("Claude {name}")
                        }
                    })
                    .unwrap_or_else(|| display_model_name(&value)),
                value,
                efforts: levels
                    .into_iter()
                    .flatten()
                    .filter_map(|level| level["id"].as_str().map(str::to_string))
                    .collect(),
                default_effort: levels
                    .into_iter()
                    .flatten()
                    .find(|level| level["badge"]["message"].as_str() == Some("Default"))
                    .and_then(|level| level["id"].as_str())
                    .unwrap_or_default()
                    .to_string(),
                requires_usage_credits: mentions_usage_credits(model),
            })
        })
        .collect()
}

/// The models in the newest model-catalog cache Claude Code has written.
/// The CLI keeps one file per account/surface, so the most recently fetched
/// one is the catalog a run started now would use.
fn claude_cached_catalog() -> Vec<ModelInfo> {
    let directory = std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude")))
        .map(|base| base.join("cache/model-catalog"));
    let Some(entries) = directory.and_then(|directory| std::fs::read_dir(directory).ok()) else {
        return Vec::new();
    };
    let mut newest: Option<(i64, Vec<ModelInfo>)> = None;
    for path in entries.flatten().map(|entry| entry.path()) {
        if path.extension().and_then(|extension| extension.to_str()) != Some("json") {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let models = parse_claude_catalog(&text);
        if models.is_empty() {
            continue;
        }
        let fetched = serde_json::from_str::<serde_json::Value>(&text)
            .ok()
            .and_then(|cache| cache["fetchedAt"].as_i64())
            .unwrap_or_default();
        if newest.as_ref().is_none_or(|(seen, _)| fetched >= *seen) {
            newest = Some((fetched, models));
        }
    }
    newest.map(|(_, models)| models).unwrap_or_default()
}

fn parse_claude_models(help: &str) -> Vec<ModelInfo> {
    let efforts = option_help(help, "--effort <level>")
        .map(|text| parenthesised_values(&text))
        .unwrap_or_default();
    let Some(model_help) = option_help(help, "--model <model>") else {
        return Vec::new();
    };
    let quoted = regex::Regex::new(r#"['"`]([a-z][a-z0-9._-]+)['"`]"#).expect("valid model regex");
    let mut values = Vec::new();
    for capture in quoted.captures_iter(&model_help) {
        let value = capture[1].to_string();
        if !values.contains(&value) {
            values.push(value);
        }
    }
    // Claude's aliases (sonnet, opus, etc.) track the latest model and are
    // therefore preferable to the single full-name example in --help.
    let aliases: Vec<_> = values
        .iter()
        .filter(|value| !value.starts_with("claude-"))
        .cloned()
        .collect();
    let selected = if aliases.is_empty() { values } else { aliases };
    selected
        .into_iter()
        .map(|value| ModelInfo {
            label: format!("Claude {} (latest)", display_model_name(&value)),
            requires_usage_credits: requires_usage_credits(&value),
            value,
            efforts: efforts.clone(),
            default_effort: String::new(),
        })
        .collect()
}

fn parse_codex_models(output: &str) -> Vec<ModelInfo> {
    let Ok(catalog) = serde_json::from_str::<serde_json::Value>(output) else {
        return Vec::new();
    };
    catalog["models"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|model| model["visibility"].as_str().unwrap_or("list") == "list")
        .filter_map(|model| {
            let value = model["slug"].as_str()?.to_string();
            let efforts = model["supported_reasoning_levels"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|level| level["effort"].as_str().map(str::to_string))
                .collect();
            Some(ModelInfo {
                label: model["display_name"]
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| display_model_name(&value)),
                value,
                efforts,
                default_effort: model["default_reasoning_level"]
                    .as_str()
                    .unwrap_or_default()
                    .to_string(),
                requires_usage_credits: false,
            })
        })
        .collect()
}

/// The models `grok models` lists. Only the lines of its `Available models:`
/// block are read, and both bullet characters it uses are accepted: the
/// default model's line is bulleted `*` and every other model's `-`, so
/// matching `*` alone would report the default as the entire catalog.
fn parse_grok_models(output: &str) -> Vec<ModelInfo> {
    let ansi = regex::Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").expect("valid ANSI regex");
    let clean = ansi.replace_all(output, "").into_owned();
    let listing = clean
        .split_once("Available models:")
        .map_or(clean.as_str(), |(_, listing)| listing);
    let model = regex::Regex::new(r"(?m)^\s*[*-]\s+([^\s(]+)").expect("valid model regex");
    model
        .captures_iter(listing)
        .map(|capture| capture[1].to_string())
        .map(|value| ModelInfo {
            label: display_model_name(&value),
            value,
            // Grok currently accepts an effort flag but its model catalog and
            // help do not enumerate supported values.
            efforts: Vec::new(),
            default_effort: String::new(),
            requires_usage_credits: false,
        })
        .collect()
}

/// Grok's models as the CLI itself knows them, from the catalog it caches
/// after talking to the Grok service. `grok models` prints only the model
/// ids, so this is where their display names, effort levels and default
/// effort come from.
fn parse_grok_catalog(cache: &str) -> Vec<ModelInfo> {
    let Ok(cache) = serde_json::from_str::<serde_json::Value>(cache) else {
        return Vec::new();
    };
    let mut models: Vec<ModelInfo> = cache["models"]
        .as_object()
        .into_iter()
        .flatten()
        .filter(|(_, model)| !model["info"]["hidden"].as_bool().unwrap_or(false))
        .map(|(id, model)| {
            let levels = model["info"]["reasoning_efforts"].as_array();
            ModelInfo {
                label: model["info"]["name"]
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| display_model_name(id)),
                value: id.clone(),
                efforts: levels
                    .into_iter()
                    .flatten()
                    .filter_map(|level| level["value"].as_str().map(str::to_string))
                    .collect(),
                default_effort: levels
                    .into_iter()
                    .flatten()
                    .find(|level| level["default"].as_bool().unwrap_or(false))
                    .and_then(|level| level["value"].as_str())
                    .unwrap_or_default()
                    .to_string(),
                requires_usage_credits: mentions_usage_credits(&model["info"]),
            }
        })
        .collect();
    // The cache is a map, so it arrives in id order. Newest first matches how
    // `grok models` prints the list, and keeps the newest model — rather than
    // the oldest — as the first entry `reconcile_config_models` repairs an
    // unavailable saved selection into.
    models.sort_by(|left, right| right.value.cmp(&left.value));
    models
}

fn grok_cached_catalog() -> Vec<ModelInfo> {
    std::env::var_os("HOME")
        .map(|home| PathBuf::from(home).join(".grok/models_cache.json"))
        .and_then(|path| std::fs::read_to_string(path).ok())
        .map(|cache| parse_grok_catalog(&cache))
        .unwrap_or_default()
}

fn discover_models(id: &str, program: &Path) -> Vec<ModelInfo> {
    match id {
        "claude" => {
            let cached = claude_cached_catalog();
            if !cached.is_empty() {
                return cached;
            }
            // No catalog cached yet (a fresh install that has not run, or a
            // signed-out CLI): `--help` still names the aliases that track
            // the latest models.
            parse_claude_models(&command_output(program, &["--help"]).1)
        }
        // The bundled catalog is updated with the CLI and avoids a network
        // refresh during the UI's periodic tool detection.
        "codex" => {
            parse_codex_models(&command_output(program, &["debug", "models", "--bundled"]).1)
        }
        "grok" => {
            let cached = grok_cached_catalog();
            let mut models = parse_grok_models(&command_output(program, &["models"]).1);
            if models.is_empty() {
                return cached;
            }
            // The live listing decides which models exist for this account;
            // the cache only labels them and lists their effort levels.
            for model in &mut models {
                if let Some(cached) = cached.iter().find(|cached| cached.value == model.value) {
                    model.label = cached.label.clone();
                    model.efforts = cached.efforts.clone();
                    model.default_effort = cached.default_effort.clone();
                    model.requires_usage_credits = cached.requires_usage_credits;
                }
            }
            models
        }
        _ => Vec::new(),
    }
}

/// Effort levels every provider CLI accepts, used only for a model whose CLI
/// does not enumerate its own.
fn fallback_efforts(id: &str) -> Vec<String> {
    let levels: &[&str] = match id {
        "claude" => &["low", "medium", "high", "xhigh", "max"],
        _ => &["low", "medium", "high", "xhigh"],
    };
    levels.iter().map(|level| level.to_string()).collect()
}

/// Last-resort catalog for a CLI that is not installed or reports nothing, so
/// the UI still offers a dropdown rather than a free-form field.
fn fallback_models(id: &str) -> Vec<ModelInfo> {
    let (values, default_effort): (&[&str], &str) = match id {
        "claude" => (
            &["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
            "",
        ),
        "codex" => (&["gpt-5.6-luna"], "medium"),
        "grok" => (&["grok-4.7", "grok-4.6"], "high"),
        _ => (&[], ""),
    };
    values
        .iter()
        .map(|value| ModelInfo {
            value: value.to_string(),
            label: display_model_name(value),
            efforts: Vec::new(),
            default_effort: default_effort.into(),
            requires_usage_credits: false,
        })
        .collect()
}

/// Models for a provider: what the installed CLI reports, else the fallback.
/// Returns whether the list came from the CLI.
///
/// When `allow_credit_models` is false, any model that draws on a separate
/// usage-credit balance ([`requires_usage_credits`]) is dropped from the
/// list entirely — it can then never be offered in a dropdown, nor be the
/// catalog's "first" model that [`reconcile_config_models`] repairs an
/// unavailable saved selection into. A saved selection that already points
/// at such a model is therefore itself repaired away on the next detect.
fn provider_models(
    id: &str,
    program: Option<&Path>,
    allow_credit_models: bool,
) -> (Vec<ModelInfo>, bool) {
    let discovered = program
        .map(|program| discover_models(id, program))
        .unwrap_or_default();
    let detected = !discovered.is_empty();
    let mut models = if detected {
        discovered
    } else {
        fallback_models(id)
    };
    for model in &mut models {
        if model.efforts.is_empty() {
            model.efforts = fallback_efforts(id);
        }
        model.requires_usage_credits |= requires_usage_credits(&model.value);
    }
    if !allow_credit_models {
        let without_credit_models: Vec<_> = models
            .iter()
            .filter(|model| !model.requires_usage_credits)
            .cloned()
            .collect();
        // A known provider always needs at least one offerable model; never
        // filter down to nothing even if every reported model needs credits.
        if !without_credit_models.is_empty() {
            models = without_credit_models;
        }
    }
    (models, detected)
}

/// Brand prefixes that carry no meaning on their own when matching a saved
/// model name against a catalog entry.
const MODEL_BRANDS: &[&str] = &["claude", "gpt", "grok"];

/// The family part of a model name: its alphabetic segments other than the
/// brand — `opus` for both the `opus` alias and `claude-opus-5`, `luna` for
/// `gpt-5.6-luna`. Empty when a name is only a brand and a version number
/// (`grok-4.7`), which is the signal that it has no family to match on.
fn model_family(value: &str) -> Vec<String> {
    value
        .to_ascii_lowercase()
        .split(['-', '_'])
        .filter(|part| {
            !part.is_empty()
                && part
                    .chars()
                    .all(|character| character.is_ascii_alphabetic())
                && !MODEL_BRANDS.contains(part)
        })
        .map(str::to_string)
        .collect()
}

/// The catalog entry a saved selection refers to: the exact model while it is
/// still offered, else the catalog's first model of the same family, else the
/// catalog's first model. The family step matters because a CLI can rename
/// the same model — the alias `opus` became `claude-opus-5` when detection
/// started reading Claude's own model catalog — and a saved `opus` should
/// then become the current Opus rather than whichever model leads the list.
fn matching_model<'a>(models: &'a [ModelInfo], saved: &str) -> &'a ModelInfo {
    if let Some(exact) = models.iter().find(|model| model.value == saved) {
        return exact;
    }
    let family = model_family(saved);
    if !family.is_empty() {
        if let Some(relative) = models
            .iter()
            .find(|model| model_family(&model.value) == family)
        {
            return relative;
        }
    }
    &models[0]
}

fn supported_effort(model: &ModelInfo, current: &str) -> String {
    if model.efforts.iter().any(|effort| effort == current) {
        return current.to_string();
    }
    if !model.default_effort.is_empty()
        && model
            .efforts
            .iter()
            .any(|effort| effort == &model.default_effort)
    {
        return model.default_effort.clone();
    }
    model
        .efforts
        .first()
        .cloned()
        .unwrap_or_else(|| current.to_string())
}

/// Reconciles saved provider, router, and tier selections with model catalogs
/// reported by the installed CLIs. A catalog is authoritative only when the
/// CLI actually returned it; fallback catalogs must never overwrite a user's
/// settings merely because a CLI is missing or temporarily unavailable.
///
/// Newly reported models naturally appear in the UI through [`detect`]. When
/// a saved model has disappeared, [`matching_model`] picks its replacement —
/// the current model of the same family where there is one, else the CLI's
/// first (normally default) model — and its reported effort levels are
/// applied. Returns human-readable descriptions of every repair so callers
/// can decide whether to persist and reload a running scheduler.
pub fn reconcile_config_models(config: &mut AppConfig, tools: &[ToolInfo]) -> Vec<String> {
    let mut repairs = Vec::new();
    for tool in tools
        .iter()
        .filter(|tool| tool.models_detected && !tool.models.is_empty())
    {
        let Some(provider) = config
            .providers
            .iter_mut()
            .find(|provider| provider.id == tool.id)
        else {
            continue;
        };
        let worker = matching_model(&tool.models, &provider.model);
        if provider.model != worker.value {
            repairs.push(format!(
                "{} worker model '{}' is unavailable; using '{}'.",
                tool.label, provider.model, worker.value
            ));
            provider.model = worker.value.clone();
        }
        let effort = supported_effort(worker, &provider.effort);
        if provider.effort != effort {
            repairs.push(format!(
                "{} worker effort '{}' is unavailable for '{}'; using '{}'.",
                tool.label, provider.effort, provider.model, effort
            ));
            provider.effort = effort;
        }

        let router = matching_model(&tool.models, &provider.router_model);
        if provider.router_model != router.value {
            repairs.push(format!(
                "{} router model '{}' is unavailable; using '{}'.",
                tool.label, provider.router_model, router.value
            ));
            provider.router_model = router.value.clone();
        }
        let effort = supported_effort(router, &provider.router_effort);
        if provider.router_effort != effort {
            repairs.push(format!(
                "{} router effort '{}' is unavailable for '{}'; using '{}'.",
                tool.label, provider.router_effort, provider.router_model, effort
            ));
            provider.router_effort = effort;
        }

        if let Some(tiers) = config.routing_tiers.get_mut(&tool.id) {
            for tier in tiers {
                let model = matching_model(&tool.models, &tier.model);
                if tier.model != model.value {
                    repairs.push(format!(
                        "{} routing tier {}-{} model '{}' is unavailable; using '{}'.",
                        tool.label,
                        tier.min_complexity,
                        tier.max_complexity,
                        tier.model,
                        model.value
                    ));
                    tier.model = model.value.clone();
                }
                let effort = supported_effort(model, &tier.effort);
                if tier.effort != effort {
                    repairs.push(format!(
                        "{} routing tier {}-{} effort '{}' is unavailable for '{}'; using '{}'.",
                        tool.label,
                        tier.min_complexity,
                        tier.max_complexity,
                        tier.effort,
                        tier.model,
                        effort
                    ));
                    tier.effort = effort;
                }
            }
        }
    }
    repairs
}

pub fn detect(config: &AppConfig, github_host: &str) -> Vec<ToolInfo> {
    let provider_bin = |id: &str| {
        config
            .provider(id)
            .map(|p| p.bin.clone())
            .unwrap_or_default()
    };
    let mut tools = vec![
        basic_tool("git", "Git", "git", true, ""),
        basic_tool("gh", "GitHub CLI", "gh", true, &config.gh_bin),
        basic_tool("python", "Python 3", "python3", true, &config.python_bin),
        basic_tool("node", "Node.js", "node", false, ""),
        basic_tool("npm", "npm", "npm", false, ""),
    ];
    for id in crate::config::KNOWN_PROVIDERS {
        let (label, name) = match id {
            "claude" => ("Claude Code", "claude"),
            "codex" => ("Codex CLI", "codex"),
            "grok" => ("Grok Build", "grok"),
            _ => continue,
        };
        tools.push(basic_tool(id, label, name, true, &provider_bin(id)));
    }
    let npm_available = tools.iter().any(|tool| tool.id == "npm" && tool.installed);
    for tool in &mut tools {
        if crate::config::KNOWN_PROVIDERS.contains(&tool.id.as_str()) {
            let program = tool.installed.then(|| PathBuf::from(&tool.path));
            (tool.models, tool.models_detected) = provider_models(
                &tool.id,
                program.as_deref(),
                config.allow_usage_credit_models,
            );
        }
        match tool.id.as_str() {
            "gh" if tool.installed => {
                let (ready, _) = command_output(
                    Path::new(&tool.path),
                    &["auth", "status", "--hostname", github_host],
                );
                tool.authenticated = Some(ready);
                tool.status = if ready {
                    "Signed in"
                } else {
                    "Sign-in required"
                }
                .into();
            }
            "claude" if tool.installed => {
                let (ready, output) =
                    command_output(Path::new(&tool.path), &["auth", "status", "--json"]);
                let logged_in = ready
                    && serde_json::from_str::<serde_json::Value>(&output)
                        .ok()
                        .and_then(|value| value.get("loggedIn").and_then(|value| value.as_bool()))
                        .unwrap_or(false);
                tool.authenticated = Some(logged_in);
                tool.status = if logged_in {
                    "Signed in"
                } else {
                    "Sign-in required"
                }
                .into();
                tool.installable = npm_available;
            }
            "codex" if tool.installed => {
                let (ready, output) = command_output(Path::new(&tool.path), &["login", "status"]);
                let logged_in = ready && output.to_lowercase().contains("logged in");
                tool.authenticated = Some(logged_in);
                tool.status = if logged_in {
                    "Signed in"
                } else {
                    "Sign-in required"
                }
                .into();
                tool.installable = npm_available;
            }
            "grok" if tool.installed => {
                let auth_file = std::env::var_os("HOME")
                    .map(PathBuf::from)
                    .map(|home| home.join(".grok/auth.json").is_file())
                    .unwrap_or(false);
                let logged_in = auth_file || std::env::var_os("XAI_API_KEY").is_some();
                tool.authenticated = Some(logged_in);
                tool.status = if logged_in {
                    "Signed in"
                } else {
                    "Sign-in required"
                }
                .into();
            }
            "claude" | "codex" => {
                tool.installable = npm_available;
                tool.status = if npm_available {
                    "Ready to install"
                } else {
                    "Install Node.js/npm first"
                }
                .into();
            }
            "grok" => {
                // Grok Build installs via the official x.ai script in a
                // Terminal window, not npm — always offerable.
                tool.installable = true;
                tool.status = "Ready to install".into();
            }
            _ => {}
        }
    }
    tools
}

pub fn configured_or_detected(configured: &str, name: &str) -> Result<PathBuf, String> {
    find_executable(name, configured)
        .ok_or_else(|| format!("{name} was not found. Install it or set its path in Settings."))
}

pub fn install_spec(provider: &str) -> Result<(PathBuf, Vec<String>), String> {
    let npm = find_executable("npm", "")
        .ok_or_else(|| "npm was not found. Install Node.js first, then retry.".to_string())?;
    let package = match provider {
        "claude" => "@anthropic-ai/claude-code",
        "codex" => "@openai/codex",
        // Grok Build is installed via its own Terminal installer, handled
        // before this function is reached.
        _ => return Err("Only Claude Code and Codex CLI install via npm.".into()),
    };
    Ok((npm, vec!["install".into(), "-g".into(), package.into()]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_codex_catalog_model_specific_efforts() {
        let models = parse_codex_models(
            r#"{"models":[{"slug":"gpt-next","display_name":"GPT Next","default_reasoning_level":"medium","supported_reasoning_levels":[{"effort":"low"},{"effort":"medium"}],"visibility":"list"},{"slug":"hidden","visibility":"hide"}]}"#,
        );
        assert_eq!(models.len(), 1);
        assert_eq!(models[0].value, "gpt-next");
        assert_eq!(models[0].efforts, ["low", "medium"]);
        assert_eq!(models[0].default_effort, "medium");
    }

    #[test]
    fn parses_claude_help_aliases_and_efforts() {
        let models = parse_claude_models(
            "--effort <level> Effort (low, medium, high, xhigh, max)\n\
             --model <model> Provide an alias (e.g. 'opus', 'sonnet') or full name (e.g. 'claude-opus-6').\n\
             --next-option <value>",
        );
        assert_eq!(
            models
                .iter()
                .map(|model| model.value.as_str())
                .collect::<Vec<_>>(),
            ["opus", "sonnet"]
        );
        assert_eq!(models[0].efforts, ["low", "medium", "high", "xhigh", "max"]);
    }

    #[test]
    fn parses_grok_model_list_without_authentication() {
        let models = parse_grok_models(
            "You are not authenticated.\n\nAvailable models:\n  * grok-4.6 (default)\n  * grok-next\n",
        );
        assert_eq!(
            models
                .iter()
                .map(|model| model.value.as_str())
                .collect::<Vec<_>>(),
            ["grok-4.6", "grok-next"]
        );
    }

    #[test]
    fn parses_every_grok_model_not_only_the_default_bullet() {
        // `grok models` bullets the default model `*` and the rest `-`; the
        // UI used to show the default alone. Prose above the listing must
        // not be mistaken for a model, however it is bulleted.
        let models = parse_grok_models(
            "You are logged in with grok.com.\n\n\
             - not a model\n\n\
             Default model: grok-4.7\n\n\
             Available models:\n  \
             * grok-4.7 (default)\n  \
             - grok-4.7-build-fast\n  \
             - grok-4.6\n  \
             - grok-4.5\n",
        );
        assert_eq!(
            models
                .iter()
                .map(|model| model.value.as_str())
                .collect::<Vec<_>>(),
            ["grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5"]
        );
    }

    #[test]
    fn parses_wrapped_claude_help_as_printed_by_the_cli() {
        let help = "\
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --environment <environment_id>        Create a new cloud session
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name (e.g.
                                        'claude-fable-5').
  -n, --name <name>                     Set a display name ('agent' setting)
";
        let models = parse_claude_models(help);
        assert_eq!(
            models
                .iter()
                .map(|model| model.value.as_str())
                .collect::<Vec<_>>(),
            ["fable", "opus", "sonnet"]
        );
        assert_eq!(models[0].efforts, ["low", "medium", "high", "xhigh", "max"]);
    }

    #[test]
    fn reads_grok_names_and_efforts_from_the_cli_model_cache() {
        let models = parse_grok_catalog(
            r#"{"models":{
                "grok-4.6":{"info":{"name":"Grok 4.6","reasoning_efforts":[
                    {"value":"high","default":true},{"value":"low","default":false}]}},
                "grok-4.7":{"info":{"name":"Grok 4.7","reasoning_efforts":[
                    {"value":"xhigh","default":false},{"value":"high","default":true}]}},
                "grok-secret":{"info":{"name":"Grok Secret","hidden":true}},
                "plain":{"info":{}}}}"#,
        );
        // Descending id order (newest model of a family first), hidden
        // models dropped, names taken from the cache.
        assert_eq!(
            models
                .iter()
                .map(|model| (model.value.as_str(), model.label.as_str()))
                .collect::<Vec<_>>(),
            [
                ("plain", "Plain"),
                ("grok-4.7", "Grok 4.7"),
                ("grok-4.6", "Grok 4.6"),
            ]
        );
        assert_eq!(models[1].efforts, ["xhigh", "high"]);
        assert_eq!(models[1].default_effort, "high");
        assert!(models[0].efforts.is_empty());
    }

    #[test]
    fn parses_the_claude_model_catalog_the_cli_caches() {
        let models = parse_claude_catalog(
            r#"{"fetchedAt":1,"catalog":{"config":{"models":[
                {"id":"claude-sonnet-5","name":"Sonnet 5","thinking":{"type":"effort",
                 "effort_options":[{"id":"low"},{"id":"high","badge":{"message":"Default"}}]}},
                {"id":"claude-fable-5-1","name":"Fable 5.1",
                 "badge":{"message":"Requires usage credits"},
                 "thinking":{"type":"effort","effort_options":[{"id":"high"}]}},
                {"id":"claude-haiku-4-5-20251001","name":"Haiku 4.5",
                 "thinking":{"type":"none"}},
                {"id":"claude-opus-4-7","name":"Opus 4.7","hidden":true}]}}}"#,
        );
        assert_eq!(
            models
                .iter()
                .map(|model| (model.value.as_str(), model.label.as_str()))
                .collect::<Vec<_>>(),
            [
                ("claude-sonnet-5", "Claude Sonnet 5"),
                ("claude-fable-5-1", "Claude Fable 5.1"),
                // The dated snapshot suffix is dropped: the CLI accepts
                // either name and the shorter one survives a new snapshot.
                ("claude-haiku-4-5", "Claude Haiku 4.5"),
            ]
        );
        assert_eq!(models[0].efforts, ["low", "high"]);
        assert_eq!(models[0].default_effort, "high");
        assert!(!models[0].requires_usage_credits);
        // The catalog says so itself; no hand-maintained list needed.
        assert!(models[1].requires_usage_credits);
        // A model with no reasoning control reports no efforts of its own.
        assert!(models[2].efforts.is_empty());
    }

    #[test]
    fn credit_models_are_recognised_by_family_not_only_by_alias() {
        assert!(requires_usage_credits("fable"));
        assert!(requires_usage_credits("claude-fable-5-1"));
        assert!(!requires_usage_credits("claude-opus-5"));
    }

    #[test]
    fn providers_without_a_catalog_still_get_dropdown_options() {
        for id in crate::config::KNOWN_PROVIDERS {
            let (models, detected) = provider_models(id, None, false);
            assert!(!detected, "{id} has no CLI to read");
            assert!(!models.is_empty(), "{id} needs fallback models");
            assert!(models.iter().all(|model| !model.efforts.is_empty()));
        }
    }

    #[test]
    fn credit_models_are_dropped_unless_allowed() {
        let help = "\
  --effort <level>  Effort level (low, medium, high, xhigh, max)
  --model <model>   Provide an alias (e.g. 'fable', 'opus', or 'sonnet').
";
        let models = parse_claude_models(help);
        assert!(models
            .iter()
            .any(|model| model.value == "fable" && model.requires_usage_credits));

        let discovered = models.clone();
        let filtered: Vec<_> = discovered
            .into_iter()
            .filter(|model| !model.requires_usage_credits)
            .collect();
        assert!(!filtered.iter().any(|model| model.value == "fable"));
        assert!(filtered.iter().any(|model| model.value == "opus"));
    }

    #[test]
    fn reconcile_heals_a_saved_credit_model_once_credits_are_disallowed() {
        // Simulates the live incident: a saved selection of "fable" (from
        // before the toggle existed, or from a run with it turned on) must
        // be repaired away once `allow_usage_credit_models` is off and the
        // catalog handed to reconcile no longer offers it.
        let mut config = AppConfig::default();
        for provider in &mut config.providers {
            if provider.id == "claude" {
                provider.model = "fable".into();
                provider.router_model = "fable".into();
            }
        }
        for tier in config.routing_tiers.get_mut("claude").unwrap() {
            tier.model = "fable".into();
        }
        let mut tool = basic_tool("claude", "Claude Code", "claude", true, "");
        tool.models = vec![
            ModelInfo {
                value: "opus".into(),
                label: "Claude Opus (latest)".into(),
                efforts: vec!["low".into(), "high".into()],
                default_effort: String::new(),
                requires_usage_credits: false,
            },
            ModelInfo {
                value: "sonnet".into(),
                label: "Claude Sonnet (latest)".into(),
                efforts: vec!["low".into(), "high".into()],
                default_effort: String::new(),
                requires_usage_credits: false,
            },
        ];
        tool.models_detected = true;

        let repairs = reconcile_config_models(&mut config, &[tool]);

        let claude = config.provider("claude").unwrap();
        assert_eq!(claude.model, "opus");
        assert_eq!(claude.router_model, "opus");
        assert!(config.routing_tiers["claude"]
            .iter()
            .all(|tier| tier.model == "opus"));
        assert!(repairs
            .iter()
            .any(|repair| repair.contains("worker model 'fable' is unavailable")));
    }

    #[test]
    fn live_catalog_repairs_retired_models_and_efforts_everywhere() {
        let mut config = AppConfig::default();
        let grok = config
            .providers
            .iter_mut()
            .find(|provider| provider.id == "grok")
            .unwrap();
        grok.model = "grok-retired".into();
        grok.effort = "max".into();
        grok.router_model = "grok-retired".into();
        grok.router_effort = "max".into();
        config.routing_tiers.get_mut("grok").unwrap()[0].model = "grok-retired".into();
        config.routing_tiers.get_mut("grok").unwrap()[0].effort = "max".into();
        let tools = vec![ToolInfo {
            id: "grok".into(),
            label: "Grok Build".into(),
            required: true,
            installed: true,
            path: "/usr/bin/grok".into(),
            version: "test".into(),
            authenticated: Some(true),
            status: "Signed in".into(),
            installable: false,
            models: vec![ModelInfo {
                value: "grok-current".into(),
                label: "Grok Current".into(),
                efforts: vec!["low".into(), "medium".into()],
                default_effort: "medium".into(),
                requires_usage_credits: false,
            }],
            models_detected: true,
        }];

        let repairs = reconcile_config_models(&mut config, &tools);

        let grok = config.provider("grok").unwrap();
        assert_eq!(
            (grok.model.as_str(), grok.effort.as_str()),
            ("grok-current", "medium")
        );
        assert_eq!(
            (grok.router_model.as_str(), grok.router_effort.as_str()),
            ("grok-current", "medium")
        );
        assert_eq!(config.routing_tiers["grok"][0].model, "grok-current");
        assert_eq!(config.routing_tiers["grok"][0].effort, "medium");
        assert!(repairs.iter().any(|repair| repair.contains("router model")));
        assert!(repairs
            .iter()
            .any(|repair| repair.contains("routing tier 1-3")));
    }

    #[test]
    fn a_renamed_model_is_repaired_into_its_own_family() {
        // Claude detection used to report the aliases `opus`/`sonnet`; it now
        // reports the full names from the CLI's own catalog. A configuration
        // saved under the old names must follow each selection to the same
        // model, not collapse onto whichever model leads the catalog.
        let mut config = AppConfig::default();
        for provider in &mut config.providers {
            if provider.id == "claude" {
                provider.model = "opus".into();
                provider.router_model = "haiku".into();
            }
        }
        config.routing_tiers.get_mut("claude").unwrap()[0].model = "claude-sonnet-4-1".into();
        let mut tool = basic_tool("claude", "Claude Code", "claude", true, "");
        tool.models = ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"]
            .into_iter()
            .map(|value| ModelInfo {
                value: value.into(),
                label: value.into(),
                efforts: vec!["low".into(), "high".into()],
                default_effort: "high".into(),
                requires_usage_credits: false,
            })
            .collect();
        tool.models_detected = true;

        reconcile_config_models(&mut config, &[tool]);

        let claude = config.provider("claude").unwrap();
        assert_eq!(claude.model, "claude-opus-5");
        assert_eq!(claude.router_model, "claude-haiku-4-5");
        assert_eq!(config.routing_tiers["claude"][0].model, "claude-sonnet-5");
    }

    #[test]
    fn a_model_named_only_by_version_has_no_family_to_match() {
        // Grok's names are brand plus version, so nothing may be treated as
        // a family: a retired grok model falls back to the first offered one
        // rather than being matched to an unrelated version.
        assert!(model_family("grok-4.7").is_empty());
        assert_eq!(model_family("claude-opus-5"), ["opus"]);
        assert_eq!(model_family("gpt-5.6-luna"), ["luna"]);
    }

    #[test]
    fn fallback_catalog_never_overwrites_saved_models() {
        let mut config = AppConfig::default();
        config
            .providers
            .iter_mut()
            .find(|provider| provider.id == "grok")
            .unwrap()
            .router_model = "grok-private-preview".into();
        let mut tool = basic_tool("grok", "Grok Build", "grok", true, "");
        tool.models = vec![ModelInfo {
            value: "grok-fallback".into(),
            label: "Grok Fallback".into(),
            efforts: vec!["low".into()],
            default_effort: "low".into(),
            requires_usage_credits: false,
        }];
        tool.models_detected = false;

        assert!(reconcile_config_models(&mut config, &[tool]).is_empty());
        assert_eq!(
            config.provider("grok").unwrap().router_model,
            "grok-private-preview"
        );
    }
}
