//! Recursive test-entry-point discovery, classification, deduplication, and
//! coverage auditing for the test scheduler (`testing.rs`).
//!
//! `discover_candidates` walks a repository once, excluding dependency and
//! generated-output directories, and returns every plausible test entry
//! point it can find (manifests, CI workflow steps, task-runner targets,
//! conventional test scripts, and repository-provided descriptors) already
//! classified and deduplicated against one another. `testing::detect_definition`
//! turns the `atomic` subset into a schedulable draft; `audit_coverage` compares
//! the full candidate set against an already-committed `.swarm/tests.json` so
//! the UI can show what is scheduled, covered, pending review, or unmapped.

use crate::testing::{suite_id, suite_name, DeviceRequirement, Requirements, TestSuiteDefinition};
use serde::Serialize;
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

/// Directories that are never repository-owned test surface: dependency
/// trees, VCS metadata, managed run data, caches, and generated output.
const EXCLUDED_DIR_NAMES: &[&str] = &[
    "node_modules",
    "target",
    ".git",
    ".hg",
    ".svn",
    ".run",
    "dist",
    "build",
    "out",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    ".cache",
    ".gradle",
    ".idea",
    ".vscode",
    ".next",
    ".pytest_cache",
    "coverage",
    ".terraform",
    ".tox",
];

/// Directory names conventionally holding test scripts, anywhere in the tree.
const CONVENTIONAL_TEST_DIR_NAMES: &[&str] = &["tests", "test", "e2e", "uat"];
const SCRIPT_EXTENSIONS: &[&str] = &["sh", "py", "js"];

/// One plausible test entry point found by recursive discovery, already
/// classified and (where determinable) related to other candidates.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Candidate {
    pub id: String,
    /// Repository-relative path this candidate was found at; empty when no
    /// single file owns it (not used by any discovery source today, kept for
    /// descriptor/synthetic sources).
    pub path: String,
    pub source: String,
    pub name: String,
    pub command: Vec<String>,
    /// One of `atomic`, `aggregate`, `helper`, `build-gate`, `alias`, `unknown`.
    pub classification: String,
    /// One of `declared` (repository-provided descriptor), `high` (explicit
    /// repository metadata), or `low` (heuristic inference — never silently
    /// enabled).
    pub confidence: String,
    pub disruptive: bool,
    pub detail: String,
    /// Repository-relative paths of other candidates whose assertions this
    /// one's command already exercises.
    pub covers: Vec<String>,
    pub requirements: Requirements,
    pub timeout_seconds: Option<u64>,
}

fn relative(workspace: &Path, path: &Path) -> String {
    path.strip_prefix(workspace)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn collect_files(workspace: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut stack = vec![workspace.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let Ok(file_type) = entry.file_type() else {
                continue;
            };
            if file_type.is_symlink() {
                continue;
            }
            let path = entry.path();
            if file_type.is_dir() {
                let name = entry.file_name().to_string_lossy().into_owned();
                if EXCLUDED_DIR_NAMES.contains(&name.as_str()) {
                    continue;
                }
                stack.push(path);
            } else if file_type.is_file() {
                files.push(path);
            }
        }
    }
    files
}

fn is_executable(path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::metadata(path)
            .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
            .unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        let _ = path;
        false
    }
}

/// Recursively finds and classifies every plausible test entry point in
/// `workspace`. Never runs a discovered command.
pub fn discover_candidates(workspace: &Path) -> Result<Vec<Candidate>, String> {
    if !workspace.is_dir() {
        return Err(format!(
            "The repository workspace does not exist at {}",
            workspace.display()
        ));
    }
    let files = collect_files(workspace);
    let mut candidates = Vec::new();
    candidates.extend(cargo_candidates(workspace, &files));
    candidates.extend(package_json_candidates(workspace, &files));
    candidates.extend(python_candidates(workspace, &files));
    candidates.extend(go_candidates(workspace, &files));
    candidates.extend(gradle_candidates(workspace, &files));
    candidates.extend(script_candidates(workspace, &files));
    candidates.extend(task_runner_candidates(workspace, &files));
    candidates.extend(ci_workflow_candidates(workspace, &files));
    candidates.extend(descriptor_candidates(workspace, &files));
    dedup_exact_command_aliases(&mut candidates);
    finalize_ids(&mut candidates);
    candidates.sort_by(|a, b| a.path.cmp(&b.path).then(a.id.cmp(&b.id)));
    Ok(candidates)
}

// ---------------------------------------------------------------------------
// Cargo workspaces and members
// ---------------------------------------------------------------------------

fn parse_cargo_workspace_members(manifest: &str) -> Vec<String> {
    let Some(start) = manifest.find("members") else {
        return Vec::new();
    };
    let after = &manifest[start..];
    let Some(open) = after.find('[') else {
        return Vec::new();
    };
    let Some(close) = after[open..].find(']') else {
        return Vec::new();
    };
    let body = &after[open + 1..open + close];
    body.split(',')
        .filter_map(|entry| {
            let trimmed = entry.trim().trim_matches('"').trim_matches('\'');
            if trimmed.is_empty() {
                None
            } else {
                Some(trimmed.to_string())
            }
        })
        .collect()
}

fn glob_matches(glob: &str, relative_dir: &str) -> bool {
    if glob == relative_dir {
        return true;
    }
    if let Some(prefix) = glob.strip_suffix("/*") {
        return Path::new(relative_dir)
            .parent()
            .map(|parent| parent.to_string_lossy() == prefix)
            .unwrap_or(false);
    }
    if let Some(prefix) = glob.strip_suffix('*') {
        return relative_dir.starts_with(prefix.trim_end_matches('/'));
    }
    false
}

