use crate::tools;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Read;
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const TEST_DEFINITION_PATH: &str = ".swarm/tests.json";
const TEST_INPUTS_FILE: &str = "test-inputs.json";

fn default_version() -> u32 {
    1
}

fn default_true() -> bool {
    true
}

fn default_timeout() -> u64 {
    1800
}

fn default_health_timeout() -> u64 {
    3
}

fn default_reporting_timeout() -> u64 {
    300
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TestDefinition {
    #[serde(default = "default_version")]
    pub version: u32,
    #[serde(default)]
    pub suites: Vec<TestSuiteDefinition>,
    #[serde(default)]
    pub reporting: Option<ReportingDefinition>,
    #[serde(default)]
    pub failure_triage: Option<ReportingDefinition>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TestSuiteDefinition {
    pub id: String,
    pub name: String,
    pub command: Vec<String>,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u64,
    #[serde(default)]
    pub disruptive: bool,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub requirements: Requirements,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Requirements {
    #[serde(default)]
    pub executables: Vec<String>,
    #[serde(default)]
    pub files: Vec<String>,
    #[serde(default)]
    pub servers: Vec<ServerRequirement>,
    #[serde(default)]
    pub mounts: Vec<MountRequirement>,
    #[serde(default)]
    pub credentials: Vec<CredentialRequirement>,
    #[serde(default)]
    pub devices: Vec<DeviceRequirement>,
    /// Data this suite needs that isn't deterministic or fixed ahead of time.
    /// When non-empty, the suite needs an enabled AI provider with usage
    /// headroom before it can run; see `run_once` and `check_ai_capability`.
    #[serde(default)]
    pub ai_test_data: Vec<AiTestDataRequirement>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AiTestDataRequirement {
    /// Written to `<SWARM_AI_TEST_DATA_DIR>/<name>.txt` for the suite command
    /// to read.
    pub name: String,
    /// Plain-language description of the data needed, sent to the AI
    /// provider as-is. The suite should treat the result as best-effort,
    /// unverified sample data — never as real or authoritative.
    pub prompt: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ServerRequirement {
    pub name: String,
    pub host: String,
    pub port: u16,
    #[serde(default = "default_health_timeout")]
    pub timeout_seconds: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MountRequirement {
    pub name: String,
    pub path: String,
    #[serde(default)]
    pub kind: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CredentialRequirement {
    pub name: String,
    #[serde(default)]
    pub environment: String,
    #[serde(default)]
    pub file: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DeviceRequirement {
    #[serde(rename = "type")]
    pub device_type: String,
    #[serde(default = "default_device_input")]
    pub input: String,
}

fn default_device_input() -> String {
    "fireTvSerial".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReportingDefinition {
    /// Deterministic repository command that receives SWARM_TEST_RESULTS.
    /// It can retain an existing GitHub issue update without involving AI.
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default = "default_reporting_timeout")]
    pub timeout_seconds: u64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DetectedDevice {
    pub serial: String,
    pub state: String,
    pub description: String,
    pub eligible: bool,
    pub selected: bool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RequirementStatus {
    pub kind: String,
    pub label: String,
    pub state: String,
    pub detail: String,
    pub action: String,
    pub input_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SuiteResult {
    pub id: String,
    pub name: String,
    pub state: String,
    pub blocked: bool,
    pub detail: String,
    pub exit_code: Option<i32>,
    pub started_at: Option<u64>,
    pub finished_at: Option<u64>,
    pub duration_ms: Option<u64>,
    #[serde(default)]
    pub output: String,
    /// What an AI provider generated for this suite's `ai_test_data`
    /// requirements, if any — the run's own record of what was made up, per
    /// the repository's test definition.
    #[serde(default)]
    pub ai_generated_data: Vec<AiGeneratedDataRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AiGeneratedDataRecord {
    pub name: String,
    pub provider: String,
    /// Truncated preview of the generated data, kept short so run history
    /// stays small; the full text lives under `ai-data/<suite id>/` in the
    /// run directory.
    pub summary: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SuitePlan {
    #[serde(flatten)]
    pub result: SuiteResult,
    pub command: String,
    pub timeout_seconds: u64,
    pub disruptive: bool,
    pub requirements: Vec<RequirementStatus>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TestPlan {
    pub available: bool,
    pub definition_path: String,
    pub results_path: String,
    pub error: String,
    pub selected_device: String,
    pub device_selection_required: bool,
    pub devices: Vec<DetectedDevice>,
    pub suites: Vec<SuitePlan>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TestDefinitionDraft {
    pub definition: String,
    pub detected_suites: usize,
    pub notes: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TestRunResults {
    pub schema_version: u32,
    pub repository: String,
    pub definition_path: String,
    pub started_at: u64,
    pub finished_at: Option<u64>,
    /// How the run was initiated: `"manual"` (Run now) or `"scheduled"`.
    #[serde(default)]
    pub trigger: String,
    pub suites: Vec<SuiteResult>,
}

pub fn definition_path(workspace: &Path) -> PathBuf {
    workspace.join(TEST_DEFINITION_PATH)
}

pub fn available(workspace: &Path) -> bool {
    definition_path(workspace).is_file()
}

fn boilerplate_suite(
    id: &str,
    name: &str,
    command: &[&str],
    executables: &[&str],
    files: &[&str],
) -> TestSuiteDefinition {
    TestSuiteDefinition {
        id: id.into(),
        name: name.into(),
        command: command.iter().map(|value| (*value).into()).collect(),
        timeout_seconds: default_timeout(),
        disruptive: false,
        enabled: true,
        requirements: Requirements {
            executables: executables.iter().map(|value| (*value).into()).collect(),
            files: files.iter().map(|value| (*value).into()).collect(),
            ..Requirements::default()
        },
    }
}

fn suite_id(value: &str) -> String {
    let mut id = value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() {
                character.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect::<String>();
    while id.contains("--") {
        id = id.replace("--", "-");
    }
    id.trim_matches('-').to_string()
}

fn suite_name(value: &str) -> String {
    value
        .split(|character: char| !character.is_ascii_alphanumeric())
        .filter(|part| !part.is_empty())
        .map(|part| {
            let mut characters = part.chars();
            characters
                .next()
                .map(|first| first.to_ascii_uppercase().to_string() + characters.as_str())
                .unwrap_or_default()
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Build a reviewable definition from conventional, repository-owned test
/// entry points. Detection reads manifests and filenames only; it never runs
/// a discovered command.
pub fn detect_definition(
    workspace: &Path,
    ai: Option<&AiRunOptions>,
) -> Result<TestDefinitionDraft, String> {
    if !workspace.is_dir() {
        return Err(format!(
            "The repository workspace does not exist at {}. Clone or choose it first.",
            workspace.display()
        ));
    }

    let mut suites = Vec::new();
    let mut notes = Vec::new();
    let cargo_manifest = workspace.join("Cargo.toml");
    if cargo_manifest.is_file() {
        let manifest = fs::read_to_string(&cargo_manifest).unwrap_or_default();
        let (id, name, command): (&str, &str, &[&str]) = if manifest.contains("[workspace]") {
            (
                "rust-workspace",
                "Rust workspace tests",
                &["cargo", "test", "--workspace"],
            )
        } else {
            ("rust", "Rust tests", &["cargo", "test"])
        };
        suites.push(boilerplate_suite(
            id,
            name,
            command,
            &["cargo"],
            &["Cargo.toml"],
        ));
    }

    let package_json = workspace.join("package.json");
    if package_json.is_file() {
        let has_test_script = fs::read_to_string(&package_json)
            .ok()
            .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
            .and_then(|value| {
                value
                    .pointer("/scripts/test")
                    .and_then(|test| test.as_str())
                    .map(str::to_string)
            })
            .is_some_and(|script| {
                let normalized = script.to_ascii_lowercase();
                !script.trim().is_empty() && !normalized.contains("no test specified")
            });
        if has_test_script {
            let (executable, command): (&str, &[&str]) =
                if workspace.join("pnpm-lock.yaml").is_file() {
                    ("pnpm", &["pnpm", "test"])
                } else if workspace.join("yarn.lock").is_file() {
                    ("yarn", &["yarn", "test"])
                } else {
                    ("npm", &["npm", "test"])
                };
            suites.push(boilerplate_suite(
                "javascript",
                "JavaScript tests",
                command,
                &[executable],
                &["package.json"],
            ));
        }
    }

    if ["pyproject.toml", "pytest.ini", "tox.ini"]
        .iter()
        .any(|name| workspace.join(name).is_file())
    {
        let manifest = ["pyproject.toml", "pytest.ini", "tox.ini"]
            .iter()
            .find(|name| workspace.join(name).is_file())
            .copied()
            .unwrap_or("pyproject.toml");
        suites.push(boilerplate_suite(
            "python",
            "Python tests",
            &["python3", "-m", "pytest"],
            &["python3"],
            &[manifest],
        ));
    }

    if workspace.join("go.mod").is_file() {
        suites.push(boilerplate_suite(
            "go",
            "Go tests",
            &["go", "test", "./..."],
            &["go"],
            &["go.mod"],
        ));
    }

    let gradle_wrappers = ["gradlew", "clients/tv-android/gradlew"];
    for wrapper in gradle_wrappers {
        if !workspace.join(wrapper).is_file() {
            continue;
        }
        let mut suite = if wrapper == "gradlew" {
            boilerplate_suite(
                "gradle",
                "Gradle tests",
                &["./gradlew", "test"],
                &["java"],
                &["gradlew"],
            )
        } else {
            boilerplate_suite(
                "android",
                "Android tests",
                &[
                    "./clients/tv-android/gradlew",
                    "-p",
                    "clients/tv-android",
                    "test",
                ],
                &["java"],
                &["clients/tv-android/gradlew"],
            )
        };
        suite.timeout_seconds = 3600;
        suites.push(suite);
    }

    let scripts_dir = workspace.join("scripts/tests");
    if scripts_dir.is_dir() {
        let mut scripts = fs::read_dir(&scripts_dir)
            .map_err(|error| format!("Could not inspect {}: {error}", scripts_dir.display()))?
            .filter_map(Result::ok)
            .filter(|entry| entry.path().is_file())
            .filter_map(|entry| entry.file_name().into_string().ok())
            .filter(|name| name.ends_with(".sh"))
            .filter(|name| {
                let stem = name.trim_end_matches(".sh");
                (stem.starts_with("test_")
                    || stem.ends_with("_tests")
                    || matches!(
                        stem,
                        "tv_e2e_suite" | "tv_uat_suite" | "tv_uat_resilience_suite"
                    ))
                    && !stem.contains("cron")
                    && !stem.starts_with("full_")
            })
            .collect::<Vec<_>>();
        scripts.sort();
        for filename in scripts {
            let stem = filename.trim_end_matches(".sh");
            let relative = format!("scripts/tests/{filename}");
            let mut suite = boilerplate_suite(
                &suite_id(stem),
                &suite_name(stem),
                &["bash", &relative],
                &["bash"],
                &[&relative],
            );
            if stem.starts_with("tv_") {
                suite.requirements.executables.push("adb".into());
                suite.requirements.devices.push(DeviceRequirement {
                    device_type: "fireTv".into(),
                    input: default_device_input(),
                });
                suite.timeout_seconds = 7200;
            }
            if stem.contains("resilience") || stem.contains("disruptive") {
                suite.disruptive = true;
            }
            suites.push(suite);
        }
    }

    let mut detected_suites = suites.len();
    if suites.is_empty() {
        let ai_found = ai.filter(|options| options.enabled).and_then(|options| {
            let capability = check_ai_capability(
                &options.python_bin,
                &options.script_dir,
                &options.providers,
                options.minimum_remaining_percent,
            );
            if !capability.available {
                notes.push(format!(
                    "AI-assisted discovery was skipped: {}",
                    capability.detail
                ));
                return None;
            }
            match ai_discover_suites(workspace, options, &capability) {
                Ok((found, ai_notes)) => {
                    notes.extend(ai_notes);
                    Some(found)
                }
                Err(error) => {
                    notes.push(format!("AI-assisted discovery could not run: {error}"));
                    None
                }
            }
        });
        match ai_found {
            Some(found) if !found.is_empty() => {
                notes.push(format!(
                    "No conventional test entry points were found, so AI suggested {} possible one(s) \
                     from the repository layout. Review the commands and requirements carefully — \
                     they start disabled until you turn them on.",
                    found.len()
                ));
                suites.extend(found);
                detected_suites = suites.len();
            }
            _ => {
                notes.push("No conventional test entry points were found. Replace the disabled placeholder command before enabling it.".into());
                let mut placeholder = boilerplate_suite(
                    "project-tests",
                    "Project tests",
                    &["replace-with-test-command"],
                    &[],
                    &[],
                );
                placeholder.enabled = false;
                suites.push(placeholder);
            }
        }
    } else {
        notes.push("Review commands and requirements before saving; detection never executes discovered files.".into());
    }
    if suites
        .iter()
        .any(|suite| !suite.requirements.devices.is_empty())
    {
        notes.push("Fire TV suites were marked as device-dependent; disruptive resilience suites require the repository opt-in.".into());
    }

    let definition = TestDefinition {
        version: 1,
        suites,
        reporting: None,
        failure_triage: None,
    };
    validate_definition(&definition)?;
    let definition = serde_json::to_string_pretty(&definition)
        .map_err(|error| format!("Could not create the test definition draft: {error}"))?;
    Ok(TestDefinitionDraft {
        definition: format!("{definition}\n"),
        detected_suites,
        notes,
    })
}

/// Validate and atomically create a repository-owned test definition. Existing
/// definitions are never overwritten by the onboarding flow.
pub fn create_definition(workspace: &Path, raw: &str) -> Result<PathBuf, String> {
    let path = definition_path(workspace);
    if path.exists() {
        return Err(format!(
            "{} already exists. Edit the repository file directly to avoid overwriting it.",
            path.display()
        ));
    }
    let definition: TestDefinition = serde_json::from_str(raw)
        .map_err(|error| format!("The draft is not valid JSON: {error}"))?;
    validate_definition(&definition)?;
    let normalized = serde_json::to_string_pretty(&definition)
        .map_err(|error| format!("Could not format the test definition: {error}"))?;
    let parent = path
        .parent()
        .ok_or_else(|| "The test definition has no parent directory".to_string())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("Could not create {}: {error}", parent.display()))?;
    let temporary = parent.join(format!(".tests.json.tmp-{}", std::process::id()));
    fs::write(&temporary, format!("{normalized}\n"))
        .map_err(|error| format!("Could not write {}: {error}", temporary.display()))?;
    if path.exists() {
        let _ = fs::remove_file(&temporary);
        return Err(format!(
            "{} was created by another process; nothing was overwritten.",
            path.display()
        ));
    }
    fs::rename(&temporary, &path).map_err(|error| {
        let _ = fs::remove_file(&temporary);
        format!("Could not install {}: {error}", path.display())
    })?;
    Ok(path)
}

/// Subdirectory of the run directory that keeps one JSON file per completed
/// test run so the UI can show run history and per-suite outcomes.
const HISTORY_DIR: &str = "test-runs";
const HISTORY_LIMIT: usize = 50;

pub fn load_definition(workspace: &Path) -> Result<TestDefinition, String> {
    let path = definition_path(workspace);
    let raw = fs::read_to_string(&path)
        .map_err(|error| format!("Could not read {}: {error}", path.display()))?;
    let definition: TestDefinition = serde_json::from_str(&raw)
        .map_err(|error| format!("Invalid {}: {error}", path.display()))?;
    validate_definition(&definition)?;
    Ok(definition)
}

fn validate_definition(definition: &TestDefinition) -> Result<(), String> {
    if definition.version != 1 {
        return Err(format!(
            "Unsupported test definition version {}; expected 1",
            definition.version
        ));
    }
    if definition.suites.is_empty() {
        return Err("The test definition must contain at least one suite".into());
    }
    let mut ids = std::collections::HashSet::new();
    for suite in &definition.suites {
        if suite.id.trim().is_empty()
            || !suite
                .id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
        {
            return Err("Suite ids may contain only letters, digits, '-' and '_'".into());
        }
        if !ids.insert(suite.id.as_str()) {
            return Err(format!("Duplicate suite id '{}'", suite.id));
        }
        if suite.name.trim().is_empty() || suite.command.is_empty() || suite.command[0].is_empty() {
            return Err(format!("Suite '{}' needs a name and command", suite.id));
        }
        if suite.timeout_seconds == 0 {
            return Err(format!(
                "Suite '{}' timeout must be at least one second",
                suite.id
            ));
        }
        for device in &suite.requirements.devices {
            if device.device_type != "fireTv" {
                return Err(format!(
                    "Suite '{}' uses unsupported device type '{}'",
                    suite.id, device.device_type
                ));
            }
        }
        for credential in &suite.requirements.credentials {
            if credential.environment.trim().is_empty() == credential.file.trim().is_empty() {
                return Err(format!(
                    "Credential '{}' in suite '{}' must set exactly one of environment or file",
                    credential.name, suite.id
                ));
            }
        }
    }
    Ok(())
}

pub fn discover_fire_tv_devices() -> Vec<DetectedDevice> {
    let Some(adb) = tools::find_executable("adb", "") else {
        return Vec::new();
    };
    let Ok(output) = Command::new(adb).args(["devices", "-l"]).output() else {
        return Vec::new();
    };
    parse_adb_devices(&String::from_utf8_lossy(&output.stdout))
}

fn parse_adb_devices(output: &str) -> Vec<DetectedDevice> {
    output
        .lines()
        .skip_while(|line| !line.starts_with("List of devices"))
        .skip(1)
        .filter_map(|line| {
            let mut fields = line.split_whitespace();
            let serial = fields.next()?.to_string();
            let state = fields.next()?.to_string();
            let description = fields.collect::<Vec<_>>().join(" ");
            Some(DetectedDevice {
                serial,
                eligible: state == "device",
                state,
                description,
                selected: false,
            })
        })
        .collect()
}

fn select_device(devices: &mut [DetectedDevice], saved: &str) -> (String, bool) {
    let eligible: Vec<usize> = devices
        .iter()
        .enumerate()
        .filter_map(|(index, device)| device.eligible.then_some(index))
        .collect();
    let selected = eligible
        .iter()
        .copied()
        .find(|index| devices[*index].serial == saved)
        .or_else(|| {
            if eligible.len() == 1 {
                eligible.first().copied()
            } else {
                None
            }
        });
    if let Some(index) = selected {
        devices[index].selected = true;
        (devices[index].serial.clone(), false)
    } else {
        (String::new(), eligible.len() > 1)
    }
}

pub fn build_plan(
    workspace: &Path,
    run_dir: &Path,
    saved_inputs: &HashMap<String, String>,
    allow_disruptive: bool,
) -> TestPlan {
    let path = definition_path(workspace);
    let results_path = run_dir.join("test-results.json");
    if !path.is_file() {
        return TestPlan {
            available: false,
            definition_path: String::new(),
            results_path: results_path.to_string_lossy().into_owned(),
            error: format!("Add {TEST_DEFINITION_PATH} to this repository"),
            selected_device: String::new(),
            device_selection_required: false,
            devices: Vec::new(),
            suites: Vec::new(),
        };
    }
    let definition = match load_definition(workspace) {
        Ok(definition) => definition,
        Err(error) => {
            return TestPlan {
                available: false,
                definition_path: path.to_string_lossy().into_owned(),
                results_path: results_path.to_string_lossy().into_owned(),
                error,
                selected_device: String::new(),
                device_selection_required: false,
                devices: Vec::new(),
                suites: Vec::new(),
            }
        }
    };
    let needs_device = definition
        .suites
        .iter()
        .any(|suite| !suite.requirements.devices.is_empty());
    let mut devices = if needs_device {
        discover_fire_tv_devices()
    } else {
        Vec::new()
    };
    let saved = saved_inputs
        .get("fireTvSerial")
        .map(String::as_str)
        .unwrap_or_default();
    let (selected_device, device_selection_required) = select_device(&mut devices, saved);
    let previous = read_results(&results_path)
        .map(|results| {
            results
                .suites
                .into_iter()
                .map(|suite| (suite.id.clone(), suite))
                .collect::<HashMap<_, _>>()
        })
        .unwrap_or_default();
    let suites = definition
        .suites
        .iter()
        .map(|suite| {
            let requirements = evaluate_requirements(workspace, suite, &devices, &selected_device);
            let waiting = requirements.iter().any(|item| item.state == "waiting");
            let missing = requirements.iter().any(|item| item.state == "missing");
            let disruptive_blocked = suite.disruptive && !allow_disruptive;
            let mut result = previous.get(&suite.id).cloned().unwrap_or(SuiteResult {
                id: suite.id.clone(),
                name: suite.name.clone(),
                state: "Ready".into(),
                blocked: false,
                detail: String::new(),
                exit_code: None,
                started_at: None,
                finished_at: None,
                duration_ms: None,
                output: String::new(),
                ai_generated_data: Vec::new(),
            });
            if !suite.enabled {
                result.state = "Skipped".into();
                result.blocked = false;
                result.detail = "Disabled by the repository test definition".into();
            } else if disruptive_blocked {
                result.state = "Skipped".into();
                result.blocked = true;
                result.detail = "Disruptive suites are not enabled for this repository".into();
            } else if waiting {
                result.state = "Waiting for input".into();
                result.blocked = true;
                result.detail = "Choose a detected device to continue".into();
            } else if missing {
                result.state = "Skipped".into();
                result.blocked = true;
                result.detail = requirements
                    .iter()
                    .filter(|item| item.state == "missing")
                    .map(|item| item.detail.as_str())
                    .collect::<Vec<_>>()
                    .join("; ");
            }
            SuitePlan {
                result,
                command: suite.command.join(" "),
                timeout_seconds: suite.timeout_seconds,
                disruptive: suite.disruptive,
                requirements,
            }
        })
        .collect();
    TestPlan {
        available: true,
        definition_path: path.to_string_lossy().into_owned(),
        results_path: results_path.to_string_lossy().into_owned(),
        error: String::new(),
        selected_device,
        device_selection_required,
        devices,
        suites,
    }
}

fn evaluate_requirements(
    workspace: &Path,
    suite: &TestSuiteDefinition,
    devices: &[DetectedDevice],
    selected_device: &str,
) -> Vec<RequirementStatus> {
    let mut statuses = Vec::new();
    for executable in &suite.requirements.executables {
        let found = tools::find_executable(executable, "");
        statuses.push(requirement(
            "executable",
            executable,
            found.is_some(),
            found
                .map(|path| path.to_string_lossy().into_owned())
                .unwrap_or_else(|| format!("{executable} was not found on PATH")),
            format!("Install {executable} and refresh requirements"),
        ));
    }
    for relative in &suite.requirements.files {
        let path = workspace.join(relative);
        statuses.push(requirement(
            "file",
            relative,
            path.exists(),
            if path.exists() {
                path.to_string_lossy().into_owned()
            } else {
                format!("{} is missing", path.display())
            },
            format!("Create or restore {relative}"),
        ));
    }
    for server in &suite.requirements.servers {
        let ready = server_ready(server);
        statuses.push(requirement(
            "server",
            &server.name,
            ready,
            if ready {
                format!("{}:{} accepted a connection", server.host, server.port)
            } else {
                format!("{}:{} is not reachable", server.host, server.port)
            },
            format!("Start {} and verify its address", server.name),
        ));
    }
    for mount in &suite.requirements.mounts {
        let path = Path::new(&mount.path);
        let ready = path.is_dir() && mount_kind_matches(path, &mount.kind);
        statuses.push(requirement(
            "mount",
            &mount.name,
            ready,
            if ready {
                format!("{} is mounted", path.display())
            } else {
                format!(
                    "{} is not mounted{}",
                    path.display(),
                    if mount.kind.is_empty() {
                        ""
                    } else {
                        " with the required filesystem"
                    }
                )
            },
            format!("Mount {} and refresh requirements", mount.name),
        ));
    }
    for credential in &suite.requirements.credentials {
        let (ready, detail, action) = if !credential.environment.is_empty() {
            let ready = std::env::var_os(&credential.environment).is_some();
            (
                ready,
                if ready {
                    format!("{} is set", credential.environment)
                } else {
                    format!("{} is not set", credential.environment)
                },
                format!(
                    "Set {} before starting SWARM Automation",
                    credential.environment
                ),
            )
        } else {
            let path = expand_home(&credential.file);
            (
                path.is_file(),
                if path.is_file() {
                    format!("{} exists", path.display())
                } else {
                    format!("{} is missing", path.display())
                },
                format!("Add the credential file at {}", path.display()),
            )
        };
        statuses.push(requirement(
            "credential",
            &credential.name,
            ready,
            detail,
            action,
        ));
    }
    for device in &suite.requirements.devices {
        let eligible = devices.iter().filter(|item| item.eligible).count();
        let ready = !selected_device.is_empty();
        let waiting = !ready && eligible > 1;
        statuses.push(RequirementStatus {
            kind: "device".into(),
            label: "Fire TV".into(),
            state: if ready {
                "ready"
            } else if waiting {
                "waiting"
            } else {
                "missing"
            }
            .into(),
            detail: if ready {
                format!("Using adb device {selected_device}")
            } else if waiting {
                "Multiple eligible adb devices were detected".into()
            } else {
                "No authorized adb device was detected".into()
            },
            action: if waiting {
                "Choose a device below".into()
            } else if ready {
                String::new()
            } else {
                "Connect a Fire TV, enable adb debugging, and refresh".into()
            },
            input_key: device.input.clone(),
        });
    }
    if !suite.requirements.ai_test_data.is_empty() {
        let names = suite
            .requirements
            .ai_test_data
            .iter()
            .map(|item| item.name.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        statuses.push(RequirementStatus {
            kind: "ai".into(),
            label: "AI test data".into(),
            state: "ready".into(),
            detail: format!(
                "Generates: {names}. Checked automatically right before this suite runs; needs an \
                 enabled AI provider with usage available, or it runs as \"Not executed\"."
            ),
            action: String::new(),
            input_key: String::new(),
        });
    }
    statuses
}

fn requirement(
    kind: &str,
    label: &str,
    ready: bool,
    detail: String,
    action: String,
) -> RequirementStatus {
    RequirementStatus {
        kind: kind.into(),
        label: label.into(),
        state: if ready { "ready" } else { "missing" }.into(),
        detail,
        action: if ready { String::new() } else { action },
        input_key: String::new(),
    }
}

fn server_ready(server: &ServerRequirement) -> bool {
    let Ok(addresses) = (server.host.as_str(), server.port).to_socket_addrs() else {
        return false;
    };
    let timeout = Duration::from_secs(server.timeout_seconds.max(1));
    addresses
        .into_iter()
        .any(|address| TcpStream::connect_timeout(&address, timeout).is_ok())
}

fn mount_kind_matches(path: &Path, kind: &str) -> bool {
    if kind.trim().is_empty() || kind == "any" {
        return true;
    }
    let output = Command::new("/sbin/mount").output();
    let Ok(output) = output else { return false };
    let needle = path.to_string_lossy();
    String::from_utf8_lossy(&output.stdout).lines().any(|line| {
        line.contains(needle.as_ref())
            && match kind {
                "smb" => {
                    line.to_ascii_lowercase().contains("smb")
                        || line.to_ascii_lowercase().contains("cifs")
                }
                other => line
                    .to_ascii_lowercase()
                    .contains(&other.to_ascii_lowercase()),
            }
    })
}

fn expand_home(value: &str) -> PathBuf {
    if let Some(rest) = value.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(value)
}

fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn read_results(path: &Path) -> Option<TestRunResults> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

pub fn save_inputs(run_dir: &Path, inputs: &HashMap<String, String>) -> Result<(), String> {
    fs::create_dir_all(run_dir).map_err(|error| error.to_string())?;
    let path = run_dir.join(TEST_INPUTS_FILE);
    let temporary = run_dir.join(format!("{TEST_INPUTS_FILE}.tmp"));
    fs::write(
        &temporary,
        serde_json::to_vec_pretty(inputs).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    fs::rename(temporary, path).map_err(|error| error.to_string())
}

fn load_inputs(run_dir: &Path, fallback: &HashMap<String, String>) -> HashMap<String, String> {
    fs::read(run_dir.join(TEST_INPUTS_FILE))
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_else(|| fallback.clone())
}

fn input_signature(run_dir: &Path) -> Vec<u8> {
    fs::read(run_dir.join(TEST_INPUTS_FILE)).unwrap_or_default()
}

fn write_results(path: &Path, results: &TestRunResults) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "Results path has no parent".to_string())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let temporary = path.with_extension("json.tmp");
    fs::write(
        &temporary,
        serde_json::to_vec_pretty(results).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    fs::rename(&temporary, path).map_err(|error| error.to_string())
}

/// Records a finished run under `<run-dir>/test-runs/<started_at>.json` and
/// prunes the oldest entries beyond `HISTORY_LIMIT`. Per-suite command output
/// is dropped from the archived copy so the history stays small; the live
/// `test-results.json` keeps the full output for the most recent run.
fn append_history(run_dir: &Path, results: &TestRunResults) {
    let dir = run_dir.join(HISTORY_DIR);
    if fs::create_dir_all(&dir).is_err() {
        return;
    }
    let mut archived = results.clone();
    for suite in &mut archived.suites {
        suite.output.clear();
    }
    let Ok(bytes) = serde_json::to_vec_pretty(&archived) else {
        return;
    };
    let path = dir.join(format!("{}.json", results.started_at));
    if fs::write(&path, bytes).is_err() {
        return;
    }
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("json"))
        .collect();
    if files.len() > HISTORY_LIMIT {
        files.sort();
        for stale in files.iter().take(files.len() - HISTORY_LIMIT) {
            let _ = fs::remove_file(stale);
        }
    }
}

/// Every archived run for `run_dir`, newest first, capped at `HISTORY_LIMIT`.
pub fn list_runs(run_dir: &Path) -> Vec<TestRunResults> {
    let dir = run_dir.join(HISTORY_DIR);
    let mut runs: Vec<TestRunResults> = fs::read_dir(&dir)
        .into_iter()
        .flatten()
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("json"))
        .filter_map(|path| serde_json::from_slice(&fs::read(path).ok()?).ok())
        .collect();
    runs.sort_by_key(|run| std::cmp::Reverse(run.started_at));
    runs.truncate(HISTORY_LIMIT);
    runs
}

/// One AI provider as far as the test scheduler's AI helpers need to know:
/// enough to probe its usage and, for Claude today, to run a one-shot
/// read-only completion. Mirrors `ResolvedProvider` in `main.rs`.
#[derive(Debug, Clone)]
pub struct AiProviderOption {
    pub id: String,
    pub bin: String,
    pub model: String,
    pub enabled: bool,
}

/// Everything a test run needs to know to gate and use AI gap-filling.
/// Built once per invocation in `main.rs`/`run_cli` from `AppConfig`.
#[derive(Debug, Clone)]
pub struct AiRunOptions {
    /// The repository's own switch — off guarantees zero AI usage from tests
    /// regardless of provider capacity.
    pub enabled: bool,
    pub python_bin: String,
    pub script_dir: PathBuf,
    pub providers: Vec<AiProviderOption>,
    pub minimum_remaining_percent: u8,
}

#[derive(Debug, Clone)]
pub struct AiCapability {
    pub available: bool,
    pub provider: String,
    pub detail: String,
}

const AI_TEST_ASSIST_SCRIPT: &str = "ai_test_assist.py";

fn run_ai_test_assist(
    python_bin: &str,
    script_dir: &Path,
    arguments: &[String],
) -> Result<serde_json::Value, String> {
    let script = script_dir.join(AI_TEST_ASSIST_SCRIPT);
    if !script.is_file() {
        return Err("The AI test-assist helper is not available in this build".into());
    }
    let output = Command::new(python_bin)
        .arg(&script)
        .args(arguments)
        .env("PATH", tools::enhanced_path())
        .env("SWARM_ISSUE_WORKER_SCRIPT_DIR", script_dir)
        .output()
        .map_err(|error| format!("Could not run the AI test-assist helper: {error}"))?;
    serde_json::from_slice(&output.stdout).map_err(|error| {
        let detail = String::from_utf8_lossy(&output.stderr);
        format!("The AI test-assist helper returned an unexpected response: {error} ({detail})")
    })
}

/// Probes enabled providers in order and returns the first with usage
/// headroom above `minimum_remaining_percent`. A single call covers every
/// suite in this run that needs AI test data.
pub fn check_ai_capability(
    python_bin: &str,
    script_dir: &Path,
    providers: &[AiProviderOption],
    minimum_remaining_percent: u8,
) -> AiCapability {
    let providers_json = serde_json::to_string(
        &providers
            .iter()
            .map(|provider| {
                serde_json::json!({
                    "id": provider.id,
                    "bin": provider.bin,
                    "enabled": provider.enabled,
                })
            })
            .collect::<Vec<_>>(),
    )
    .unwrap_or_else(|_| "[]".into());
    let arguments = vec![
        "capacity".into(),
        "--providers-json".into(),
        providers_json,
        "--minimum-percent".into(),
        minimum_remaining_percent.to_string(),
        "--python-bin".into(),
        python_bin.into(),
        "--script-dir".into(),
        script_dir.to_string_lossy().into_owned(),
    ];
    match run_ai_test_assist(python_bin, script_dir, &arguments) {
        Ok(value) => AiCapability {
            available: value["available"].as_bool().unwrap_or(false),
            provider: value["provider"].as_str().unwrap_or_default().into(),
            detail: value["detail"].as_str().unwrap_or_default().into(),
        },
        Err(error) => AiCapability {
            available: false,
            provider: String::new(),
            detail: error,
        },
    }
}

/// Generates best-effort data for every `ai_test_data` requirement of one
/// suite, writes each to `<run-dir>/ai-data/<suite id>/<name>.txt`, and
/// returns the directory plus a short record for the test run's history.
fn generate_suite_ai_test_data(
    run_dir: &Path,
    suite: &TestSuiteDefinition,
    ai: &AiRunOptions,
    capability: &AiCapability,
) -> Result<(PathBuf, Vec<AiGeneratedDataRecord>), String> {
    let provider = ai
        .providers
        .iter()
        .find(|candidate| candidate.id == capability.provider)
        .ok_or_else(|| format!("Provider '{}' is no longer configured", capability.provider))?;
    let dir = run_dir.join("ai-data").join(&suite.id);
    fs::create_dir_all(&dir).map_err(|error| error.to_string())?;
    let mut records = Vec::new();
    for requirement in &suite.requirements.ai_test_data {
        let prompt = format!(
            "You are generating best-effort placeholder data for an automated test suite named \
             '{}'. The data will never be treated as real or verified — it only needs to plausibly \
             match what is asked for below. Respond with ONLY the data itself, no explanation, no \
             markdown fences.\n\nData needed ('{}'): {}",
            suite.name, requirement.name, requirement.prompt
        );
        let arguments = vec![
            "generate".into(),
            "--provider".into(),
            provider.id.clone(),
            "--bin".into(),
            provider.bin.clone(),
            "--model".into(),
            provider.model.clone(),
            "--prompt".into(),
            prompt,
            "--timeout".into(),
            "120".into(),
        ];
        let value = run_ai_test_assist(&ai.python_bin, &ai.script_dir, &arguments)?;
        if !value["ok"].as_bool().unwrap_or(false) {
            return Err(value["error"]
                .as_str()
                .unwrap_or("AI test-data generation failed")
                .to_string());
        }
        let text = value["text"].as_str().unwrap_or_default();
        fs::write(dir.join(format!("{}.txt", requirement.name)), text)
            .map_err(|error| error.to_string())?;
        let summary: String = if text.chars().count() > 160 {
            text.chars().take(160).collect::<String>() + "…"
        } else {
            text.to_string()
        };
        records.push(AiGeneratedDataRecord {
            name: requirement.name.clone(),
            provider: provider.id.clone(),
            summary,
        });
    }
    Ok((dir, records))
}

/// Asks AI to suggest test entry points a conventional-manifest scan could
/// not find, only ever called when that scan found nothing at all. Suggested
/// suites always come back disabled — a human reviews and enables them
/// explicitly, the same as the plain placeholder this replaces.
fn ai_discover_suites(
    workspace: &Path,
    ai: &AiRunOptions,
    capability: &AiCapability,
) -> Result<(Vec<TestSuiteDefinition>, Vec<String>), String> {
    let provider = ai
        .providers
        .iter()
        .find(|candidate| candidate.id == capability.provider)
        .ok_or_else(|| format!("Provider '{}' is no longer configured", capability.provider))?;
    let arguments = vec![
        "discover".into(),
        "--workspace".into(),
        workspace.to_string_lossy().into_owned(),
        "--provider".into(),
        provider.id.clone(),
        "--bin".into(),
        provider.bin.clone(),
        "--model".into(),
        provider.model.clone(),
        "--timeout".into(),
        "90".into(),
    ];
    let value = run_ai_test_assist(&ai.python_bin, &ai.script_dir, &arguments)?;
    if !value["ok"].as_bool().unwrap_or(false) {
        return Err(value["error"]
            .as_str()
            .unwrap_or("AI-assisted discovery failed")
            .to_string());
    }
    let notes = value["notes"]
        .as_array()
        .map(|notes| {
            notes
                .iter()
                .filter_map(|note| note.as_str().map(str::to_string))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let mut suites = Vec::new();
    let mut seen_ids = std::collections::HashSet::new();
    for raw in value["suites"].as_array().into_iter().flatten() {
        let Some(command) = raw["command"].as_array() else {
            continue;
        };
        let command: Vec<String> = command
            .iter()
            .filter_map(|part| part.as_str().map(str::to_string))
            .collect();
        if command.is_empty() || command.iter().any(String::is_empty) {
            continue;
        }
        let name = raw["name"]
            .as_str()
            .unwrap_or("AI-suggested suite")
            .to_string();
        let mut id = suite_id(raw["id"].as_str().unwrap_or(&name));
        if id.is_empty() {
            id = suite_id(&name);
        }
        while !seen_ids.insert(id.clone()) {
            id = format!("{id}-2");
        }
        suites.push(TestSuiteDefinition {
            id,
            name,
            command,
            timeout_seconds: raw["timeoutSeconds"].as_u64().unwrap_or(default_timeout()),
            disruptive: raw["disruptive"].as_bool().unwrap_or(false),
            // AI-suggested commands are unverified guesses; a human must
            // review and turn them on explicitly.
            enabled: false,
            requirements: Requirements::default(),
        });
    }
    Ok((suites, notes))
}

#[allow(clippy::too_many_arguments)]
pub fn run_once(
    workspace: &Path,
    run_dir: &Path,
    repository: &str,
    saved_inputs: &HashMap<String, String>,
    allow_disruptive: bool,
    triage_enabled: bool,
    trigger: &str,
    ai: &AiRunOptions,
) -> Result<i32, String> {
    let definition = load_definition(workspace)?;
    let resolved_inputs = load_inputs(run_dir, saved_inputs);
    let plan = build_plan(workspace, run_dir, &resolved_inputs, allow_disruptive);
    if !plan.available {
        return Err(plan.error);
    }
    // Checked once for the whole run, not per suite: every suite that needs
    // AI test data shares the same provider pick and usage snapshot.
    let any_suite_needs_ai = definition
        .suites
        .iter()
        .any(|suite| !suite.requirements.ai_test_data.is_empty());
    let capability = if any_suite_needs_ai && ai.enabled {
        Some(check_ai_capability(
            &ai.python_bin,
            &ai.script_dir,
            &ai.providers,
            ai.minimum_remaining_percent,
        ))
    } else {
        None
    };
    let results_path = run_dir.join("test-results.json");
    let mut results = TestRunResults {
        schema_version: 1,
        repository: repository.into(),
        definition_path: definition_path(workspace).to_string_lossy().into_owned(),
        started_at: unix_timestamp(),
        finished_at: None,
        trigger: trigger.to_string(),
        suites: plan
            .suites
            .iter()
            .enumerate()
            .map(|(index, suite)| {
                let mut result = suite.result.clone();
                // A new cycle always retries every eligible suite. Historical
                // terminal states are for display only and must never turn a
                // previous pass/failure into an implicit skip.
                if !result.blocked {
                    result.state = "Ready".into();
                    result.detail.clear();
                    result.exit_code = None;
                    result.started_at = None;
                    result.finished_at = None;
                    result.duration_ms = None;
                    result.output.clear();
                    result.ai_generated_data.clear();
                    if !definition.suites[index]
                        .requirements
                        .ai_test_data
                        .is_empty()
                    {
                        if !ai.enabled {
                            result.state = "Not executed".into();
                            result.blocked = true;
                            result.detail = "Not executed - reason: AI test-data generation is \
                                turned off for this repository"
                                .into();
                        } else if let Some(capability) = &capability {
                            if !capability.available {
                                result.state = "Not executed".into();
                                result.blocked = true;
                                result.detail = format!(
                                    "Not executed - reason: AI required and usage exhausted ({})",
                                    capability.detail
                                );
                            }
                        }
                    }
                }
                result
            })
            .collect(),
    };
    write_results(&results_path, &results)?;
    let mut any_failure = false;
    for (index, suite) in definition.suites.iter().enumerate() {
        if results.suites[index].state != "Ready" {
            continue;
        }
        let started_at = unix_timestamp();
        results.suites[index].state = "Running".into();
        results.suites[index].started_at = Some(started_at);
        results.suites[index].detail.clear();
        write_results(&results_path, &results)?;
        if !suite.requirements.ai_test_data.is_empty() {
            // Gated above: reaching this point means `ai.enabled` and a
            // capability with headroom were both confirmed for this run.
            let capability = capability.as_ref().expect("gated above");
            match generate_suite_ai_test_data(run_dir, suite, ai, capability) {
                Ok((_dir, records)) => results.suites[index].ai_generated_data = records,
                Err(error) => {
                    results.suites[index].state = "Failed".into();
                    results.suites[index].detail =
                        format!("AI test-data generation failed: {error}");
                    results.suites[index].finished_at = Some(unix_timestamp());
                    write_results(&results_path, &results)?;
                    any_failure = true;
                    continue;
                }
            }
        }
        let ai_data_dir = if suite.requirements.ai_test_data.is_empty() {
            None
        } else {
            Some(run_dir.join("ai-data").join(&suite.id))
        };
        let outcome = run_suite(
            workspace,
            run_dir,
            suite,
            &plan.selected_device,
            ai_data_dir.as_deref(),
        )
        .unwrap_or_else(|error| CommandOutcome {
            exit_code: Some(127),
            duration_ms: 0,
            detail: error,
            output: String::new(),
        });
        results.suites[index].state = if outcome.exit_code == Some(0) {
            "Passed".into()
        } else {
            any_failure = true;
            "Failed".into()
        };
        results.suites[index].exit_code = outcome.exit_code;
        results.suites[index].finished_at = Some(unix_timestamp());
        results.suites[index].duration_ms = Some(outcome.duration_ms);
        results.suites[index].detail = outcome.detail;
        results.suites[index].output = outcome.output;
        write_results(&results_path, &results)?;
    }
    results.finished_at = Some(unix_timestamp());
    write_results(&results_path, &results)?;
    append_history(run_dir, &results);
    if let Some(reporting) = definition.reporting.filter(|item| !item.command.is_empty()) {
        let _ = run_auxiliary_command(
            workspace,
            &reporting.command,
            &results_path,
            reporting.timeout_seconds,
        );
    }
    if any_failure && triage_enabled {
        if let Some(triage) = definition
            .failure_triage
            .filter(|item| !item.command.is_empty())
        {
            let _ = run_auxiliary_command(
                workspace,
                &triage.command,
                &results_path,
                triage.timeout_seconds,
            );
        }
    }
    Ok(if any_failure { 1 } else { 0 })
}

struct CommandOutcome {
    exit_code: Option<i32>,
    duration_ms: u64,
    detail: String,
    output: String,
}

fn run_suite(
    workspace: &Path,
    run_dir: &Path,
    suite: &TestSuiteDefinition,
    selected_device: &str,
    ai_data_dir: Option<&Path>,
) -> Result<CommandOutcome, String> {
    fs::create_dir_all(run_dir).map_err(|error| error.to_string())?;
    let log_path = run_dir.join(format!("{}.log", suite.id));
    let stdout = File::create(&log_path).map_err(|error| error.to_string())?;
    let stderr = stdout.try_clone().map_err(|error| error.to_string())?;
    let started = Instant::now();
    let mut command = Command::new(&suite.command[0]);
    command
        .args(&suite.command[1..])
        .current_dir(workspace)
        .env("PATH", tools::enhanced_path())
        .env("SWARM_FIRE_TV_SERIAL", selected_device)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    if let Some(dir) = ai_data_dir {
        command.env("SWARM_AI_TEST_DATA_DIR", dir);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start suite '{}': {error}", suite.name))?;
    let deadline = Instant::now() + Duration::from_secs(suite.timeout_seconds);
    let (exit_code, detail) = loop {
        if let Some(status) = child.try_wait().map_err(|error| error.to_string())? {
            break (
                status.code(),
                format!(
                    "Exited with status {}",
                    status
                        .code()
                        .map_or_else(|| "signal".into(), |code| code.to_string())
                ),
            );
        }
        if Instant::now() >= deadline {
            #[cfg(unix)]
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            #[cfg(not(unix))]
            let _ = child.kill();
            let _ = child.wait();
            break (
                None,
                format!("Timed out after {} seconds", suite.timeout_seconds),
            );
        }
        thread::sleep(Duration::from_millis(100));
    };
    let mut output = String::new();
    if let Ok(mut file) = File::open(&log_path) {
        let _ = file.read_to_string(&mut output);
        if output.len() > 64 * 1024 {
            let mut start = output.len() - 64 * 1024;
            while !output.is_char_boundary(start) {
                start += 1;
            }
            output = output.split_off(start);
        }
    }
    Ok(CommandOutcome {
        exit_code,
        duration_ms: started.elapsed().as_millis() as u64,
        detail,
        output,
    })
}

fn run_auxiliary_command(
    workspace: &Path,
    command: &[String],
    results_path: &Path,
    timeout_seconds: u64,
) -> bool {
    let mut process = Command::new(&command[0]);
    process
        .args(&command[1..])
        .current_dir(workspace)
        .env("PATH", tools::enhanced_path())
        .env("SWARM_TEST_RESULTS", results_path)
        .stdin(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        process.process_group(0);
    }
    let Ok(mut child) = process.spawn() else {
        return false;
    };
    let deadline = Instant::now() + Duration::from_secs(timeout_seconds.max(1));
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Err(_) => return false,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(100)),
            Ok(None) => {
                #[cfg(unix)]
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                #[cfg(not(unix))]
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
    }
}

pub fn run_cli(arguments: &[String]) -> Option<i32> {
    let marker = arguments
        .iter()
        .position(|argument| argument == "--swarm-test-runner")?;
    let value = |flag: &str| {
        arguments[marker + 1..]
            .windows(2)
            .find(|pair| pair[0] == flag)
            .map(|pair| pair[1].clone())
    };
    let workspace = PathBuf::from(value("--workspace").unwrap_or_default());
    let run_dir = PathBuf::from(value("--run-dir").unwrap_or_default());
    let repository = value("--repository").unwrap_or_default();
    let selected = value("--device").unwrap_or_default();
    let allow_disruptive = arguments
        .iter()
        .any(|argument| argument == "--allow-disruptive");
    let triage_enabled = arguments.iter().any(|argument| argument == "--triage");
    let once = arguments.iter().any(|argument| argument == "--once");
    let hour = value("--hour")
        .and_then(|value| value.parse::<u8>().ok())
        .unwrap_or(3);
    let inputs = if selected.is_empty() {
        HashMap::new()
    } else {
        HashMap::from([("fireTvSerial".into(), selected)])
    };
    let trigger = if once { "manual" } else { "scheduled" };
    let enabled_providers: Vec<String> = arguments[marker + 1..]
        .windows(2)
        .filter(|pair| pair[0] == "--enabled-provider")
        .map(|pair| pair[1].clone())
        .collect();
    let ai = AiRunOptions {
        enabled: arguments
            .iter()
            .any(|argument| argument == "--ai-test-data-enabled"),
        python_bin: value("--python-bin").unwrap_or_else(|| "python3".into()),
        script_dir: PathBuf::from(value("--script-dir").unwrap_or_default()),
        minimum_remaining_percent: value("--minimum-remaining-percent")
            .and_then(|value| value.parse::<u8>().ok())
            .unwrap_or(10),
        providers: crate::config::KNOWN_PROVIDERS
            .iter()
            .map(|id| AiProviderOption {
                id: (*id).to_string(),
                bin: value(&format!("--{id}-bin")).unwrap_or_default(),
                model: value(&format!("--{id}-model")).unwrap_or_default(),
                enabled: enabled_providers.iter().any(|enabled| enabled == id),
            })
            .collect(),
    };
    loop {
        let inputs_before_run = input_signature(&run_dir);
        match run_once(
            &workspace,
            &run_dir,
            &repository,
            &inputs,
            allow_disruptive,
            triage_enabled,
            trigger,
            &ai,
        ) {
            Ok(code) if once => return Some(code),
            Err(error) if once => {
                eprintln!("{error}");
                return Some(2);
            }
            Ok(_) => {}
            Err(error) => eprintln!("{error}"),
        }
        let deadline = Instant::now() + Duration::from_secs(seconds_until_hour(hour));
        while Instant::now() < deadline {
            // Saving a UI selection wakes the scheduler so newly eligible
            // suites retry immediately instead of waiting until tomorrow.
            if input_signature(&run_dir) != inputs_before_run {
                break;
            }
            thread::sleep(Duration::from_secs(2));
        }
    }
}

fn seconds_until_hour(hour: u8) -> u64 {
    #[cfg(unix)]
    unsafe {
        let now = libc::time(std::ptr::null_mut());
        let mut local: libc::tm = std::mem::zeroed();
        libc::localtime_r(&now, &mut local);
        let elapsed =
            (local.tm_hour as i64 * 3600) + (local.tm_min as i64 * 60) + local.tm_sec as i64;
        let target = hour as i64 * 3600;
        ((target - elapsed + 86_400) % 86_400).max(60) as u64
    }
    #[cfg(not(unix))]
    {
        let _ = hour;
        86_400
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn detects_conventional_manifests_and_classifies_hardware_scripts() {
        let workspace = tempdir().unwrap();
        fs::write(
            workspace.path().join("Cargo.toml"),
            "[workspace]\nmembers = []\n",
        )
        .unwrap();
        fs::create_dir_all(workspace.path().join("clients/tv-android")).unwrap();
        fs::write(
            workspace.path().join("clients/tv-android/gradlew"),
            "#!/bin/sh\n",
        )
        .unwrap();
        fs::create_dir_all(workspace.path().join("scripts/tests")).unwrap();
        fs::write(
            workspace
                .path()
                .join("scripts/tests/tv_uat_resilience_suite.sh"),
            "#!/bin/sh\n",
        )
        .unwrap();

        let draft = detect_definition(workspace.path(), None).unwrap();
        let definition: TestDefinition = serde_json::from_str(&draft.definition).unwrap();
        assert_eq!(draft.detected_suites, 3);
        assert_eq!(
            definition.suites[0].command,
            ["cargo", "test", "--workspace"]
        );
        assert!(definition.suites.iter().any(|suite| suite.id == "android"));
        let tv = definition
            .suites
            .iter()
            .find(|suite| suite.id == "tv-uat-resilience-suite")
            .unwrap();
        assert!(tv.disruptive);
        assert_eq!(tv.requirements.devices[0].device_type, "fireTv");
        assert!(tv.requirements.executables.contains(&"adb".to_string()));
    }

    #[test]
    fn detection_returns_an_editable_disabled_placeholder_when_no_tests_are_found() {
        let workspace = tempdir().unwrap();
        let draft = detect_definition(workspace.path(), None).unwrap();
        let definition: TestDefinition = serde_json::from_str(&draft.definition).unwrap();
        assert_eq!(draft.detected_suites, 0);
        assert_eq!(definition.suites.len(), 1);
        assert!(!definition.suites[0].enabled);
    }

    #[test]
    fn detection_skips_ai_discovery_and_notes_why_when_usage_is_exhausted() {
        let workspace = tempdir().unwrap();
        let script_dir = tempdir().unwrap();
        write_stub_ai_assist(script_dir.path(), StubAiAssist::CapacityUnavailable);
        let ai = ai_options_with_stub(script_dir.path(), "claude");
        let draft = detect_definition(workspace.path(), Some(&ai)).unwrap();
        let definition: TestDefinition = serde_json::from_str(&draft.definition).unwrap();
        assert_eq!(definition.suites.len(), 1);
        assert!(!definition.suites[0].enabled);
        assert!(draft
            .notes
            .iter()
            .any(|note| note.contains("AI-assisted discovery was skipped")));
    }

    #[test]
    fn detection_adds_disabled_ai_suggested_suites_when_capacity_allows() {
        let workspace = tempdir().unwrap();
        let script_dir = tempdir().unwrap();
        write_stub_ai_assist(script_dir.path(), StubAiAssist::DiscoverOneSuite);
        let ai = ai_options_with_stub(script_dir.path(), "claude");
        let draft = detect_definition(workspace.path(), Some(&ai)).unwrap();
        let definition: TestDefinition = serde_json::from_str(&draft.definition).unwrap();
        assert_eq!(draft.detected_suites, 1);
        assert_eq!(definition.suites.len(), 1);
        assert!(!definition.suites[0].enabled);
        assert_eq!(definition.suites[0].command, ["make", "test"]);
    }

    #[test]
    fn create_definition_validates_and_never_overwrites_an_existing_file() {
        let workspace = tempdir().unwrap();
        let raw = r#"{"version":1,"suites":[{"id":"unit","name":"Unit","command":["true"]}]}"#;
        let path = create_definition(workspace.path(), raw).unwrap();
        assert_eq!(path, definition_path(workspace.path()));
        assert!(load_definition(workspace.path()).is_ok());

        let error = create_definition(workspace.path(), raw).unwrap_err();
        assert!(error.contains("already exists"));
        let invalid_workspace = tempdir().unwrap();
        let error = create_definition(invalid_workspace.path(), "not json").unwrap_err();
        assert!(error.contains("not valid JSON"));
        assert!(!definition_path(invalid_workspace.path()).exists());
    }

    #[test]
    fn parses_adb_devices_and_marks_only_authorized_devices_eligible() {
        let devices = parse_adb_devices(
            "List of devices attached\n192.0.2.1:5555 device product:b device:c\nABC unauthorized usb:1\n\n",
        );
        assert_eq!(devices.len(), 2);
        assert!(devices[0].eligible);
        assert!(!devices[1].eligible);
    }

    #[test]
    fn saved_device_wins_and_a_single_device_is_selected_automatically() {
        let mut devices = parse_adb_devices("List of devices attached\none device\ntwo device\n");
        assert_eq!(select_device(&mut devices, "two"), ("two".into(), false));
        let mut one = parse_adb_devices("List of devices attached\nonly device\n");
        assert_eq!(select_device(&mut one, ""), ("only".into(), false));
    }

    #[test]
    fn unrelated_ready_suite_is_not_blocked_by_missing_hardware() {
        let workspace = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"unit","name":"Unit","command":["true"]},{"id":"tv","name":"TV","command":["true"],"requirements":{"files":["connected-fire-tv.marker"],"devices":[{"type":"fireTv"}]}}]}"#,
        )
        .unwrap();
        let plan = build_plan(
            workspace.path(),
            &workspace.path().join("run"),
            &HashMap::new(),
            false,
        );
        assert_eq!(plan.suites[0].result.state, "Ready");
        assert_eq!(plan.suites[1].result.state, "Skipped");
        assert!(plan.suites[1].result.blocked);
    }

    #[test]
    fn runner_continues_after_a_failure_and_writes_structured_results() {
        let workspace = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"bad","name":"Bad","command":["/usr/bin/false"],"timeoutSeconds":2},{"id":"good","name":"Good","command":["/usr/bin/true"],"timeoutSeconds":2}]}"#,
        )
        .unwrap();
        let run_dir = workspace.path().join("run");
        assert_eq!(
            run_once(
                workspace.path(),
                &run_dir,
                "owner/repo",
                &HashMap::new(),
                false,
                false,
                "manual",
                &ai_disabled(),
            )
            .unwrap(),
            1
        );
        let results = read_results(&run_dir.join("test-results.json")).unwrap();
        assert_eq!(results.suites[0].state, "Failed");
        assert_eq!(results.suites[1].state, "Passed");
        let history = list_runs(&run_dir);
        assert_eq!(history.len(), 1);
        assert_eq!(history[0].trigger, "manual");
        assert_eq!(history[0].suites[1].state, "Passed");
    }

    #[test]
    fn saved_ui_inputs_override_the_runner_startup_snapshot() {
        let directory = tempdir().unwrap();
        let fallback = HashMap::from([("fireTvSerial".into(), "old".into())]);
        let saved = HashMap::from([("fireTvSerial".into(), "new".into())]);
        save_inputs(directory.path(), &saved).unwrap();
        assert_eq!(
            load_inputs(directory.path(), &fallback).get("fireTvSerial"),
            Some(&"new".to_string())
        );
    }

    #[test]
    fn failure_triage_is_not_invoked_when_the_optional_feature_is_disabled() {
        let workspace = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        let marker = workspace.path().join("triage-ran");
        let definition = serde_json::json!({
            "version": 1,
            "suites": [{
                "id": "failure",
                "name": "Failure",
                "command": ["/usr/bin/false"]
            }],
            "failureTriage": {
                "command": ["/usr/bin/touch", marker]
            }
        });
        fs::write(
            definition_path(workspace.path()),
            serde_json::to_vec(&definition).unwrap(),
        )
        .unwrap();
        assert_eq!(
            run_once(
                workspace.path(),
                &workspace.path().join("run"),
                "owner/repo",
                &HashMap::new(),
                false,
                false,
                "scheduled",
                &ai_disabled(),
            )
            .unwrap(),
            1
        );
        assert!(!marker.exists());
    }

    fn ai_disabled() -> AiRunOptions {
        AiRunOptions {
            enabled: false,
            python_bin: "python3".into(),
            script_dir: PathBuf::new(),
            providers: Vec::new(),
            minimum_remaining_percent: 10,
        }
    }

    enum StubAiAssist {
        CapacityUnavailable,
        DiscoverOneSuite,
    }

    /// Writes a fake `ai_test_assist.py` that answers with a fixed, canned
    /// response for whichever subcommand it's called with — standing in for
    /// the real helper (which needs a real, signed-in AI CLI) so the Rust
    /// wiring around it — gating, file writes, run results — can be tested
    /// deterministically and offline.
    fn write_stub_ai_assist(script_dir: &Path, stub: StubAiAssist) {
        // Every real call opens with a `capacity` probe before doing
        // anything else, so each stub answers both subcommands it needs.
        let body = match stub {
            StubAiAssist::CapacityUnavailable => {
                r#"print('{"available": false, "provider": null, "detail": "session 2% remaining"}')"#
                    .to_string()
            }
            StubAiAssist::DiscoverOneSuite => {
                "if sys.argv[1] == 'capacity':\n\
                 \tprint('{\"available\": true, \"provider\": \"claude\", \"detail\": \"99% remaining\"}')\n\
                 elif sys.argv[1] == 'discover':\n\
                 \tprint('{\"ok\": true, \"suites\": [{\"id\": \"make\", \"name\": \"Make tests\", \
                 \"command\": [\"make\", \"test\"]}], \"notes\": []}')\n"
                    .to_string()
            }
        };
        fs::write(
            script_dir.join(AI_TEST_ASSIST_SCRIPT),
            format!("import sys\n{body}\n"),
        )
        .unwrap();
    }

    fn ai_options_with_stub(script_dir: &Path, provider_id: &str) -> AiRunOptions {
        AiRunOptions {
            enabled: true,
            python_bin: "python3".into(),
            script_dir: script_dir.to_path_buf(),
            providers: vec![AiProviderOption {
                id: provider_id.into(),
                bin: "stub-bin".into(),
                model: String::new(),
                enabled: true,
            }],
            minimum_remaining_percent: 10,
        }
    }

    #[test]
    fn ai_test_data_suite_is_not_executed_when_the_repository_switch_is_off() {
        let workspace = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"needs-ai","name":"Needs AI","command":["/usr/bin/true"],
                "requirements":{"aiTestData":[{"name":"sample","prompt":"a sample"}]}}]}"#,
        )
        .unwrap();
        let run_dir = workspace.path().join("run");
        run_once(
            workspace.path(),
            &run_dir,
            "owner/repo",
            &HashMap::new(),
            false,
            false,
            "manual",
            &ai_disabled(),
        )
        .unwrap();
        let results = read_results(&run_dir.join("test-results.json")).unwrap();
        assert_eq!(results.suites[0].state, "Not executed");
        assert!(results.suites[0].detail.contains("turned off"));
    }

    #[test]
    fn ai_test_data_suite_is_not_executed_when_usage_is_exhausted() {
        let workspace = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"needs-ai","name":"Needs AI","command":["/usr/bin/true"],
                "requirements":{"aiTestData":[{"name":"sample","prompt":"a sample"}]}}]}"#,
        )
        .unwrap();
        let script_dir = tempdir().unwrap();
        write_stub_ai_assist(script_dir.path(), StubAiAssist::CapacityUnavailable);
        let ai = ai_options_with_stub(script_dir.path(), "claude");
        let run_dir = workspace.path().join("run");
        run_once(
            workspace.path(),
            &run_dir,
            "owner/repo",
            &HashMap::new(),
            false,
            false,
            "manual",
            &ai,
        )
        .unwrap();
        let results = read_results(&run_dir.join("test-results.json")).unwrap();
        assert_eq!(results.suites[0].state, "Not executed");
        assert!(results.suites[0]
            .detail
            .contains("AI required and usage exhausted"));
        assert!(results.suites[0].detail.contains("2% remaining"));
    }

    #[test]
    fn ai_test_data_suite_runs_and_records_generated_data_when_capacity_allows() {
        let workspace = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"needs-ai","name":"Needs AI",
                "command":["/bin/sh", "-c", "test -f \"$SWARM_AI_TEST_DATA_DIR/sample.txt\""],
                "requirements":{"aiTestData":[{"name":"sample","prompt":"a sample"}]}}]}"#,
        )
        .unwrap();
        let script_dir = tempdir().unwrap();
        // This run needs both `capacity` and `generate` answered distinctly,
        // unlike the canned single-answer stubs above.
        fs::write(
            script_dir.path().join(AI_TEST_ASSIST_SCRIPT),
            "import sys\n\
             if sys.argv[1] == 'capacity':\n\
             \tprint('{\"available\": true, \"provider\": \"claude\", \"detail\": \"99% remaining\"}')\n\
             elif sys.argv[1] == 'generate':\n\
             \tprint('{\"ok\": true, \"text\": \"sample data\"}')\n",
        )
        .unwrap();
        let ai = ai_options_with_stub(script_dir.path(), "claude");
        let run_dir = workspace.path().join("run");
        run_once(
            workspace.path(),
            &run_dir,
            "owner/repo",
            &HashMap::new(),
            false,
            false,
            "manual",
            &ai,
        )
        .unwrap();
        let results = read_results(&run_dir.join("test-results.json")).unwrap();
        assert_eq!(results.suites[0].state, "Passed");
        assert_eq!(results.suites[0].ai_generated_data.len(), 1);
        assert_eq!(results.suites[0].ai_generated_data[0].provider, "claude");
        assert_eq!(
            results.suites[0].ai_generated_data[0].summary,
            "sample data"
        );
    }
}
