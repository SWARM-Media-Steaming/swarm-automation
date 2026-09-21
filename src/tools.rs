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
            })
        })
        .collect()
}

fn parse_grok_models(output: &str) -> Vec<ModelInfo> {
    let ansi = regex::Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").expect("valid ANSI regex");
    let clean = ansi.replace_all(output, "");
    let model = regex::Regex::new(r"(?m)^\s*\*\s+([^\s(]+)").expect("valid model regex");
    model
        .captures_iter(&clean)
        .map(|capture| capture[1].to_string())
        .map(|value| ModelInfo {
            label: display_model_name(&value),
            value,
            // Grok currently accepts an effort flag but its model catalog and
            // help do not enumerate supported values.
            efforts: Vec::new(),
            default_effort: String::new(),
        })
        .collect()
}

/// Effort levels per Grok model, from the catalog the CLI caches after
/// talking to the Grok service (`grok models` itself lists no efforts).
fn parse_grok_efforts(cache: &str) -> std::collections::HashMap<String, (Vec<String>, String)> {
    let Ok(cache) = serde_json::from_str::<serde_json::Value>(cache) else {
        return Default::default();
    };
    cache["models"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(id, model)| {
            let levels = model["info"]["reasoning_efforts"].as_array()?;
            let efforts: Vec<String> = levels
                .iter()
                .filter_map(|level| level["value"].as_str().map(str::to_string))
                .collect();
            let default = levels
                .iter()
                .find(|level| level["default"].as_bool().unwrap_or(false))
                .and_then(|level| level["value"].as_str())
                .unwrap_or_default()
                .to_string();
            (!efforts.is_empty()).then(|| (id.clone(), (efforts, default)))
        })
        .collect()
}

fn grok_cached_efforts() -> std::collections::HashMap<String, (Vec<String>, String)> {
    std::env::var_os("HOME")
        .map(|home| PathBuf::from(home).join(".grok/models_cache.json"))
        .and_then(|path| std::fs::read_to_string(path).ok())
        .map(|cache| parse_grok_efforts(&cache))
        .unwrap_or_default()
}

fn discover_models(id: &str, program: &Path) -> Vec<ModelInfo> {
    let (_, output) = match id {
        "claude" => command_output(program, &["--help"]),
        // The bundled catalog is updated with the CLI and avoids a network
        // refresh during the UI's periodic tool detection.
        "codex" => command_output(program, &["debug", "models", "--bundled"]),
        "grok" => command_output(program, &["models"]),
        _ => return Vec::new(),
    };
    match id {
        "claude" => parse_claude_models(&output),
        "codex" => parse_codex_models(&output),
        "grok" => {
            let cached = grok_cached_efforts();
            let mut models = parse_grok_models(&output);
            for model in &mut models {
                if let Some((efforts, default)) = cached.get(&model.value) {
                    model.efforts = efforts.clone();
                    model.default_effort = default.clone();
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
        "claude" => (&["opus", "sonnet", "haiku"], ""),
        "codex" => (&["gpt-5.6-luna"], "medium"),
        "grok" => (&["grok-4.6"], "high"),
        _ => (&[], ""),
    };
    values
        .iter()
        .map(|value| ModelInfo {
            value: value.to_string(),
            label: display_model_name(value),
            efforts: Vec::new(),
            default_effort: default_effort.into(),
        })
        .collect()
}

/// Models for a provider: what the installed CLI reports, else the fallback.
/// Returns whether the list came from the CLI.
fn provider_models(id: &str, program: Option<&Path>) -> (Vec<ModelInfo>, bool) {
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
    }
    (models, detected)
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
/// a saved model has disappeared, the CLI's first (normally default) model is
/// selected and its reported effort levels are applied. Returns human-readable
/// descriptions of every repair so callers can decide whether to persist and
/// reload a running scheduler.
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
        let fallback = &tool.models[0];

        let worker = tool
            .models
            .iter()
            .find(|model| model.value == provider.model)
            .unwrap_or(fallback);
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

        let router = tool
            .models
            .iter()
            .find(|model| model.value == provider.router_model)
            .unwrap_or(fallback);
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
                let model = tool
                    .models
                    .iter()
                    .find(|model| model.value == tier.model)
                    .unwrap_or(fallback);
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
            (tool.models, tool.models_detected) = provider_models(&tool.id, program.as_deref());
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
    fn reads_grok_efforts_from_the_cli_model_cache() {
        let efforts = parse_grok_efforts(
            r#"{"models":{"grok-4.6":{"info":{"reasoning_efforts":[{"value":"high","default":true},{"value":"low","default":false}]}},"plain":{"info":{}}}}"#,
        );
        assert_eq!(efforts.len(), 1);
        assert_eq!(efforts["grok-4.6"].0, ["high", "low"]);
        assert_eq!(efforts["grok-4.6"].1, "high");
    }

    #[test]
    fn providers_without_a_catalog_still_get_dropdown_options() {
        for id in crate::config::KNOWN_PROVIDERS {
            let (models, detected) = provider_models(id, None);
            assert!(!detected, "{id} has no CLI to read");
            assert!(!models.is_empty(), "{id} needs fallback models");
            assert!(models.iter().all(|model| !model.efforts.is_empty()));
        }
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
        }];
        tool.models_detected = false;

        assert!(reconcile_config_models(&mut config, &[tool]).is_empty());
        assert_eq!(
            config.provider("grok").unwrap().router_model,
            "grok-private-preview"
        );
    }
}
