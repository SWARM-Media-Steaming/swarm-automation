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
    /// Models and reasoning levels reported by this installed provider CLI.
    /// An empty list means the CLI is missing or does not expose a catalog.
    pub models: Vec<ModelInfo>,
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

fn values_in_parentheses(text: &str, option: &str) -> Vec<String> {
    let Some(start) = text.find(option) else {
        return Vec::new();
    };
    let excerpt = &text[start..text.len().min(start + 500)];
    let Some(open) = excerpt.find('(') else {
        return Vec::new();
    };
    let Some(close) = excerpt[open + 1..].find(')') else {
        return Vec::new();
    };
    excerpt[open + 1..open + 1 + close]
        .split(',')
        .map(|value| value.trim().trim_matches(['\'', '"']))
        .filter(|value| {
            !value.is_empty()
                && value
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric() || character == '-')
        })
        .map(str::to_string)
        .collect()
}

fn parse_claude_models(help: &str) -> Vec<ModelInfo> {
    let efforts = values_in_parentheses(help, "--effort <level>");
    let Some(start) = help.find("--model <model>") else {
        return Vec::new();
    };
    let excerpt = &help[start..help.len().min(start + 700)];
    let quoted = regex::Regex::new(r"'([a-z][a-z0-9._-]+)'").expect("valid model regex");
    let mut values = Vec::new();
    for capture in quoted.captures_iter(excerpt) {
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

fn provider_models(id: &str, program: &Path) -> Vec<ModelInfo> {
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
        "grok" => parse_grok_models(&output),
        _ => Vec::new(),
    }
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
        if crate::config::KNOWN_PROVIDERS.contains(&tool.id.as_str()) && tool.installed {
            tool.models = provider_models(&tool.id, Path::new(&tool.path));
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
}
