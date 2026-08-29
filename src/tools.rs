use crate::config::AppConfig;
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::Command;

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
    }
}

pub fn detect(config: &AppConfig) -> Vec<ToolInfo> {
    let mut tools = vec![
        basic_tool("git", "Git", "git", true, ""),
        basic_tool("gh", "GitHub CLI", "gh", true, &config.gh_bin),
        basic_tool("python", "Python 3", "python3", true, &config.python_bin),
        basic_tool("node", "Node.js", "node", false, ""),
        basic_tool("npm", "npm", "npm", false, ""),
        basic_tool("claude", "Claude Code", "claude", true, &config.claude_bin),
        basic_tool("codex", "Codex CLI", "codex", true, &config.codex_bin),
    ];
    let npm_available = tools.iter().any(|tool| tool.id == "npm" && tool.installed);
    for tool in &mut tools {
        match tool.id.as_str() {
            "gh" if tool.installed => {
                let (ready, _) = command_output(
                    Path::new(&tool.path),
                    &["auth", "status", "--hostname", &config.github_host],
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
            "claude" | "codex" => {
                tool.installable = npm_available;
                tool.status = if npm_available {
                    "Ready to install"
                } else {
                    "Install Node.js/npm first"
                }
                .into();
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
        _ => {
            return Err("Only Claude Code and Codex CLI can be installed from this screen.".into())
        }
    };
    Ok((npm, vec!["install".into(), "-g".into(), package.into()]))
}
