use crate::test_discovery;
use crate::tools;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const TEST_DEFINITION_PATH: &str = ".swarm/tests.json";
const TEST_INPUTS_FILE: &str = "test-inputs.json";
const OUTPUT_PREVIEW_BYTES: usize = 16 * 1024;
const CHILD_NOFILE_LIMIT: u64 = 8192;
const CHECKOUT_LOCK_FILE: &str = "swarm-test-run.lock";

fn default_version() -> u32 {
    1
}

pub(crate) fn default_true() -> bool {
    true
}

pub(crate) fn default_timeout() -> u64 {
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
    /// Persist the autonomous framework choice through definition round trips.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub adversarial_bootstrap: Option<serde_json::Value>,
    #[serde(default = "default_version")]
    pub version: u32,
    #[serde(default)]
    pub suites: Vec<TestSuiteDefinition>,
    /// Reviewed explanation of coverage relationships and intentionally
    /// omitted aggregate/alias commands.
    #[serde(default)]
    pub coverage_notes: Vec<String>,
    #[serde(default)]
    pub reporting: Option<ReportingDefinition>,
    #[serde(default)]
    pub failure_triage: Option<ReportingDefinition>,
    /// Schema v2 user-supplied values. V1 definitions deserialize with an
    /// empty list and retain their legacy requirements/device behavior.
    #[serde(default)]
    pub inputs: Vec<TestInputDefinition>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TestInputDefinition {
    pub id: String,
    pub label: String,
    #[serde(default)]
    pub help: String,
    #[serde(rename = "type")]
    pub input_type: String,
    #[serde(default)]
    pub required: bool,
    #[serde(default)]
    pub default: serde_json::Value,
    #[serde(default)]
    pub validation: InputValidation,
    #[serde(default)]
    pub discovery: Option<InputDiscovery>,
    /// Empty means every suite consumes the input.
    #[serde(default)]
    pub suites: Vec<String>,
    #[serde(default = "default_input_persistence")]
    pub persistence: String,
    #[serde(default)]
    pub binding: InputBinding,
    #[serde(default)]
    pub options: Vec<InputOption>,
}

fn default_input_persistence() -> String {
    "repository".into()
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InputValidation {
    #[serde(default)]
    pub pattern: String,
    #[serde(default)]
    pub min: Option<f64>,
    #[serde(default)]
    pub max: Option<f64>,
    #[serde(default)]
    pub min_length: Option<usize>,
    #[serde(default)]
    pub max_length: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum InputDiscovery {
    Kind(String),
    Detailed {
        kind: String,
        #[serde(default)]
        environment: String,
    },
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InputBinding {
    #[serde(default)]
    pub environment: String,
    /// Each entry is appended as one argv element. `{value}` is replaced
    /// without tokenization; for booleans the entries are present only when true.
    #[serde(default, alias = "argv")]
    pub arguments: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum InputOption {
    Value(String),
    Labeled { value: String, label: String },
}

impl InputOption {
    fn value(&self) -> &str {
        match self {
            Self::Value(v) => v,
            Self::Labeled { value, .. } => value,
        }
    }
    fn label(&self) -> &str {
        match self {
            Self::Value(v) => v,
            Self::Labeled { label, .. } => label,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TestSuiteDefinition {
    #[serde(default)]
    pub origin: String,
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
    /// Other candidate paths or plain-language descriptions of assertions
    /// this suite's command already exercises, so the coverage audit can
    /// bucket a matching detected candidate as "covered" instead of
    /// "unmapped" without scheduling it separately.
    #[serde(default)]
    pub covers: Vec<String>,
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
    pub paths: Vec<PathRequirement>,
    #[serde(default)]
    pub android_sdk: Option<AndroidSdkRequirement>,
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
    #[serde(default)]
    pub host: String,
    #[serde(default)]
    pub port: u16,
    /// Optional HTTP health endpoint. When set, a successful 2xx/3xx HTTP
    /// response is required; merely accepting a TCP connection is not enough.
    #[serde(default)]
    pub url: String,
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
pub struct PathRequirement {
    pub name: String,
    pub path: String,
    /// `file`, `directory`, or `any` (the default).
    #[serde(default)]
    pub kind: String,
    /// Open/read the path during preflight instead of accepting metadata alone.
    #[serde(default = "default_true")]
    pub readable: bool,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AndroidSdkRequirement {
    /// Gradle project containing an optional `local.properties` sdk.dir entry.
    #[serde(default)]
    pub project: String,
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
    /// Argument understood by the suite script. The scheduler appends this
    /// and the selected serial as two distinct argv entries.
    #[serde(default = "default_device_argument")]
    pub argument: String,
}

fn default_device_input() -> String {
    "fireTvSerial".into()
}

fn default_device_argument() -> String {
    "--device".into()
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
    #[serde(default)]
    pub origin: String,
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
    /// Full log retained on disk; `output` is only a bounded preview.
    #[serde(default)]
    pub log_path: String,
    /// What an AI provider generated for this suite's `ai_test_data`
    /// requirements, if any — the run's own record of what was made up, per
    /// the repository's test definition.
    #[serde(default)]
    pub ai_generated_data: Vec<AiGeneratedDataRecord>,
    /// Exact direct-exec argv, with secret values replaced by `<redacted>`.
    #[serde(default)]
    pub argv: Vec<String>,
    /// Names only; environment values are deliberately not retained.
    #[serde(default)]
    pub environment: Vec<String>,
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
    pub argv: Vec<String>,
    pub environment: Vec<String>,
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
    pub inputs: Vec<ResolvedInput>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ResolvedInput {
    pub id: String,
    pub label: String,
    pub help: String,
    pub input_type: String,
    pub required: bool,
    pub value: String,
    pub has_value: bool,
    pub valid: bool,
    pub state: String,
    pub message: String,
    pub provenance: String,
    pub persistence: String,
    pub suites: Vec<String>,
    pub options: Vec<ResolvedInputOption>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ResolvedInputOption {
    pub value: String,
    pub label: String,
    pub detected: bool,
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
    /// Immutable Git commit captured before any suite starts.
    #[serde(default)]
    pub tested_commit: String,
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
        origin: String::new(),
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
        covers: Vec::new(),
    }
}

pub(crate) fn suite_id(value: &str) -> String {
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

pub(crate) fn suite_name(value: &str) -> String {
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
///
/// Candidates are found recursively (see `test_discovery::discover_candidates`)
/// across nested manifests, CI workflows, task runners, and conventional
/// test-script directories, then classified. Only candidates classified as
/// `atomic` become schedulable suites here; aggregates, aliases, helpers, and
/// unknown/low-confidence candidates are surfaced as notes instead, so an
/// aggregate like `full_uat_suite.sh` and the atomic commands it wraps are
/// never scheduled together.
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

    let candidates = test_discovery::discover_candidates(workspace)?;
    let mut suites = Vec::new();
    let mut notes = Vec::new();

    for candidate in candidates.iter().filter(|c| c.classification == "atomic") {
        let covers = candidate
            .covers
            .iter()
            .filter(|path| candidates.iter().any(|other| &other.path == *path))
            .cloned()
            .collect::<Vec<_>>();
        suites.push(TestSuiteDefinition {
            origin: String::new(),
            id: candidate.id.clone(),
            name: candidate.name.clone(),
            command: candidate.command.clone(),
            timeout_seconds: candidate.timeout_seconds.unwrap_or_else(default_timeout),
            disruptive: candidate.disruptive,
            enabled: candidate.confidence != "low",
            requirements: candidate.requirements.clone(),
            covers,
        });
    }
    for candidate in candidates.iter().filter(|c| c.classification != "atomic") {
        notes.push(format!(
            "{} — {} ({}, {} confidence): {}",
            if candidate.path.is_empty() {
                candidate.name.as_str()
            } else {
                candidate.path.as_str()
            },
            candidate.name,
            candidate.classification,
            candidate.confidence,
            candidate.detail,
        ));
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
        adversarial_bootstrap: None,
        version: 1,
        suites,
        coverage_notes: Vec::new(),
        reporting: None,
        failure_triage: None,
        inputs: Vec::new(),
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
    if !matches!(definition.version, 1 | 2) {
        return Err(format!(
            "Unsupported test definition version {}; expected 1 or 2",
            definition.version
        ));
    }
    if definition.version == 1 && !definition.inputs.is_empty() {
        return Err("Test inputs require schema version 2".into());
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
            if device.argument.trim().is_empty() || !device.argument.starts_with('-') {
                return Err(format!(
                    "Device argument in suite '{}' must be a non-empty option such as --device",
                    suite.id
                ));
            }
        }
        if let Some(android) = &suite.requirements.android_sdk {
            if Path::new(&android.project).is_absolute() {
                return Err(format!(
                    "Android SDK project in suite '{}' must be relative to the repository",
                    suite.id
                ));
            }
        }
        for path in &suite.requirements.paths {
            if path.name.trim().is_empty() || path.path.trim().is_empty() {
                return Err(format!(
                    "Path requirements in suite '{}' need a name and path",
                    suite.id
                ));
            }
            if !matches!(path.kind.as_str(), "" | "any" | "file" | "directory") {
                return Err(format!(
                    "Path '{}' in suite '{}' has unsupported kind '{}'",
                    path.name, suite.id, path.kind
                ));
            }
        }
        for server in &suite.requirements.servers {
            if server.url.is_empty() && (server.host.is_empty() || server.port == 0) {
                return Err(format!(
                    "Server '{}' in suite '{}' needs either url or host and port",
                    server.name, suite.id
                ));
            }
            if !server.url.is_empty() && !server.url.starts_with("http://") {
                return Err(format!(
                    "Server '{}' in suite '{}' uses an unsupported health URL; only http:// is supported",
                    server.name, suite.id
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
    let suite_ids = definition
        .suites
        .iter()
        .map(|s| s.id.as_str())
        .collect::<std::collections::HashSet<_>>();
    let mut input_ids = std::collections::HashSet::new();
    for input in &definition.inputs {
        if input.id.is_empty()
            || !input
                .id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
        {
            return Err("Input ids may contain only letters, digits, '-' and '_'".into());
        }
        if !input_ids.insert(input.id.as_str()) {
            return Err(format!("Duplicate input id '{}'", input.id));
        }
        if input.label.trim().is_empty() {
            return Err(format!("Input '{}' needs a label", input.id));
        }
        if !matches!(
            input.input_type.as_str(),
            "text"
                | "number"
                | "boolean"
                | "file"
                | "directory"
                | "select"
                | "device"
                | "environment"
                | "secret"
        ) {
            return Err(format!(
                "Input '{}' has unsupported type '{}'",
                input.id, input.input_type
            ));
        }
        if !matches!(
            input.persistence.as_str(),
            "repository" | "session-only" | "keychain" | "os-keychain" | "osKeychain"
        ) {
            return Err(format!(
                "Input '{}' has unsupported persistence policy '{}'",
                input.id, input.persistence
            ));
        }
        let keychain_persistence = matches!(
            input.persistence.as_str(),
            "keychain" | "os-keychain" | "osKeychain"
        );
        if input.input_type == "secret" && !keychain_persistence {
            return Err(format!(
                "Secret input '{}' must use keychain persistence",
                input.id
            ));
        }
        if input.input_type == "secret" && !input.default.is_null() {
            return Err(format!(
                "Secret input '{}' cannot declare a default value",
                input.id
            ));
        }
        if input.input_type != "secret" && keychain_persistence {
            return Err(format!(
                "Only secret inputs may use keychain persistence ('{}')",
                input.id
            ));
        }
        if input.binding.environment.is_empty() && input.binding.arguments.is_empty() {
            return Err(format!(
                "Input '{}' needs an environment or argument binding",
                input.id
            ));
        }
        if !input.binding.environment.is_empty()
            && !input
                .binding
                .environment
                .chars()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
        {
            return Err(format!(
                "Input '{}' has an invalid environment variable name",
                input.id
            ));
        }
        if input.input_type != "boolean"
            && input
                .binding
                .arguments
                .iter()
                .all(|a| !a.contains("{value}"))
            && !input.binding.arguments.is_empty()
        {
            return Err(format!(
                "Input '{}' argument binding must include {{value}}",
                input.id
            ));
        }
        if input
            .suites
            .iter()
            .any(|id| !suite_ids.contains(id.as_str()))
        {
            return Err(format!("Input '{}' references an unknown suite", input.id));
        }
        if input.input_type == "select" && input.options.is_empty() && input.discovery.is_none() {
            return Err(format!(
                "Select input '{}' needs options or discovery",
                input.id
            ));
        }
        if !input.validation.pattern.is_empty() {
            regex::Regex::new(&input.validation.pattern).map_err(|e| {
                format!(
                    "Input '{}' has an invalid validation pattern: {e}",
                    input.id
                )
            })?;
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

const KEYCHAIN_SERVICE: &str = "com.swarm-media-streaming.swarm-automation.test-input";

fn keychain_entry(repo_id: &str, key: &str) -> Result<keyring::Entry, String> {
    keyring::Entry::new(KEYCHAIN_SERVICE, &format!("{repo_id}:{key}"))
        .map_err(|error| format!("Could not access the OS keychain: {error}"))
}

pub fn save_secret(repo_id: &str, key: &str, value: Option<&str>) -> Result<(), String> {
    let entry = keychain_entry(repo_id, key)?;
    match value.filter(|value| !value.is_empty()) {
        Some(value) => entry
            .set_password(value)
            .map_err(|error| format!("Could not save secret in the OS keychain: {error}")),
        None => match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(format!(
                "Could not clear secret from the OS keychain: {error}"
            )),
        },
    }
}

fn load_secret(repo_id: &str, key: &str) -> Option<String> {
    keychain_entry(repo_id, key).ok()?.get_password().ok()
}

fn default_value(value: &serde_json::Value) -> Option<String> {
    match value {
        serde_json::Value::Null => None,
        serde_json::Value::String(value) => Some(value.clone()),
        serde_json::Value::Bool(value) => Some(value.to_string()),
        serde_json::Value::Number(value) => Some(value.to_string()),
        _ => None,
    }
}

fn discovery_values(input: &TestInputDefinition) -> Vec<(String, String)> {
    let Some(discovery) = &input.discovery else {
        return Vec::new();
    };
    let (kind, environment) = match discovery {
        InputDiscovery::Kind(kind) => (kind.as_str(), ""),
        InputDiscovery::Detailed { kind, environment } => (kind.as_str(), environment.as_str()),
    };
    match kind {
        "adbDevices" | "fireTv" => discover_fire_tv_devices()
            .into_iter()
            .filter(|d| d.eligible)
            .map(|d| {
                let label = if d.description.is_empty() {
                    d.serial.clone()
                } else {
                    format!("{} · {}", d.serial, d.description)
                };
                (d.serial, label)
            })
            .collect(),
        "androidSdk" => ["ANDROID_HOME", "ANDROID_SDK_ROOT"]
            .iter()
            .filter_map(|name| std::env::var(name).ok())
            .chain(std::env::var_os("HOME").map(|home| {
                PathBuf::from(home)
                    .join("Library/Android/sdk")
                    .to_string_lossy()
                    .into_owned()
            }))
            .filter(|path| Path::new(path).is_dir())
            .map(|path| (path.clone(), path))
            .collect(),
        "environment" if !environment.is_empty() => std::env::var(environment)
            .ok()
            .map(|value| vec![(value.clone(), value)])
            .unwrap_or_default(),
        _ => Vec::new(),
    }
}

fn input_error(
    workspace: &Path,
    input: &TestInputDefinition,
    value: &str,
    discovered: &[(String, String)],
) -> Option<String> {
    if value.is_empty() {
        return input.required.then(|| "Required".into());
    }
    match input.input_type.as_str() {
        "number" => {
            let Ok(number) = value.parse::<f64>() else {
                return Some("Must be a number".into());
            };
            if !number.is_finite() {
                return Some("Must be a finite number".into());
            }
            if input.validation.min.is_some_and(|min| number < min) {
                return Some(format!(
                    "Must be at least {}",
                    input.validation.min.unwrap()
                ));
            }
            if input.validation.max.is_some_and(|max| number > max) {
                return Some(format!("Must be at most {}", input.validation.max.unwrap()));
            }
        }
        "boolean" if !matches!(value, "true" | "false") => {
            return Some("Must be true or false".into())
        }
        "file" | "directory" => {
            let path = resolve_requirement_path(workspace, value);
            let valid = if input.input_type == "file" {
                path.is_file()
            } else {
                path.is_dir()
            };
            if !valid {
                return Some(format!(
                    "{} is not an available {}",
                    path.display(),
                    input.input_type
                ));
            }
        }
        "select" => {
            let allowed = input.options.iter().any(|option| option.value() == value)
                || discovered.iter().any(|(candidate, _)| candidate == value);
            if !allowed {
                return Some("The selected value is no longer available".into());
            }
        }
        "device"
            if input.discovery.is_some()
                && !discovered.iter().any(|(candidate, _)| candidate == value) =>
        {
            return Some("The selected device is no longer available".into());
        }
        _ => {}
    }
    if input
        .validation
        .min_length
        .is_some_and(|min| value.chars().count() < min)
    {
        return Some(format!(
            "Must contain at least {} characters",
            input.validation.min_length.unwrap()
        ));
    }
    if input
        .validation
        .max_length
        .is_some_and(|max| value.chars().count() > max)
    {
        return Some(format!(
            "Must contain at most {} characters",
            input.validation.max_length.unwrap()
        ));
    }
    if !input.validation.pattern.is_empty()
        && !regex::Regex::new(&input.validation.pattern)
            .ok()
            .is_some_and(|pattern| pattern.is_match(value))
    {
        return Some("Does not match the required format".into());
    }
    None
}

fn resolve_inputs(
    definition: &TestDefinition,
    workspace: &Path,
    repo_id: &str,
    saved_inputs: &HashMap<String, String>,
) -> (Vec<ResolvedInput>, HashMap<String, String>) {
    let mut controls = Vec::new();
    let mut values = HashMap::new();
    for input in &definition.inputs {
        let discovered = discovery_values(input);
        let saved = if input.input_type == "secret" {
            load_secret(repo_id, &input.id)
        } else {
            saved_inputs.get(&input.id).cloned()
        };
        let (value, provenance) = if let Some(value) = saved {
            (value, "saved")
        } else if let Some(value) = default_value(&input.default) {
            (value, "default")
        } else if discovered.len() == 1 {
            (discovered[0].0.clone(), "detected")
        } else {
            (String::new(), "")
        };
        let mut error = input_error(workspace, input, &value, &discovered);
        let unavailable_discovery = value.is_empty()
            && input.required
            && input.discovery.is_some()
            && discovered.is_empty()
            && matches!(input.input_type.as_str(), "device" | "select");
        if unavailable_discovery {
            error = Some("No available values were detected".into());
        }
        let has_value = !value.is_empty();
        let state = if unavailable_discovery {
            "invalid"
        } else if error.is_some() {
            if has_value {
                "invalid"
            } else {
                "required"
            }
        } else if provenance == "detected" {
            "detected"
        } else if provenance == "saved" {
            "saved"
        } else {
            "ready"
        };
        if has_value {
            values.insert(input.id.clone(), value.clone());
        }
        let mut options = input
            .options
            .iter()
            .map(|option| ResolvedInputOption {
                value: option.value().into(),
                label: option.label().into(),
                detected: false,
            })
            .collect::<Vec<_>>();
        for (value, label) in discovered {
            if !options.iter().any(|option| option.value == value) {
                options.push(ResolvedInputOption {
                    value,
                    label,
                    detected: true,
                });
            }
        }
        controls.push(ResolvedInput {
            id: input.id.clone(),
            label: input.label.clone(),
            help: input.help.clone(),
            input_type: input.input_type.clone(),
            required: input.required,
            value: if input.input_type == "secret" {
                String::new()
            } else {
                value
            },
            has_value,
            valid: error.is_none(),
            state: state.into(),
            message: error.unwrap_or_default(),
            provenance: provenance.into(),
            persistence: input.persistence.clone(),
            suites: input.suites.clone(),
            options,
        });
    }
    (controls, values)
}

fn input_consumed(input: &TestInputDefinition, suite: &TestSuiteDefinition) -> bool {
    input.suites.is_empty() || input.suites.iter().any(|id| id == &suite.id)
}

fn bound_command(
    definition: &TestDefinition,
    suite: &TestSuiteDefinition,
    values: &HashMap<String, String>,
    redact: bool,
) -> (Vec<String>, Vec<(String, String)>) {
    let mut argv = suite.command.clone();
    let mut environment = Vec::new();
    for input in definition
        .inputs
        .iter()
        .filter(|input| input_consumed(input, suite))
    {
        let Some(actual) = values.get(&input.id) else {
            continue;
        };
        let truthy = actual == "true";
        let rendered = if redact && input.input_type == "secret" {
            "<redacted>"
        } else {
            actual
        };
        if input.input_type != "boolean" || truthy {
            argv.extend(
                input
                    .binding
                    .arguments
                    .iter()
                    .map(|argument| argument.replace("{value}", rendered)),
            );
        }
        if !input.binding.environment.is_empty() {
            environment.push((input.binding.environment.clone(), rendered.to_string()));
        }
    }
    (argv, environment)
}

pub fn build_plan(
    workspace: &Path,
    run_dir: &Path,
    repo_id: &str,
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
            inputs: Vec::new(),
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
                inputs: Vec::new(),
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
    let (inputs, resolved_values) = resolve_inputs(&definition, workspace, repo_id, saved_inputs);
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
            let mut requirements =
                evaluate_requirements(workspace, suite, &devices, &selected_device);
            for (input, control) in definition
                .inputs
                .iter()
                .zip(inputs.iter())
                .filter(|(input, _)| input_consumed(input, suite))
            {
                requirements.push(RequirementStatus {
                    kind: "input".into(),
                    label: input.label.clone(),
                    state: if control.valid {
                        "ready"
                    } else if control.state == "invalid" {
                        "missing"
                    } else {
                        "waiting"
                    }
                    .into(),
                    detail: if control.valid {
                        match control.provenance.as_str() {
                            "saved" => "Using saved value".into(),
                            "detected" => "Detected automatically".into(),
                            "default" => "Using default value".into(),
                            _ => "Value supplied".into(),
                        }
                    } else {
                        control.message.clone()
                    },
                    action: if control.valid {
                        String::new()
                    } else if control.has_value {
                        "Correct or reset this input".into()
                    } else {
                        "Supply this required input".into()
                    },
                    input_key: input.id.clone(),
                });
            }
            let waiting = requirements.iter().any(|item| item.state == "waiting");
            let missing = requirements.iter().any(|item| item.state == "missing");
            let disruptive_blocked = suite.disruptive && !allow_disruptive;
            let mut result = previous.get(&suite.id).cloned().unwrap_or(SuiteResult {
                origin: suite.origin.clone(),
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
                log_path: String::new(),
                ai_generated_data: Vec::new(),
                argv: Vec::new(),
                environment: Vec::new(),
            });
            result.origin = suite.origin.clone();
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
                result.detail = requirements
                    .iter()
                    .filter(|item| item.state == "waiting")
                    .map(|item| format!("{}: {}", item.label, item.detail))
                    .collect::<Vec<_>>()
                    .join("; ");
            } else if missing {
                result.state = "Blocked".into();
                result.blocked = true;
                result.detail = requirements
                    .iter()
                    .filter(|item| item.state == "missing")
                    .map(|item| item.detail.as_str())
                    .collect::<Vec<_>>()
                    .join("; ");
            }
            let (mut argv, environment) = bound_command(&definition, suite, &resolved_values, true);
            if !selected_device.is_empty() {
                for device in &suite.requirements.devices {
                    argv.extend([device.argument.clone(), selected_device.clone()]);
                }
            }
            let environment_names = environment
                .iter()
                .map(|(name, _)| name.clone())
                .collect::<Vec<_>>();
            result.argv = argv.clone();
            result.environment = environment_names.clone();
            SuitePlan {
                result,
                command: serde_json::to_string(&argv).unwrap_or_default(),
                timeout_seconds: suite.timeout_seconds,
                disruptive: suite.disruptive,
                requirements,
                argv,
                environment: environment_names,
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
        inputs,
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
        let path = resolve_requirement_path(workspace, relative);
        let ready = path.is_file() && File::open(&path).is_ok();
        statuses.push(requirement(
            "file",
            relative,
            ready,
            if ready {
                path.to_string_lossy().into_owned()
            } else {
                format!("{} is missing or unreadable", path.display())
            },
            format!("Create or restore {relative}"),
        ));
    }
    for server in &suite.requirements.servers {
        let (ready, detail) = server_ready(server);
        statuses.push(requirement(
            "server",
            &server.name,
            ready,
            detail,
            format!("Start {} and verify its address", server.name),
        ));
    }
    for mount in &suite.requirements.mounts {
        let path = expand_home(&mount.path);
        let ready =
            path.is_dir() && fs::read_dir(&path).is_ok() && mount_kind_matches(&path, &mount.kind);
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
    for path_requirement in &suite.requirements.paths {
        let path = resolve_requirement_path(workspace, &path_requirement.path);
        let metadata = fs::metadata(&path).ok();
        let kind_ready =
            metadata
                .as_ref()
                .is_some_and(|metadata| match path_requirement.kind.as_str() {
                    "file" => metadata.is_file(),
                    "directory" => metadata.is_dir(),
                    "" | "any" => true,
                    _ => false,
                });
        let readable = !path_requirement.readable || readable_path(&path, &path_requirement.kind);
        let ready = kind_ready && readable;
        statuses.push(requirement(
            "path",
            &path_requirement.name,
            ready,
            if ready {
                format!("{} is present and readable", path.display())
            } else {
                format!(
                    "{} is missing, has the wrong type, or is not readable",
                    path.display()
                )
            },
            format!(
                "Make {} available and refresh requirements",
                path_requirement.name
            ),
        ));
    }
    if let Some(android) = &suite.requirements.android_sdk {
        let (ready, detail) = android_sdk_ready(workspace, android);
        statuses.push(requirement(
            "androidSdk",
            "Android SDK",
            ready,
            detail,
            "Install a usable Android SDK or set sdk.dir, ANDROID_HOME, or ANDROID_SDK_ROOT".into(),
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

fn server_ready(server: &ServerRequirement) -> (bool, String) {
    if !server.url.is_empty() {
        return http_health_ready(server);
    }
    let Ok(addresses) = (server.host.as_str(), server.port).to_socket_addrs() else {
        return (
            false,
            format!("{}:{} could not be resolved", server.host, server.port),
        );
    };
    let timeout = Duration::from_secs(server.timeout_seconds.max(1));
    let ready = addresses
        .into_iter()
        .any(|address| TcpStream::connect_timeout(&address, timeout).is_ok());
    (
        ready,
        if ready {
            format!("{}:{} accepted a connection", server.host, server.port)
        } else {
            format!("{}:{} is not reachable", server.host, server.port)
        },
    )
}

fn http_health_ready(server: &ServerRequirement) -> (bool, String) {
    let Ok(url) = url::Url::parse(&server.url) else {
        return (false, format!("{} is not a valid URL", server.url));
    };
    let Some(host) = url.host_str() else {
        return (false, format!("{} has no host", server.url));
    };
    let port = url.port_or_known_default().unwrap_or(80);
    let timeout = Duration::from_secs(server.timeout_seconds.max(1));
    let Ok(addresses) = (host, port).to_socket_addrs() else {
        return (false, format!("{} could not be resolved", server.url));
    };
    for address in addresses {
        let Ok(mut stream) = TcpStream::connect_timeout(&address, timeout) else {
            continue;
        };
        let _ = stream.set_read_timeout(Some(timeout));
        let _ = stream.set_write_timeout(Some(timeout));
        let target = if let Some(query) = url.query() {
            format!("{}?{}", url.path(), query)
        } else if url.path().is_empty() {
            "/".into()
        } else {
            url.path().into()
        };
        let request = format!("GET {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n");
        if stream.write_all(request.as_bytes()).is_err() {
            continue;
        }
        let mut response = [0_u8; 128];
        let Ok(count) = stream.read(&mut response) else {
            continue;
        };
        let status = String::from_utf8_lossy(&response[..count])
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|value| value.parse::<u16>().ok());
        if status.is_some_and(|status| (200..400).contains(&status)) {
            return (
                true,
                format!("{} returned HTTP {}", server.url, status.unwrap()),
            );
        }
        return (
            false,
            status.map_or_else(
                || format!("{} did not return HTTP", server.url),
                |status| format!("{} returned HTTP {status}", server.url),
            ),
        );
    }
    (false, format!("{} is not reachable", server.url))
}

fn resolve_requirement_path(workspace: &Path, value: &str) -> PathBuf {
    let expanded = expand_home(value);
    if expanded.is_absolute() {
        expanded
    } else {
        workspace.join(expanded)
    }
}

fn readable_path(path: &Path, kind: &str) -> bool {
    match kind {
        "directory" => fs::read_dir(path).is_ok(),
        "file" => File::open(path).is_ok(),
        _ if path.is_dir() => fs::read_dir(path).is_ok(),
        _ => File::open(path).is_ok(),
    }
}

fn android_sdk_ready(workspace: &Path, requirement: &AndroidSdkRequirement) -> (bool, String) {
    let project = workspace.join(&requirement.project);
    let local_sdk = fs::read_to_string(project.join("local.properties"))
        .ok()
        .and_then(|raw| {
            raw.lines()
                .find_map(|line| line.trim().strip_prefix("sdk.dir=").map(str::to_string))
        })
        .map(|value| PathBuf::from(value.replace("\\:", ":").replace("\\\\", "\\")));
    let sdk = local_sdk
        .or_else(|| std::env::var_os("ANDROID_HOME").map(PathBuf::from))
        .or_else(|| std::env::var_os("ANDROID_SDK_ROOT").map(PathBuf::from))
        .or_else(|| {
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join("Library/Android/sdk"))
        });
    let Some(sdk) = sdk else {
        return (false, "No Android SDK location is configured".into());
    };
    let has_platform = directory_has_entry(&sdk.join("platforms"));
    let has_build_tools = directory_has_entry(&sdk.join("build-tools"));
    let has_adb = sdk.join("platform-tools/adb").is_file();
    let ready = sdk.is_dir() && has_platform && has_build_tools && has_adb;
    (
        ready,
        if ready {
            format!("Usable Android SDK at {}", sdk.display())
        } else {
            format!(
                "Android SDK at {} is incomplete (needs a platform, build-tools, and platform-tools/adb)",
                sdk.display()
            )
        },
    )
}

fn directory_has_entry(path: &Path) -> bool {
    fs::read_dir(path)
        .ok()
        .and_then(|mut entries| entries.next())
        .is_some()
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
/// prunes the oldest entries beyond `HISTORY_LIMIT`. Each suite contains only
/// the bounded preview captured from its full sibling `.log` file.
fn append_history(run_dir: &Path, results: &TestRunResults) {
    let dir = run_dir.join(HISTORY_DIR);
    if fs::create_dir_all(&dir).is_err() {
        return;
    }
    let Ok(bytes) = serde_json::to_vec_pretty(results) else {
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
            origin: String::new(),
            id,
            name,
            command,
            timeout_seconds: raw["timeoutSeconds"].as_u64().unwrap_or(default_timeout()),
            disruptive: raw["disruptive"].as_bool().unwrap_or(false),
            // AI-suggested commands are unverified guesses; a human must
            // review and turn them on explicitly.
            enabled: false,
            requirements: Requirements::default(),
            covers: Vec::new(),
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
    let checkout = CheckoutGuard::acquire(workspace)?;
    let definition = load_definition(workspace)?;
    let resolved_inputs = load_inputs(run_dir, saved_inputs);
    let repo_id = crate::config::repo_slug(repository);
    let plan = build_plan(
        workspace,
        run_dir,
        &repo_id,
        &resolved_inputs,
        allow_disruptive,
    );
    if !plan.available {
        return Err(plan.error);
    }
    // Resolve again for execution so secrets remain internal and the same
    // validation/binding path is used immediately before children start.
    let (_, input_values) = resolve_inputs(&definition, workspace, &repo_id, &resolved_inputs);
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
        tested_commit: checkout.commit.clone(),
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
                    result.log_path.clear();
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
        if let Err(error) = checkout.verify(workspace) {
            for remaining in results.suites.iter_mut().skip(index) {
                if remaining.state == "Ready" {
                    remaining.state = "Blocked".into();
                    remaining.blocked = true;
                    remaining.detail = error.clone();
                }
            }
            write_results(&results_path, &results)?;
            break;
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
        let outcome = run_suite_with_inputs(
            workspace,
            &run_dir
                .join("test-logs")
                .join(results.started_at.to_string()),
            suite,
            &plan.selected_device,
            &definition,
            &input_values,
            ai_data_dir.as_deref(),
            &checkout,
        )
        .unwrap_or_else(|error| CommandOutcome {
            exit_code: Some(127),
            duration_ms: 0,
            detail: error,
            output: String::new(),
            log_path: String::new(),
            blocked: false,
        });
        results.suites[index].state = if outcome.blocked {
            results.suites[index].blocked = true;
            "Blocked".into()
        } else if outcome.exit_code == Some(0) {
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
        results.suites[index].log_path = outcome.log_path;
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
    log_path: String,
    blocked: bool,
}

struct CheckoutGuard {
    commit: String,
    branch: String,
    lock_path: PathBuf,
    _lock: File,
}

impl CheckoutGuard {
    fn acquire(workspace: &Path) -> Result<Self, String> {
        let commit = git_value(workspace, &["rev-parse", "--verify", "HEAD"])?;
        let status = git_value(
            workspace,
            &["status", "--porcelain", "--untracked-files=all"],
        )?;
        if !status.is_empty() {
            return Err(
                "The test checkout must be clean so results identify an exact commit; commit or remove local changes first"
                    .into(),
            );
        }
        git_value(
            workspace,
            &["ls-files", "--error-unmatch", TEST_DEFINITION_PATH],
        )
        .map_err(|_| {
            format!(
                "{TEST_DEFINITION_PATH} must be tracked before tests can run so a fresh clone uses the same contract"
            )
        })?;
        let branch = git_value(workspace, &["symbolic-ref", "--quiet", "--short", "HEAD"])
            .unwrap_or_else(|_| "(detached)".into());
        let common_dir = git_value(workspace, &["rev-parse", "--git-common-dir"])?;
        let common_dir = {
            let path = PathBuf::from(common_dir);
            if path.is_absolute() {
                path
            } else {
                workspace.join(path)
            }
        };
        let lock_path = common_dir.join(CHECKOUT_LOCK_FILE);
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        let mut lock = match options.open(&lock_path) {
            Ok(lock) => lock,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                if stale_checkout_lock(&lock_path) {
                    fs::remove_file(&lock_path).map_err(|remove_error| {
                        format!("Could not remove stale test-run lock: {remove_error}")
                    })?;
                    options.open(&lock_path).map_err(|retry_error| {
                        format!("Could not acquire checkout test-run lock: {retry_error}")
                    })?
                } else {
                    return Err(
                        "Another test or issue-worker run currently owns this checkout".into(),
                    );
                }
            }
            Err(error) => return Err(format!("Could not acquire checkout test-run lock: {error}")),
        };
        writeln!(lock, "{}\n{}", std::process::id(), commit)
            .map_err(|error| format!("Could not write checkout test-run lock: {error}"))?;
        Ok(Self {
            commit,
            branch,
            lock_path,
            _lock: lock,
        })
    }

    fn verify(&self, workspace: &Path) -> Result<(), String> {
        let commit = git_value(workspace, &["rev-parse", "--verify", "HEAD"])?;
        let branch = git_value(workspace, &["symbolic-ref", "--quiet", "--short", "HEAD"])
            .unwrap_or_else(|_| "(detached)".into());
        if commit != self.commit || branch != self.branch {
            return Err(format!(
                "Blocked because the checkout changed during the run (expected {} at {}, found {} at {})",
                self.branch, self.commit, branch, commit
            ));
        }
        Ok(())
    }
}

impl Drop for CheckoutGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.lock_path);
    }
}

fn git_value(workspace: &Path, arguments: &[&str]) -> Result<String, String> {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(workspace)
        .output()
        .map_err(|error| format!("Could not inspect the test checkout: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "Could not inspect the test checkout: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn stale_checkout_lock(path: &Path) -> bool {
    let pid = fs::read_to_string(path)
        .ok()
        .and_then(|raw| raw.lines().next()?.parse::<i32>().ok());
    let Some(pid) = pid else { return true };
    #[cfg(unix)]
    unsafe {
        libc::kill(pid, 0) != 0
            && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
    }
    #[cfg(not(unix))]
    {
        false
    }
}

#[allow(clippy::too_many_arguments)]
fn run_suite_with_inputs(
    workspace: &Path,
    run_dir: &Path,
    suite: &TestSuiteDefinition,
    selected_device: &str,
    definition: &TestDefinition,
    input_values: &HashMap<String, String>,
    ai_data_dir: Option<&Path>,
    checkout: &CheckoutGuard,
) -> Result<CommandOutcome, String> {
    fs::create_dir_all(run_dir).map_err(|error| error.to_string())?;
    let log_path = run_dir.join(format!("{}.log", suite.id));
    let stdout = File::create(&log_path).map_err(|error| error.to_string())?;
    let stderr = stdout.try_clone().map_err(|error| error.to_string())?;
    let started = Instant::now();
    let (bound, environment) = bound_command(definition, suite, input_values, false);
    let mut command = Command::new(&bound[0]);
    let mut arguments = bound[1..].to_vec();
    if !selected_device.is_empty() {
        for device in &suite.requirements.devices {
            arguments.extend([device.argument.clone(), selected_device.to_string()]);
        }
    }
    command
        .args(&arguments)
        .current_dir(workspace)
        .env("PATH", tools::enhanced_path())
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    for (name, value) in environment {
        command.env(name, value);
    }
    if let Some(dir) = ai_data_dir {
        command.env("SWARM_AI_TEST_DATA_DIR", dir);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
        set_safe_nofile_limit(&mut command);
    }
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start suite '{}': {error}", suite.name))?;
    let deadline = Instant::now() + Duration::from_secs(suite.timeout_seconds);
    let (exit_code, detail, blocked) = loop {
        if let Some(status) = child.try_wait().map_err(|error| error.to_string())? {
            break (
                status.code(),
                format!(
                    "Exited with status {}",
                    status
                        .code()
                        .map_or_else(|| "signal".into(), |code| code.to_string())
                ),
                false,
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
                false,
            );
        }
        if let Err(error) = checkout.verify(workspace) {
            #[cfg(unix)]
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            #[cfg(not(unix))]
            let _ = child.kill();
            let _ = child.wait();
            break (None, error, true);
        }
        thread::sleep(Duration::from_millis(100));
    };
    let mut output = String::new();
    if let Ok(mut file) = File::open(&log_path) {
        let _ = file.read_to_string(&mut output);
        if output.len() > OUTPUT_PREVIEW_BYTES {
            let mut start = output.len() - OUTPUT_PREVIEW_BYTES;
            while !output.is_char_boundary(start) {
                start += 1;
            }
            output = output.split_off(start);
        }
    }
    let secrets = definition
        .inputs
        .iter()
        .filter(|input| input.input_type == "secret")
        .filter_map(|input| input_values.get(&input.id))
        .filter(|value| !value.is_empty())
        .collect::<Vec<_>>();
    for secret in &secrets {
        output = output.replace(secret.as_str(), "<redacted>");
    }
    if !secrets.is_empty() {
        if let Ok(mut full) = fs::read_to_string(&log_path) {
            for secret in secrets {
                full = full.replace(secret.as_str(), "<redacted>");
            }
            let _ = fs::write(&log_path, full);
        }
    }
    Ok(CommandOutcome {
        exit_code,
        duration_ms: started.elapsed().as_millis() as u64,
        detail,
        output,
        log_path: log_path.to_string_lossy().into_owned(),
        blocked,
    })
}

#[cfg(test)]
fn run_suite(
    workspace: &Path,
    run_dir: &Path,
    suite: &TestSuiteDefinition,
    selected_device: &str,
    ai_data_dir: Option<&Path>,
    checkout: &CheckoutGuard,
) -> Result<CommandOutcome, String> {
    let definition = TestDefinition {
        adversarial_bootstrap: None,
        version: 1,
        suites: vec![suite.clone()],
        coverage_notes: Vec::new(),
        reporting: None,
        failure_triage: None,
        inputs: Vec::new(),
    };
    run_suite_with_inputs(
        workspace,
        run_dir,
        suite,
        selected_device,
        &definition,
        &HashMap::new(),
        ai_data_dir,
        checkout,
    )
}

#[cfg(test)]
fn suite_arguments(suite: &TestSuiteDefinition, selected_device: &str) -> Vec<String> {
    let mut arguments = suite.command[1..].to_vec();
    if !selected_device.is_empty() {
        for device in &suite.requirements.devices {
            arguments.extend([device.argument.clone(), selected_device.to_string()]);
        }
    }
    arguments
}

#[cfg(unix)]
fn set_safe_nofile_limit(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    unsafe {
        command.pre_exec(|| {
            let mut limits: libc::rlimit = std::mem::zeroed();
            if libc::getrlimit(libc::RLIMIT_NOFILE, &mut limits) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            let desired = (CHILD_NOFILE_LIMIT as libc::rlim_t).min(limits.rlim_max);
            if limits.rlim_cur < desired {
                limits.rlim_cur = desired;
                if libc::setrlimit(libc::RLIMIT_NOFILE, &limits) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
            }
            Ok(())
        });
    }
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
        set_safe_nofile_limit(&mut process);
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
    use std::net::TcpListener;
    use tempfile::tempdir;

    fn commit_test_workspace(workspace: &Path) -> String {
        let run = |arguments: &[&str]| {
            let output = Command::new("git")
                .args(arguments)
                .current_dir(workspace)
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "git {:?}: {}",
                arguments,
                String::from_utf8_lossy(&output.stderr)
            );
            String::from_utf8_lossy(&output.stdout).trim().to_string()
        };
        run(&["init", "-q"]);
        run(&["config", "user.email", "tests@example.invalid"]);
        run(&["config", "user.name", "SWARM Tests"]);
        run(&["add", "."]);
        run(&["commit", "-q", "-m", "test fixture"]);
        run(&["rev-parse", "HEAD"])
    }

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
    fn repository_test_contract_is_tracked_and_atomic() {
        let workspace = Path::new(env!("CARGO_MANIFEST_DIR"));
        let definition = load_definition(workspace).unwrap();
        let ids = definition
            .suites
            .iter()
            .map(|suite| suite.id.as_str())
            .collect::<std::collections::HashSet<_>>();
        assert_eq!(ids.len(), definition.suites.len());
        assert!(definition
            .suites
            .iter()
            .any(|suite| { suite.command == ["cargo", "test", "--locked"] }));
        assert!(definition.suites.iter().all(|suite| {
            !suite.command.iter().any(|argument| {
                argument.contains("full_uat_suite") || argument.contains("media_server_uat_tests")
            })
        }));
        assert!(git_value(
            workspace,
            &["ls-files", "--error-unmatch", TEST_DEFINITION_PATH]
        )
        .is_ok());
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
    fn adversarial_origin_is_backward_compatible_and_survives_scheduled_plans() {
        let legacy: TestSuiteDefinition =
            serde_json::from_str(r#"{"id":"unit","name":"Unit","command":["true"]}"#).unwrap();
        assert!(legacy.origin.is_empty());
        let workspace = tempdir().unwrap();
        let raw = r#"{"version":1,"adversarialBootstrap":{"framework":"unittest"},"suites":[{"id":"adversarial-180","name":"Independent acceptance","origin":"adversarial","command":["true"]}]}"#;
        create_definition(workspace.path(), raw).unwrap();
        let definition = load_definition(workspace.path()).unwrap();
        assert_eq!(definition.suites[0].origin, "adversarial");
        assert_eq!(
            definition.adversarial_bootstrap.as_ref().unwrap()["framework"],
            "unittest"
        );
        let runs = tempdir().unwrap();
        let plan = build_plan(
            workspace.path(),
            runs.path(),
            "repo",
            &HashMap::new(),
            false,
        );
        assert_eq!(plan.suites[0].result.origin, "adversarial");
        assert_eq!(
            serde_json::to_value(&definition.suites[0]).unwrap()["origin"],
            "adversarial"
        );
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
    fn selected_device_is_bound_as_exact_device_arguments() {
        let suite = TestSuiteDefinition {
            origin: String::new(),
            id: "tv".into(),
            name: "TV".into(),
            command: vec!["suite.sh".into(), "--no-issue".into()],
            timeout_seconds: 10,
            disruptive: false,
            enabled: true,
            requirements: Requirements {
                devices: vec![DeviceRequirement {
                    device_type: "fireTv".into(),
                    input: "fireTvSerial".into(),
                    argument: "--device".into(),
                }],
                ..Requirements::default()
            },
            covers: Vec::new(),
        };
        assert_eq!(
            suite_arguments(&suite, "192.0.2.44:5555"),
            ["--no-issue", "--device", "192.0.2.44:5555"]
        );
    }

    #[test]
    fn schema_v2_resolves_validates_and_binds_generic_inputs_per_suite() {
        let workspace = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        let definition = serde_json::json!({
            "version": 2,
            "inputs": [
                {"id":"stunPort","label":"STUN port","type":"number","required":true,"validation":{"min":1,"max":65535},"suites":["network"],"binding":{"environment":"SWARM_STUN_PORT"}},
                {"id":"selector","label":"Scenario","type":"text","suites":["network"],"binding":{"arguments":["--test","{value}"]}},
                {"id":"all","label":"All devices","type":"boolean","default":false,"suites":["network"],"binding":{"arguments":["--all"]}},
                {"id":"data","label":"Data directory","type":"directory","required":true,"suites":["blocked"],"binding":{"environment":"SWARM_SERVER_DATA_DIR"}}
            ],
            "suites": [
                {"id":"network","name":"Network","command":["runner","base"]},
                {"id":"blocked","name":"Blocked","command":["runner"]}
            ]
        });
        fs::write(
            definition_path(workspace.path()),
            serde_json::to_vec(&definition).unwrap(),
        )
        .unwrap();
        let saved = HashMap::from([
            ("stunPort".into(), "3478".into()),
            ("selector".into(), "living room".into()),
            ("all".into(), "true".into()),
        ]);
        let plan = build_plan(
            workspace.path(),
            &workspace.path().join("run"),
            "owner__repo",
            &saved,
            false,
        );
        assert_eq!(plan.suites[0].result.state, "Ready");
        assert_eq!(
            plan.suites[0].argv,
            ["runner", "base", "--test", "living room", "--all"]
        );
        assert_eq!(plan.suites[0].environment, ["SWARM_STUN_PORT"]);
        assert_eq!(plan.suites[1].result.state, "Waiting for input");
        assert!(plan.suites[1].result.detail.contains("Data directory"));
        assert_eq!(
            plan.inputs
                .iter()
                .find(|input| input.id == "stunPort")
                .unwrap()
                .provenance,
            "saved"
        );
    }

    #[test]
    fn secret_bindings_are_redacted_from_plan_results_and_full_logs() {
        let workspace = tempdir().unwrap();
        let run_dir = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"probe","name":"Probe","command":["true"]}]}"#,
        )
        .unwrap();
        commit_test_workspace(workspace.path());
        let definition: TestDefinition = serde_json::from_value(serde_json::json!({
            "version":2,
            "inputs":[{"id":"token","label":"Token","type":"secret","required":true,"persistence":"keychain","binding":{"environment":"TEST_TOKEN","arguments":["{value}"]}}],
            "suites":[{"id":"probe","name":"Probe","command":["/bin/sh","-c","printf '%s' \"$TEST_TOKEN\""]}]
        })).unwrap();
        validate_definition(&definition).unwrap();
        let values = HashMap::from([("token".into(), "never-store-this".into())]);
        let (preview, _) = bound_command(&definition, &definition.suites[0], &values, true);
        assert!(preview.contains(&"<redacted>".to_string()));
        assert!(!serde_json::to_string(&preview)
            .unwrap()
            .contains("never-store-this"));
        let checkout = CheckoutGuard::acquire(workspace.path()).unwrap();
        let outcome = run_suite_with_inputs(
            workspace.path(),
            run_dir.path(),
            &definition.suites[0],
            "",
            &definition,
            &values,
            None,
            &checkout,
        )
        .unwrap();
        assert_eq!(outcome.output, "<redacted>");
        assert_eq!(fs::read_to_string(outcome.log_path).unwrap(), "<redacted>");
    }

    #[test]
    fn invalid_android_sdk_blocks_before_execution() {
        let workspace = tempdir().unwrap();
        fs::create_dir_all(workspace.path().join(".swarm")).unwrap();
        fs::create_dir_all(workspace.path().join("clients/tv-android")).unwrap();
        fs::write(
            workspace.path().join("clients/tv-android/local.properties"),
            "sdk.dir=/definitely/not/an/android/sdk\n",
        )
        .unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"android","name":"Android","command":["/usr/bin/true"],"requirements":{"androidSdk":{"project":"clients/tv-android"}}}]}"#,
        )
        .unwrap();
        let plan = build_plan(
            workspace.path(),
            &workspace.path().join("run"),
            "test-repo",
            &HashMap::new(),
            false,
        );
        assert_eq!(plan.suites[0].result.state, "Blocked");
        assert!(plan.suites[0]
            .requirements
            .iter()
            .any(|requirement| requirement.kind == "androidSdk" && requirement.state == "missing"));
    }

    #[test]
    fn http_server_requirement_checks_the_health_path_and_status() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 256];
            let count = stream.read(&mut request).unwrap();
            assert!(String::from_utf8_lossy(&request[..count]).starts_with("GET /health "));
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                .unwrap();
        });
        let requirement = ServerRequirement {
            name: "Media server".into(),
            host: String::new(),
            port: 0,
            url: format!("http://127.0.0.1:{port}/health"),
            timeout_seconds: 1,
        };
        let (ready, detail) = server_ready(&requirement);
        server.join().unwrap();
        assert!(ready);
        assert!(detail.contains("HTTP 200"));
    }

    #[test]
    fn blocked_preconditions_do_not_return_a_test_failure() {
        let workspace = tempdir().unwrap();
        let run_dir = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"blocked","name":"Blocked","command":["/usr/bin/false"],"requirements":{"paths":[{"name":"library","path":"missing/library.sqlite","kind":"file"}]}}]}"#,
        )
        .unwrap();
        commit_test_workspace(workspace.path());
        assert_eq!(
            run_once(
                workspace.path(),
                run_dir.path(),
                "owner/repo",
                &HashMap::new(),
                false,
                false,
                "manual",
                &ai_disabled(),
            )
            .unwrap(),
            0
        );
        let results = read_results(&run_dir.path().join("test-results.json")).unwrap();
        assert_eq!(results.suites[0].state, "Blocked");
        assert!(results.suites[0].blocked);
    }

    #[test]
    fn runner_detects_a_checkout_commit_change_and_blocks_the_suite() {
        let workspace = tempdir().unwrap();
        let run_dir = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"moves-head","name":"Moves HEAD","command":["/bin/sh","-c","git switch -q --detach HEAD~1; sleep 1"]}]}"#,
        )
        .unwrap();
        commit_test_workspace(workspace.path());
        fs::write(workspace.path().join("second"), "second").unwrap();
        let run = |arguments: &[&str]| {
            assert!(Command::new("git")
                .args(arguments)
                .current_dir(workspace.path())
                .status()
                .unwrap()
                .success());
        };
        run(&["add", "second"]);
        run(&["commit", "-q", "-m", "second"]);
        assert_eq!(
            run_once(
                workspace.path(),
                run_dir.path(),
                "owner/repo",
                &HashMap::new(),
                false,
                false,
                "manual",
                &ai_disabled(),
            )
            .unwrap(),
            0
        );
        let results = read_results(&run_dir.path().join("test-results.json")).unwrap();
        assert_eq!(results.suites[0].state, "Blocked");
        assert!(results.suites[0].detail.contains("checkout changed"));
    }

    #[cfg(unix)]
    #[test]
    fn child_suite_gets_a_safe_nofile_limit_and_bounded_output_preview() {
        let workspace = tempdir().unwrap();
        let run_dir = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"probe","name":"Probe","command":["/usr/bin/true"]}]}"#,
        )
        .unwrap();
        commit_test_workspace(workspace.path());
        let checkout = CheckoutGuard::acquire(workspace.path()).unwrap();
        let suite = TestSuiteDefinition {
            origin: String::new(),
            id: "probe".into(),
            name: "Probe".into(),
            command: vec![
                "/bin/sh".into(),
                "-c".into(),
                "ulimit -n; yes x | head -c 20000".into(),
            ],
            timeout_seconds: 5,
            disruptive: false,
            enabled: true,
            requirements: Requirements::default(),
            covers: Vec::new(),
        };
        let outcome = run_suite(
            workspace.path(),
            run_dir.path(),
            &suite,
            "",
            None,
            &checkout,
        )
        .unwrap();
        assert_eq!(outcome.exit_code, Some(0));
        assert!(outcome.output.len() <= OUTPUT_PREVIEW_BYTES);
        let full = fs::read(run_dir.path().join("probe.log")).unwrap();
        assert!(full.len() > OUTPUT_PREVIEW_BYTES);
        let limit = String::from_utf8_lossy(&full)
            .lines()
            .next()
            .unwrap()
            .parse::<u64>()
            .unwrap();
        assert!(limit >= 4096);
    }

    #[test]
    fn unrelated_ready_suite_is_not_blocked_by_missing_hardware() {
        let workspace = tempdir().unwrap();
        fs::create_dir(workspace.path().join(".swarm")).unwrap();
        fs::write(
            definition_path(workspace.path()),
            r#"{"version":1,"suites":[{"id":"unit","name":"Unit","command":["true"]},{"id":"tv","name":"TV","command":["true"],"requirements":{"files":["connected-fire-tv.marker"]}}]}"#,
        )
        .unwrap();
        let plan = build_plan(
            workspace.path(),
            &workspace.path().join("run"),
            "test-repo",
            &HashMap::new(),
            false,
        );
        assert_eq!(plan.suites[0].result.state, "Ready");
        assert_eq!(plan.suites[1].result.state, "Blocked");
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
        let tested_commit = commit_test_workspace(workspace.path());
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
        assert_eq!(results.tested_commit, tested_commit);
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
        commit_test_workspace(workspace.path());
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
        commit_test_workspace(workspace.path());
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
        commit_test_workspace(workspace.path());
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
        commit_test_workspace(workspace.path());
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
