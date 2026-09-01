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

#[derive(Debug, Clone, Deserialize)]
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

#[derive(Debug, Clone, Deserialize)]
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

#[derive(Debug, Clone, Default, Deserialize)]
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
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ServerRequirement {
    pub name: String,
    pub host: String,
    pub port: u16,
    #[serde(default = "default_health_timeout")]
    pub timeout_seconds: u64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MountRequirement {
    pub name: String,
    pub path: String,
    #[serde(default)]
    pub kind: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CredentialRequirement {
    pub name: String,
    #[serde(default)]
    pub environment: String,
    #[serde(default)]
    pub file: String,
}

#[derive(Debug, Clone, Deserialize)]
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

#[derive(Debug, Clone, Deserialize)]
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
    runs.sort_by(|a, b| b.started_at.cmp(&a.started_at));
    runs.truncate(HISTORY_LIMIT);
    runs
}

pub fn run_once(
    workspace: &Path,
    run_dir: &Path,
    repository: &str,
    saved_inputs: &HashMap<String, String>,
    allow_disruptive: bool,
    triage_enabled: bool,
    trigger: &str,
) -> Result<i32, String> {
    let definition = load_definition(workspace)?;
    let resolved_inputs = load_inputs(run_dir, saved_inputs);
    let plan = build_plan(workspace, run_dir, &resolved_inputs, allow_disruptive);
    if !plan.available {
        return Err(plan.error);
    }
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
            .map(|suite| {
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
        let outcome =
            run_suite(workspace, run_dir, suite, &plan.selected_device).unwrap_or_else(|error| {
                CommandOutcome {
                    exit_code: Some(127),
                    duration_ms: 0,
                    detail: error,
                    output: String::new(),
                }
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
            r#"{"version":1,"suites":[{"id":"unit","name":"Unit","command":["true"]},{"id":"tv","name":"TV","command":["true"],"requirements":{"devices":[{"type":"fireTv"}]}}]}"#,
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
            )
            .unwrap(),
            1
        );
        assert!(!marker.exists());
    }
}