fn cargo_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    let mut out = Vec::new();
    let root_manifest = workspace.join("Cargo.toml");
    if !root_manifest.is_file() {
        return out;
    }
    let manifest = fs::read_to_string(&root_manifest).unwrap_or_default();
    let is_workspace = manifest.contains("[workspace]");
    let locked = workspace.join("Cargo.lock").is_file();
    let mut command = vec!["cargo".to_string(), "test".to_string()];
    if is_workspace {
        command.push("--workspace".into());
    }
    if locked {
        command.push("--locked".into());
    }
    out.push(Candidate {
        id: if is_workspace {
            "rust-workspace"
        } else {
            "rust"
        }
        .into(),
        path: "Cargo.toml".into(),
        source: "cargo-manifest".into(),
        name: if is_workspace {
            "Rust workspace tests"
        } else {
            "Rust tests"
        }
        .into(),
        command,
        classification: "atomic".into(),
        confidence: "high".into(),
        disruptive: false,
        detail: "Root Cargo manifest".into(),
        covers: Vec::new(),
        requirements: Requirements {
            executables: vec!["cargo".into()],
            files: vec!["Cargo.toml".into()],
            ..Requirements::default()
        },
        timeout_seconds: None,
    });
    if is_workspace {
        let member_globs = parse_cargo_workspace_members(&manifest);
        let mut member_paths = Vec::new();
        for member_manifest in files.iter().filter(|path| {
            path.file_name().and_then(|name| name.to_str()) == Some("Cargo.toml")
                && path.as_path() != root_manifest.as_path()
        }) {
            let member_dir = member_manifest.parent().unwrap_or(workspace);
            let member_rel_dir = relative(workspace, member_dir);
            if !member_globs
                .iter()
                .any(|glob| glob_matches(glob, &member_rel_dir))
            {
                continue;
            }
            let rel_manifest = relative(workspace, member_manifest);
            member_paths.push(rel_manifest.clone());
            out.push(Candidate {
                id: suite_id(&format!("cargo-member-{rel_manifest}")),
                path: rel_manifest,
                source: "cargo-workspace-member".into(),
                name: suite_name(&format!("cargo member {member_rel_dir}")),
                command: Vec::new(),
                classification: "helper".into(),
                confidence: "high".into(),
                disruptive: false,
                detail: "Workspace member; exercised by the workspace-level cargo test command"
                    .into(),
                covers: Vec::new(),
                requirements: Requirements::default(),
                timeout_seconds: None,
            });
        }
        if let Some(root) = out.first_mut() {
            root.covers.extend(member_paths);
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Nested package.json (npm/yarn/pnpm), including Roku-style clients
// ---------------------------------------------------------------------------

fn package_json_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    let mut out = Vec::new();
    for manifest_path in files
        .iter()
        .filter(|path| path.file_name().and_then(|name| name.to_str()) == Some("package.json"))
    {
        let dir = manifest_path.parent().unwrap_or(workspace);
        let has_test_script = fs::read_to_string(manifest_path)
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
        if !has_test_script {
            continue;
        }
        let is_root = dir == workspace;
        let rel_dir = relative(workspace, dir);
        let executable = if dir.join("pnpm-lock.yaml").is_file() {
            "pnpm"
        } else if dir.join("yarn.lock").is_file() {
            "yarn"
        } else {
            "npm"
        };
        let (id, name, command) = if is_root {
            (
                "javascript".to_string(),
                "JavaScript tests".to_string(),
                vec![executable.to_string(), "test".to_string()],
            )
        } else {
            let command = match executable {
                "pnpm" => vec![
                    "pnpm".into(),
                    "--dir".into(),
                    rel_dir.clone(),
                    "test".into(),
                ],
                "yarn" => vec![
                    "yarn".into(),
                    "--cwd".into(),
                    rel_dir.clone(),
                    "test".into(),
                ],
                _ => vec![
                    "npm".into(),
                    "--prefix".into(),
                    rel_dir.clone(),
                    "test".into(),
                ],
            };
            (
                suite_id(&format!("javascript-{rel_dir}")),
                format!("JavaScript tests ({rel_dir})"),
                command,
            )
        };
        out.push(Candidate {
            id,
            path: relative(workspace, manifest_path),
            source: "nested-package-json".into(),
            name,
            command,
            classification: "atomic".into(),
            confidence: "high".into(),
            disruptive: false,
            detail: format!("package.json test script in {rel_dir}"),
            covers: Vec::new(),
            requirements: Requirements {
                executables: vec![executable.into()],
                files: vec![relative(workspace, manifest_path)],
                ..Requirements::default()
            },
            timeout_seconds: None,
        });
    }
    out
}

// ---------------------------------------------------------------------------
// Python and Go manifests at nested project roots
// ---------------------------------------------------------------------------

fn python_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    const MANIFESTS: &[&str] = &["pyproject.toml", "pytest.ini", "tox.ini"];
    let mut dirs: Vec<PathBuf> = Vec::new();
    for name in MANIFESTS {
        for path in files
            .iter()
            .filter(|path| path.file_name().and_then(|n| n.to_str()) == Some(*name))
        {
            let dir = path.parent().unwrap_or(workspace).to_path_buf();
            if !dirs.contains(&dir) {
                dirs.push(dir);
            }
        }
    }
    dirs.into_iter()
        .map(|dir| {
            let is_root = dir == workspace;
            let manifest_name = MANIFESTS
                .iter()
                .find(|name| dir.join(name).is_file())
                .copied()
                .unwrap_or("pyproject.toml");
            let rel_manifest = relative(workspace, &dir.join(manifest_name));
            let rel_dir = relative(workspace, &dir);
            let (id, name, command) = if is_root {
                (
                    "python".to_string(),
                    "Python tests".to_string(),
                    vec!["python3".into(), "-m".into(), "pytest".into()],
                )
            } else {
                (
                    suite_id(&format!("python-{rel_dir}")),
                    format!("Python tests ({rel_dir})"),
                    vec![
                        "python3".into(),
                        "-m".into(),
                        "pytest".into(),
                        rel_dir.clone(),
                    ],
                )
            };
            Candidate {
                id,
                path: rel_manifest.clone(),
                source: "python-manifest".into(),
                name,
                command,
                classification: "atomic".into(),
                confidence: "high".into(),
                disruptive: false,
                detail: format!("Python manifest at {rel_manifest}"),
                covers: Vec::new(),
                requirements: Requirements {
                    executables: vec!["python3".into()],
                    files: vec![rel_manifest],
                    ..Requirements::default()
                },
                timeout_seconds: None,
            }
        })
        .collect()
}

fn go_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    files
        .iter()
        .filter(|path| path.file_name().and_then(|n| n.to_str()) == Some("go.mod"))
        .map(|path| {
            let dir = path.parent().unwrap_or(workspace);
            let is_root = dir == workspace;
            let rel = relative(workspace, path);
            let rel_dir = relative(workspace, dir);
            let (id, name, command) = if is_root {
                (
                    "go".to_string(),
                    "Go tests".to_string(),
                    vec!["go".into(), "test".into(), "./...".into()],
                )
            } else {
                (
                    suite_id(&format!("go-{rel_dir}")),
                    format!("Go tests ({rel_dir})"),
                    vec!["go".into(), "test".into(), format!("./{rel_dir}/...")],
                )
            };
            Candidate {
                id,
                path: rel.clone(),
                source: "go-manifest".into(),
                name,
                command,
                classification: "atomic".into(),
                confidence: "high".into(),
                disruptive: false,
                detail: format!("Go module at {rel}"),
                covers: Vec::new(),
                requirements: Requirements {
                    executables: vec!["go".into()],
                    files: vec![rel],
                    ..Requirements::default()
                },
                timeout_seconds: None,
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Gradle wrappers (root, Android, or any other nested module)
// ---------------------------------------------------------------------------

fn gradle_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    files
        .iter()
        .filter(|path| path.file_name().and_then(|n| n.to_str()) == Some("gradlew"))
        .map(|wrapper| {
            let dir = wrapper.parent().unwrap_or(workspace);
            let rel_wrapper = relative(workspace, wrapper);
            let rel_dir = relative(workspace, dir);
            let is_root = dir == workspace;
            let android = rel_wrapper.to_ascii_lowercase().contains("android");
            let (id, name, command) = if android {
                (
                    "android".to_string(),
                    "Android tests".to_string(),
                    vec![
                        format!("./{rel_wrapper}"),
                        "-p".into(),
                        rel_dir.clone(),
                        "test".into(),
                    ],
                )
            } else if is_root {
                (
                    "gradle".to_string(),
                    "Gradle tests".to_string(),
                    vec!["./gradlew".into(), "test".into()],
                )
            } else {
                (
                    suite_id(&format!("gradle-{rel_dir}")),
                    format!("Gradle tests ({rel_dir})"),
                    vec![
                        format!("./{rel_wrapper}"),
                        "-p".into(),
                        rel_dir.clone(),
                        "test".into(),
                    ],
                )
            };
            Candidate {
                id,
                path: rel_wrapper.clone(),
                source: "gradle-wrapper".into(),
                name,
                command,
                classification: "atomic".into(),
                confidence: "high".into(),
                disruptive: false,
                detail: format!("Gradle wrapper at {rel_wrapper}"),
                covers: Vec::new(),
                requirements: Requirements {
                    executables: vec!["java".into()],
                    files: vec![rel_wrapper],
                    ..Requirements::default()
                },
                timeout_seconds: Some(3600),
            }
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Conventional test-script directories (scripts/tests, e2e, uat, ...)
// ---------------------------------------------------------------------------

fn is_under_conventional_test_dir(relative_path: &str) -> bool {
    Path::new(relative_path).components().any(|component| {
        let name = component.as_os_str().to_string_lossy().to_ascii_lowercase();
        CONVENTIONAL_TEST_DIR_NAMES.contains(&name.as_str())
    })
}

fn script_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    let scripts: Vec<&PathBuf> = files
        .iter()
        .filter(|path| {
            let rel = relative(workspace, path);
            if !is_under_conventional_test_dir(&rel) {
                return false;
            }
            let extension = path.extension().and_then(|ext| ext.to_str()).unwrap_or("");
            SCRIPT_EXTENSIONS.contains(&extension) || is_executable(path)
        })
        .collect();

    let entries: Vec<(String, String, String)> = scripts
        .iter()
        .map(|path| {
            let rel = relative(workspace, path);
            let filename = path
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default();
            let content = fs::read_to_string(path).unwrap_or_default();
            (rel, filename, content)
        })
        .collect();

    let mut out = Vec::new();
    for (rel, filename, content) in &entries {
        let stem = Path::new(filename)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        let lower_content = content.to_ascii_lowercase();

        let mut covers: Vec<String> = entries
            .iter()
            .filter(|(other_rel, other_filename, _)| {
                other_rel != rel && content.contains(other_filename.as_str())
            })
            .map(|(other_rel, _, _)| other_rel.clone())
            .collect();
        let invokes_cargo_test = lower_content.contains("cargo test");
        if invokes_cargo_test {
            covers.push("Cargo.toml".to_string());
        }

        let is_tv = stem.starts_with("tv_");
        let disruptive = stem.contains("resilience") || stem.contains("disruptive");

        let (classification, confidence, detail): (String, String, String) = if stem
            .contains("cron")
        {
            (
                "helper".into(),
                "high".into(),
                if covers.is_empty() {
                    "Scheduler wrapper; the invoked test command could not be identified".into()
                } else {
                    format!(
                        "Scheduler wrapper around {}; not scheduled separately",
                        covers.join(", ")
                    )
                },
            )
        } else if covers.len() > 1 {
            (
                "aggregate".into(),
                "high".into(),
                format!("Aggregates: {}", covers.join(", ")),
            )
        } else if covers.len() == 1 {
            (
                "alias".into(),
                "high".into(),
                format!("Overlaps assertions already run by {}", covers[0]),
            )
        } else if stem.starts_with("test_") {
            (
                "helper".into(),
                "high".into(),
                "Repository self/integration check".into(),
            )
        } else if stem.ends_with("_suite") || stem.ends_with("_tests") {
            (
                "atomic".into(),
                "high".into(),
                "Conventional test-script naming".into(),
            )
        } else {
            (
                    "unknown".into(),
                    "low".into(),
                    "Executable script under a conventional test directory; purpose could not be determined automatically".into(),
                )
        };

        let mut requirements = Requirements {
            files: vec![rel.clone()],
            ..Requirements::default()
        };
        if is_tv {
            requirements.executables.push("adb".into());
            requirements.devices.push(DeviceRequirement {
                device_type: "fireTv".into(),
                input: "fireTvSerial".into(),
                argument: "--device".into(),
            });
        }
        out.push(Candidate {
            id: suite_id(&stem),
            path: rel.clone(),
            source: "test-script".into(),
            name: suite_name(&stem),
            command: vec!["bash".into(), rel.clone()],
            classification,
            confidence,
            disruptive,
            detail,
            covers,
            requirements,
            timeout_seconds: if is_tv { Some(7200) } else { None },
        });
    }
    out
}

// ---------------------------------------------------------------------------
// Makefile / Justfile / Taskfile "test" targets
// ---------------------------------------------------------------------------

fn has_make_target(content: &str, name: &str) -> bool {
    let needle = format!("{name}:");
    content
        .lines()
        .any(|line| line.trim_start().starts_with(&needle))
}

fn has_yaml_task(content: &str, name: &str) -> bool {
    let needle = format!("{name}:");
    let mut in_tasks = false;
    for line in content.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("tasks:") {
            in_tasks = true;
            continue;
        }
        if !in_tasks {
            continue;
        }
        if !line.starts_with(' ') && !line.starts_with('\t') && !trimmed.is_empty() {
            in_tasks = false;
            continue;
        }
        if trimmed.starts_with(&needle) {
            return true;
        }
    }
    false
}

fn task_runner_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    let mut out = Vec::new();
    for path in files {
        let Some(filename) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        let rel = relative(workspace, path);
        let dir = path.parent().unwrap_or(workspace);
        let is_root = dir == workspace;
        let content = fs::read_to_string(path).unwrap_or_default();
        let (source, command): (&str, Vec<String>) = match filename {
            "Makefile" | "makefile" if has_make_target(&content, "test") => (
                "makefile-target",
                if is_root {
                    vec!["make".into(), "test".into()]
                } else {
                    vec![
                        "make".into(),
                        "-C".into(),
                        relative(workspace, dir),
                        "test".into(),
                    ]
                },
            ),
            "Justfile" | "justfile" if has_make_target(&content, "test") => (
                "justfile-target",
                if is_root {
                    vec!["just".into(), "test".into()]
                } else {
                    vec![
                        "just".into(),
                        "--justfile".into(),
                        rel.clone(),
                        "test".into(),
                    ]
                },
            ),
            "Taskfile.yml" | "Taskfile.yaml" if has_yaml_task(&content, "test") => (
                "taskfile-target",
                if is_root {
                    vec!["task".into(), "test".into()]
                } else {
                    vec![
                        "task".into(),
                        "--taskfile".into(),
                        rel.clone(),
                        "test".into(),
                    ]
                },
            ),
            _ => continue,
        };
        let id = if is_root {
            source.trim_end_matches("-target").to_string()
        } else {
            suite_id(&format!("{source}-{rel}"))
        };
        out.push(Candidate {
            id,
            path: rel.clone(),
            source: source.into(),
            name: format!(
                "{} test target ({rel})",
                suite_name(source.trim_end_matches("-target"))
            ),
            command,
            classification: "atomic".into(),
            confidence: "high".into(),
            disruptive: false,
            detail: format!("`test` target declared in {rel}"),
            covers: Vec::new(),
            requirements: Requirements {
                files: vec![rel],
                ..Requirements::default()
            },
            timeout_seconds: None,
        });
    }
    out
}

// ---------------------------------------------------------------------------
// GitHub Actions workflow test commands
// ---------------------------------------------------------------------------

fn split_command_segments(line: &str) -> Vec<String> {
    line.split("&&")
        .flat_map(|segment| segment.split(';'))
        .map(|segment| segment.trim().to_string())
        .filter(|segment| !segment.is_empty())
        .collect()
}

fn extract_run_commands(content: &str) -> Vec<String> {
    let mut commands = Vec::new();
    let lines: Vec<&str> = content.lines().collect();
    let mut index = 0;
    while index < lines.len() {
        let line = lines[index];
        let trimmed = line.trim_start();
        let indent = line.len() - trimmed.len();
        // A step's `run:` key is commonly written as a YAML sequence item
        // (`- run: ...`); strip that marker before matching the key itself.
        let keyed = trimmed.strip_prefix("- ").map_or(trimmed, str::trim_start);
        if let Some(rest) = keyed.strip_prefix("run:") {
            let rest = rest.trim();
            if rest == "|" || rest == "|-" || rest == ">" {
                let mut cursor = index + 1;
                while cursor < lines.len() {
                    let next = lines[cursor];
                    let next_trimmed = next.trim_start();
                    if next_trimmed.is_empty() {
                        cursor += 1;
                        continue;
                    }
                    let next_indent = next.len() - next_trimmed.len();
                    if next_indent <= indent {
                        break;
                    }
                    commands.extend(split_command_segments(next_trimmed));
                    cursor += 1;
                }
                index = cursor;
                continue;
            } else if !rest.is_empty() {
                commands.extend(split_command_segments(rest));
            }
        }
        index += 1;
    }
    commands
}

fn looks_like_test_command(command: &str) -> bool {
    const MARKERS: &[&str] = &[
        "cargo test",
        "npm test",
        "npm run test",
        "yarn test",
        "pnpm test",
        "pytest",
        "go test",
        "gradlew test",
        "gradle test",
        "unittest",
    ];
    let lower = command.to_ascii_lowercase();
    MARKERS.iter().any(|marker| lower.contains(marker))
}

fn shell_tokenize(command: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut quote: Option<char> = None;
    for character in command.chars() {
        match quote {
            Some(active) if character == active => quote = None,
            Some(_) => current.push(character),
            None if character == '"' || character == '\'' => quote = Some(character),
            None if character.is_whitespace() => {
                if !current.is_empty() {
                    tokens.push(std::mem::take(&mut current));
                }
            }
            None => current.push(character),
        }
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    tokens
}

fn ci_workflow_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    let mut out = Vec::new();
    for path in files.iter().filter(|path| {
        let rel = relative(workspace, path);
        rel.starts_with(".github/workflows/")
            && matches!(
                path.extension().and_then(|ext| ext.to_str()),
                Some("yml") | Some("yaml")
            )
    }) {
        let rel = relative(workspace, path);
        let content = fs::read_to_string(path).unwrap_or_default();
        for command_line in extract_run_commands(&content) {
            if !looks_like_test_command(&command_line) {
                continue;
            }
            let argv = shell_tokenize(&command_line);
            if argv.is_empty() {
                continue;
            }
            out.push(Candidate {
                id: suite_id(&format!("ci-{rel}-{command_line}")),
                path: rel.clone(),
                source: "ci-workflow".into(),
                name: format!("CI: {command_line}"),
                command: argv,
                classification: "atomic".into(),
                confidence: "low".into(),
                disruptive: false,
                detail: format!(
                    "Reconstructed from a `run:` step in {rel}; review before enabling"
                ),
                covers: Vec::new(),
                requirements: Requirements::default(),
                timeout_seconds: None,
            });
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Repository-provided machine-readable descriptors
// ---------------------------------------------------------------------------

fn descriptor_candidates(workspace: &Path, files: &[PathBuf]) -> Vec<Candidate> {
    let mut out = Vec::new();
    for path in files
        .iter()
        .filter(|path| path.file_name().and_then(|n| n.to_str()) == Some("swarm-test.json"))
    {
        let rel = relative(workspace, path);
        let Ok(raw) = fs::read_to_string(path) else {
            continue;
        };
        let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) else {
            continue;
        };
        let Some(command) = value.get("command").and_then(|c| c.as_array()) else {
            continue;
        };
        let command: Vec<String> = command
            .iter()
            .filter_map(|part| part.as_str().map(str::to_string))
            .collect();
        if command.is_empty() {
            continue;
        }
        let name = value
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("Declared test")
            .to_string();
        let classification = value
            .get("classification")
            .and_then(|c| c.as_str())
            .unwrap_or("atomic")
            .to_string();
        let disruptive = value
            .get("disruptive")
            .and_then(|d| d.as_bool())
            .unwrap_or(false);
        out.push(Candidate {
            id: suite_id(&format!("descriptor-{rel}")),
            path: rel.clone(),
            source: "repository-descriptor".into(),
            name,
            command,
            classification,
            confidence: "declared".into(),
            disruptive,
            detail: format!("Declared by {rel}"),
            covers: Vec::new(),
            requirements: Requirements::default(),
            timeout_seconds: None,
        });
    }
    out
}

// ---------------------------------------------------------------------------
// Cross-source deduplication and stable id assignment
// ---------------------------------------------------------------------------

/// When two candidates from different sources resolved to the exact same
/// command (e.g. a CI step running the same invocation as a manifest-derived
/// suite), keep the more trustworthy source as canonical and mark the other
/// an alias so it is never scheduled twice.
fn dedup_exact_command_aliases(candidates: &mut [Candidate]) {
    fn priority(source: &str) -> u8 {
        match source {
            "repository-descriptor" => 0,
            "cargo-manifest"
            | "cargo-workspace-member"
            | "nested-package-json"
            | "gradle-wrapper"
            | "python-manifest"
            | "go-manifest" => 1,
            "makefile-target" | "justfile-target" | "taskfile-target" => 2,
            "test-script" => 3,
            "ci-workflow" => 4,
            _ => 5,
        }
    }
    let commands: Vec<(usize, Vec<String>, u8)> = candidates
        .iter()
        .enumerate()
        .filter(|(_, candidate)| !candidate.command.is_empty())
        .map(|(index, candidate)| {
            (
                index,
                candidate.command.clone(),
                priority(&candidate.source),
            )
        })
        .collect();
    for index in 0..candidates.len() {
        if candidates[index].command.is_empty() || candidates[index].classification == "alias" {
            continue;
        }
        let self_priority = priority(&candidates[index].source);
        for &(other_index, ref other_command, other_priority) in &commands {
            if other_index == index
                || other_command != &candidates[index].command
                || other_priority >= self_priority
            {
                continue;
            }
            let canonical_path = candidates[other_index].path.clone();
            let canonical_name = candidates[other_index].name.clone();
            candidates[index].classification = "alias".into();
            candidates[index].confidence = "high".into();
            if candidates[index].covers.is_empty() {
                candidates[index].covers = vec![canonical_path];
            }
            candidates[index].detail =
                format!("Duplicate of {canonical_name}; not scheduled separately");
            break;
        }
    }
}

fn finalize_ids(candidates: &mut [Candidate]) {
    let mut seen = HashSet::new();
    for candidate in candidates.iter_mut() {
        if candidate.id.trim().is_empty() {
            candidate.id = suite_id(&candidate.name);
        }
        let mut id = candidate.id.clone();
        while !seen.insert(id.clone()) {
            id = format!("{id}-2");
        }
        candidate.id = id;
    }
}

// ---------------------------------------------------------------------------
// Coverage audit: candidates vs. the committed test definition
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CoverageAuditEntry {
    pub id: String,
    pub path: String,
    pub name: String,
    pub source: String,
    pub classification: String,
    pub confidence: String,
    pub detail: String,
    /// The committed suite id or covering-candidate description this entry
    /// resolved against, when applicable.
    pub mapped_to: String,
}

#[derive(Debug, Clone, Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct CoverageAudit {
    pub mapped_scheduled: Vec<CoverageAuditEntry>,
    pub mapped_covered: Vec<CoverageAuditEntry>,
    pub disabled_pending_review: Vec<CoverageAuditEntry>,
    pub unmapped: Vec<CoverageAuditEntry>,
    /// False whenever `unmapped` is non-empty — the UI must not claim
    /// complete coverage while any candidate remains unaccounted for.
    pub complete: bool,
}

fn matches_suite(candidate: &Candidate, suite: &TestSuiteDefinition) -> bool {
    if !candidate.command.is_empty() && candidate.command == suite.command {
        return true;
    }
    if candidate.path.is_empty() {
        return false;
    }
    suite
        .requirements
        .files
        .iter()
        .any(|file| file == &candidate.path)
        || suite.command.iter().any(|argument| {
            argument == &candidate.path || argument.ends_with(&format!("/{}", candidate.path))
        })
}

fn suite_covers(suite: &TestSuiteDefinition, candidate: &Candidate) -> bool {
    suite.covers.iter().any(|entry| {
        entry == &candidate.path
            || (!candidate.command.is_empty() && *entry == candidate.command.join(" "))
            || (!candidate.path.is_empty() && entry.contains(candidate.path.as_str()))
    })
}

#[derive(Clone)]
enum Bucket {
    Scheduled(String),
    Covered(String),
    PendingReview(String),
    Unmapped,
}

/// Compares recursively discovered candidates against the repository's
/// committed test definition so the UI can show, per candidate, whether it
/// is scheduled, covered by another suite, disabled pending review, or
/// entirely unmapped. Never modifies the committed definition.
pub fn audit_coverage(candidates: &[Candidate], suites: &[TestSuiteDefinition]) -> CoverageAudit {
    let mut buckets: std::collections::HashMap<String, Bucket> = std::collections::HashMap::new();
    for candidate in candidates {
        let mut found = None;
        for suite in suites {
            if matches_suite(candidate, suite) {
                found = Some(if suite.enabled {
                    Bucket::Scheduled(suite.id.clone())
                } else {
                    Bucket::PendingReview(suite.id.clone())
                });
                break;
            }
        }
        if found.is_none() {
            for suite in suites {
                if suite_covers(suite, candidate) {
                    found = Some(Bucket::Covered(format!("suite:{}", suite.id)));
                    break;
                }
            }
        }
        buckets.insert(candidate.id.clone(), found.unwrap_or(Bucket::Unmapped));
    }

    // Propagate candidate-to-candidate `covers` relationships (aggregates,
    // aliases, workspace members) through a few relaxation passes so shallow
    // chains (member -> workspace, wrapper -> aggregate -> leaf) converge.
    for _ in 0..3 {
        let snapshot: std::collections::HashMap<String, Bucket> = buckets.clone();
        for candidate in candidates {
            if !matches!(buckets.get(&candidate.id), Some(Bucket::Unmapped)) {
                continue;
            }
            if let Some(coverer) = candidates
                .iter()
                .find(|other| other.covers.iter().any(|path| path == &candidate.path))
            {
                if !matches!(snapshot.get(&coverer.id), Some(Bucket::Unmapped) | None) {
                    buckets.insert(
                        candidate.id.clone(),
                        Bucket::Covered(format!("candidate:{}", coverer.name)),
                    );
                    continue;
                }
            }
            if !candidate.covers.is_empty() {
                let all_accounted = candidate.covers.iter().all(|path| {
                    candidates
                        .iter()
                        .find(|other| &other.path == path)
                        .is_none_or(|other| {
                            !matches!(snapshot.get(&other.id), Some(Bucket::Unmapped) | None)
                        })
                });
                if all_accounted {
                    buckets.insert(
                        candidate.id.clone(),
                        Bucket::Covered(format!("aggregates:{}", candidate.covers.join(", "))),
                    );
                }
            }
        }
    }

    let mut audit = CoverageAudit {
        complete: false,
        ..CoverageAudit::default()
    };
    for candidate in candidates {
        let entry_base = CoverageAuditEntry {
            id: candidate.id.clone(),
            path: candidate.path.clone(),
            name: candidate.name.clone(),
            source: candidate.source.clone(),
            classification: candidate.classification.clone(),
            confidence: candidate.confidence.clone(),
            detail: candidate.detail.clone(),
            mapped_to: String::new(),
        };
        match buckets.get(&candidate.id) {
            Some(Bucket::Scheduled(id)) => {
                audit.mapped_scheduled.push(CoverageAuditEntry {
                    mapped_to: id.clone(),
                    ..entry_base
                });
            }
            Some(Bucket::Covered(detail)) => {
                audit.mapped_covered.push(CoverageAuditEntry {
                    mapped_to: detail.clone(),
                    ..entry_base
                });
            }
            Some(Bucket::PendingReview(id)) => {
                audit.disabled_pending_review.push(CoverageAuditEntry {
                    mapped_to: id.clone(),
                    ..entry_base
                });
            }
            _ => audit.unmapped.push(entry_base),
        }
    }
    audit.complete = audit.unmapped.is_empty();
    audit
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn write(workspace: &Path, relative: &str, content: &str) {
        let path = workspace.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, content).unwrap();
    }

    #[cfg(unix)]
    fn make_executable(workspace: &Path, relative: &str) {
        use std::os::unix::fs::PermissionsExt;
        let path = workspace.join(relative);
        let mut permissions = fs::metadata(&path).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&path, permissions).unwrap();
    }

    #[test]
    fn excludes_dependency_and_generated_output_directories() {
        let workspace = tempdir().unwrap();
        write(workspace.path(), "Cargo.toml", "[package]\nname = \"x\"\n");
        write(
            workspace.path(),
            "node_modules/pkg/package.json",
            r#"{"scripts":{"test":"jest"}}"#,
        );
        write(
            workspace.path(),
            "target/debug/Cargo.toml",
            "[package]\nname = \"generated\"\n",
        );
        write(
            workspace.path(),
            ".git/hooks/Cargo.toml",
            "[package]\nname = \"vcs\"\n",
        );
        let candidates = discover_candidates(workspace.path()).unwrap();
        assert!(candidates.iter().all(|c| !c.path.contains("node_modules")));
        assert!(candidates.iter().all(|c| !c.path.contains("target/")));
        assert!(candidates.iter().all(|c| !c.path.contains(".git/")));
    }

    #[test]
    fn finds_nested_cargo_workspace_and_roku_package_json() {
        let workspace = tempdir().unwrap();
        write(
            workspace.path(),
            "Cargo.toml",
            "[workspace]\nmembers = [\"crates/*\"]\n",
        );
        write(
            workspace.path(),
            "crates/server/Cargo.toml",
            "[package]\nname = \"server\"\n",
        );
        write(
            workspace.path(),
            "clients/roku/package.json",
            r#"{"scripts":{"test":"roku-test"}}"#,
        );
        let candidates = discover_candidates(workspace.path()).unwrap();
        let root = candidates
            .iter()
            .find(|c| c.source == "cargo-manifest")
            .unwrap();
        assert_eq!(root.command, ["cargo", "test", "--workspace"]);
        let member = candidates
            .iter()
            .find(|c| c.source == "cargo-workspace-member")
            .unwrap();
        assert_eq!(member.classification, "helper");
        assert!(root.covers.contains(&member.path));
        let roku = candidates
            .iter()
            .find(|c| c.source == "nested-package-json")
            .unwrap();
        assert_eq!(roku.classification, "atomic");
        assert!(roku.command.iter().any(|arg| arg.contains("clients/roku")));
    }

    #[test]
    fn extracts_ci_workflow_test_commands() {
        let workspace = tempdir().unwrap();
        write(
            workspace.path(),
            ".github/workflows/ci.yml",
            "jobs:\n  build:\n    steps:\n      - run: cargo build\n      - run: |\n          echo hi\n          cargo test --workspace\n",
        );
        let candidates = discover_candidates(workspace.path()).unwrap();
        let ci = candidates.iter().find(|c| c.source == "ci-workflow");
        let ci = ci.expect("a cargo test step should be extracted");
        assert_eq!(ci.command, ["cargo", "test", "--workspace"]);
        assert_eq!(ci.confidence, "low");
        assert!(!candidates
            .iter()
            .any(|c| c.source == "ci-workflow" && c.command.contains(&"build".to_string())));
    }

    #[test]
    fn classifies_aggregate_wrapper_and_self_test_scripts() {
        let workspace = tempdir().unwrap();
        write(
            workspace.path(),
            "Cargo.toml",
            "[package]\nname = \"backend\"\n",
        );
        write(
            workspace.path(),
            "scripts/tests/media_server_uat_tests.sh",
            "#!/bin/sh\ncargo test -p media-server --test uat\n",
        );
        write(
            workspace.path(),
            "scripts/tests/tv_e2e_suite.sh",
            "#!/bin/sh\necho e2e\n",
        );
        write(
            workspace.path(),
            "scripts/tests/full_uat_suite.sh",
            "#!/bin/sh\ncargo test\nbash scripts/tests/media_server_uat_tests.sh\nbash scripts/tests/tv_e2e_suite.sh\n",
        );
        write(
            workspace.path(),
            "scripts/tests/full_uat_cron.sh",
            "#!/bin/sh\nwhile true; do ./full_uat_suite.sh; sleep 3600; done\n",
        );
        write(
            workspace.path(),
            "scripts/tests/test_worker_self_check.sh",
            "#!/bin/sh\necho self-check\n",
        );
        #[cfg(unix)]
        for script in [
            "scripts/tests/media_server_uat_tests.sh",
            "scripts/tests/tv_e2e_suite.sh",
            "scripts/tests/full_uat_suite.sh",
            "scripts/tests/full_uat_cron.sh",
            "scripts/tests/test_worker_self_check.sh",
        ] {
            make_executable(workspace.path(), script);
        }

        let candidates = discover_candidates(workspace.path()).unwrap();
        let by_stem = |stem: &str| {
            candidates
                .iter()
                .find(|c| c.path.ends_with(stem))
                .unwrap_or_else(|| panic!("missing candidate for {stem}"))
        };

        let media_server = by_stem("media_server_uat_tests.sh");
        assert_eq!(media_server.classification, "alias");
        assert!(media_server.covers.contains(&"Cargo.toml".to_string()));

        let e2e = by_stem("tv_e2e_suite.sh");
        assert_eq!(e2e.classification, "atomic");

        let full_suite = by_stem("full_uat_suite.sh");
        assert_eq!(full_suite.classification, "aggregate");
        assert!(full_suite
            .covers
            .iter()
            .any(|c| c.ends_with("media_server_uat_tests.sh")));
        assert!(full_suite
            .covers
            .iter()
            .any(|c| c.ends_with("tv_e2e_suite.sh")));

        let cron = by_stem("full_uat_cron.sh");
        assert_eq!(cron.classification, "helper");
        assert!(cron.detail.contains("Scheduler wrapper"));
        assert!(cron.covers.iter().any(|c| c.ends_with("full_uat_suite.sh")));

        let self_check = by_stem("test_worker_self_check.sh");
        assert_eq!(self_check.classification, "helper");
        assert!(self_check.detail.contains("self/integration"));
    }

    #[test]
    fn unrecognized_scripts_are_unknown_and_low_confidence() {
        let workspace = tempdir().unwrap();
        write(workspace.path(), "e2e/mystery.sh", "#!/bin/sh\necho ?\n");
        #[cfg(unix)]
        make_executable(workspace.path(), "e2e/mystery.sh");
        let candidates = discover_candidates(workspace.path()).unwrap();
        let mystery = candidates
            .iter()
            .find(|c| c.path.ends_with("mystery.sh"))
            .unwrap();
        assert_eq!(mystery.classification, "unknown");
        assert_eq!(mystery.confidence, "low");
    }

    #[test]
    fn deduplicates_exact_command_matches_across_sources() {
        let workspace = tempdir().unwrap();
        write(workspace.path(), "Cargo.toml", "[package]\nname = \"x\"\n");
        write(
            workspace.path(),
            ".github/workflows/ci.yml",
            "jobs:\n  build:\n    steps:\n      - run: cargo test\n",
        );
        let candidates = discover_candidates(workspace.path()).unwrap();
        let ci = candidates
            .iter()
            .find(|c| c.source == "ci-workflow")
            .unwrap();
        assert_eq!(ci.classification, "alias");
        assert!(ci.covers.contains(&"Cargo.toml".to_string()));
    }

    #[test]
    fn repository_descriptor_takes_precedence_and_is_declared_confidence() {
        let workspace = tempdir().unwrap();
        write(
            workspace.path(),
            "clients/roku/swarm-test.json",
            r#"{"name":"Roku tests","command":["bin/roku-test"],"classification":"atomic"}"#,
        );
        let candidates = discover_candidates(workspace.path()).unwrap();
        let descriptor = candidates
            .iter()
            .find(|c| c.source == "repository-descriptor")
            .unwrap();
        assert_eq!(descriptor.confidence, "declared");
        assert_eq!(descriptor.command, ["bin/roku-test"]);
    }

    #[test]
    fn extracts_makefile_test_targets() {
        let workspace = tempdir().unwrap();
        write(
            workspace.path(),
            "Makefile",
            "build:\n\t@echo build\n\ntest: build\n\t@echo test\n",
        );
        let candidates = discover_candidates(workspace.path()).unwrap();
        let make = candidates
            .iter()
            .find(|c| c.source == "makefile-target")
            .unwrap();
        assert_eq!(make.command, ["make", "test"]);
        assert_eq!(make.confidence, "high");
    }

    fn suite(id: &str, command: &[&str], enabled: bool) -> TestSuiteDefinition {
        TestSuiteDefinition {
            id: id.into(),
            name: id.into(),
            command: command.iter().map(|s| s.to_string()).collect(),
            timeout_seconds: 60,
            disruptive: false,
            enabled,
            requirements: Requirements::default(),
            covers: Vec::new(),
        }
    }

    #[test]
    fn audit_buckets_scheduled_covered_pending_and_unmapped_candidates() {
        let scheduled = Candidate {
            id: "rust".into(),
            path: "Cargo.toml".into(),
            source: "cargo-manifest".into(),
            name: "Rust tests".into(),
            command: vec!["cargo".into(), "test".into()],
            classification: "atomic".into(),
            confidence: "high".into(),
            disruptive: false,
            detail: String::new(),
            covers: vec!["scripts/tests/media_server_uat_tests.sh".into()],
            requirements: Requirements::default(),
            timeout_seconds: None,
        };
        let covered = Candidate {
            id: "media-server-uat".into(),
            path: "scripts/tests/media_server_uat_tests.sh".into(),
            source: "test-script".into(),
            name: "Media Server Uat Tests".into(),
            command: vec![
                "bash".into(),
                "scripts/tests/media_server_uat_tests.sh".into(),
            ],
            classification: "alias".into(),
            confidence: "high".into(),
            disruptive: false,
            detail: String::new(),
            covers: vec!["Cargo.toml".into()],
            requirements: Requirements::default(),
            timeout_seconds: None,
        };
        let pending = Candidate {
            id: "unknown-script".into(),
            path: "scripts/tests/mystery.sh".into(),
            source: "test-script".into(),
            name: "Mystery".into(),
            command: vec!["bash".into(), "scripts/tests/mystery.sh".into()],
            classification: "unknown".into(),
            confidence: "low".into(),
            disruptive: false,
            detail: String::new(),
            covers: Vec::new(),
            requirements: Requirements::default(),
            timeout_seconds: None,
        };
        let unmapped = Candidate {
            id: "go".into(),
            path: "go.mod".into(),
            source: "go-manifest".into(),
            name: "Go tests".into(),
            command: vec!["go".into(), "test".into(), "./...".into()],
            classification: "atomic".into(),
            confidence: "high".into(),
            disruptive: false,
            detail: String::new(),
            covers: Vec::new(),
            requirements: Requirements::default(),
            timeout_seconds: None,
        };
        let candidates = vec![scheduled, covered, pending.clone(), unmapped];
        let suites = vec![
            suite("rust", &["cargo", "test"], true),
            suite("mystery", &["bash", "scripts/tests/mystery.sh"], false),
        ];
        let audit = audit_coverage(&candidates, &suites);
        assert_eq!(audit.mapped_scheduled.len(), 1);
        assert_eq!(audit.mapped_scheduled[0].id, "rust");
        assert_eq!(audit.mapped_covered.len(), 1);
        assert_eq!(audit.mapped_covered[0].id, "media-server-uat");
        assert_eq!(audit.disabled_pending_review.len(), 1);
        assert_eq!(audit.disabled_pending_review[0].id, "unknown-script");
        assert_eq!(audit.unmapped.len(), 1);
        assert_eq!(audit.unmapped[0].id, "go");
        assert!(!audit.complete);
    }

    #[test]
    fn audit_is_complete_only_when_nothing_is_unmapped() {
        let candidate = Candidate {
            id: "rust".into(),
            path: "Cargo.toml".into(),
            source: "cargo-manifest".into(),
            name: "Rust tests".into(),
            command: vec!["cargo".into(), "test".into()],
            classification: "atomic".into(),
            confidence: "high".into(),
            disruptive: false,
            detail: String::new(),
            covers: Vec::new(),
            requirements: Requirements::default(),
            timeout_seconds: None,
        };
        let complete = audit_coverage(
            std::slice::from_ref(&candidate),
            &[suite("rust", &["cargo", "test"], true)],
        );
        assert!(complete.complete);
        let incomplete = audit_coverage(&[candidate], &[]);
        assert!(!incomplete.complete);
        assert_eq!(incomplete.unmapped.len(), 1);
    }
}
