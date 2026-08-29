use serde::Serialize;
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter};

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProcessStatus {
    pub state: String,
    pub pid: Option<u32>,
    pub started_at: Option<u64>,
    pub exit_code: Option<i32>,
    pub detail: String,
}

impl Default for ProcessStatus {
    fn default() -> Self {
        Self {
            state: "stopped".into(),
            pid: None,
            started_at: None,
            exit_code: None,
            detail: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogEvent {
    pub source: String,
    pub line: String,
    pub stream: String,
    pub timestamp: u64,
}

struct ManagedProcess {
    child: Child,
    paused: bool,
    started_at: u64,
    detail: String,
}

#[derive(Default)]
struct ProcessSlot {
    process: Option<ManagedProcess>,
    last_exit: Option<i32>,
    last_detail: String,
}

#[derive(Default)]
pub struct ProcessManager {
    issue: Mutex<ProcessSlot>,
    uat: Mutex<ProcessSlot>,
    task: Mutex<ProcessSlot>,
}

impl ProcessManager {
    fn slot(&self, name: &str) -> Result<&Mutex<ProcessSlot>, String> {
        match name {
            "issue" => Ok(&self.issue),
            "uat" => Ok(&self.uat),
            "task" => Ok(&self.task),
            _ => Err(format!("Unknown process slot: {name}")),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn spawn(
        &self,
        app: &AppHandle,
        slot_name: &str,
        source: &str,
        program: &Path,
        arguments: &[String],
        environment: &[(String, String)],
        working_directory: &Path,
        log_path: PathBuf,
    ) -> Result<ProcessStatus, String> {
        let mutex = self.slot(slot_name)?;
        let mut slot = mutex
            .lock()
            .map_err(|_| "Process state lock was poisoned".to_string())?;
        refresh_slot(&mut slot, source, app, &log_path);
        if slot.process.is_some() {
            return Err(format!("{source} is already running."));
        }

        let mut command = Command::new(program);
        command
            .args(arguments)
            .current_dir(working_directory)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .stdin(Stdio::null());
        for (key, value) in environment {
            command.env(key, value);
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            // Put the scheduler and every child it later launches into one
            // process group. Pause/stop then reaches the active AI CLI and
            // its build/test children, not only the sleeping parent.
            command.process_group(0);
        }
        let mut child = command
            .spawn()
            .map_err(|error| format!("Could not start {source}: {error}"))?;
        let pid = child.id();
        let started_at = unix_timestamp();
        let detail = format!("{} {}", program.display(), arguments.join(" "));
        if let Some(stdout) = child.stdout.take() {
            stream_lines(
                app.clone(),
                source.to_string(),
                "stdout",
                stdout,
                log_path.clone(),
            );
        }
        if let Some(stderr) = child.stderr.take() {
            stream_lines(
                app.clone(),
                source.to_string(),
                "stderr",
                stderr,
                log_path.clone(),
            );
        }
        emit_log(
            app,
            &log_path,
            source,
            "system",
            &format!("Started {source} as pid {pid}."),
        );
        slot.last_exit = None;
        slot.last_detail.clear();
        slot.process = Some(ManagedProcess {
            child,
            paused: false,
            started_at,
            detail: detail.clone(),
        });
        Ok(ProcessStatus {
            state: "running".into(),
            pid: Some(pid),
            started_at: Some(started_at),
            exit_code: None,
            detail,
        })
    }

    pub fn status(
        &self,
        app: &AppHandle,
        slot_name: &str,
        source: &str,
        log_path: &Path,
    ) -> Result<ProcessStatus, String> {
        let mut slot = self
            .slot(slot_name)?
            .lock()
            .map_err(|_| "Process state lock was poisoned".to_string())?;
        refresh_slot(&mut slot, source, app, log_path);
        Ok(status_for_slot(&slot))
    }

    pub fn pause(&self, slot_name: &str) -> Result<ProcessStatus, String> {
        self.signal(slot_name, libc::SIGSTOP, true)
    }

    pub fn resume(&self, slot_name: &str) -> Result<ProcessStatus, String> {
        self.signal(slot_name, libc::SIGCONT, false)
    }

    fn signal(&self, slot_name: &str, signal: i32, paused: bool) -> Result<ProcessStatus, String> {
        let mut slot = self
            .slot(slot_name)?
            .lock()
            .map_err(|_| "Process state lock was poisoned".to_string())?;
        let process = slot
            .process
            .as_mut()
            .ok_or_else(|| "The process is not running.".to_string())?;
        signal_group(process.child.id(), signal)?;
        process.paused = paused;
        Ok(status_for_slot(&slot))
    }

    pub fn stop(&self, slot_name: &str) -> Result<ProcessStatus, String> {
        let mut slot = self
            .slot(slot_name)?
            .lock()
            .map_err(|_| "Process state lock was poisoned".to_string())?;
        let Some(mut process) = slot.process.take() else {
            return Ok(status_for_slot(&slot));
        };
        // A stopped process cannot handle SIGTERM until it is resumed.
        if process.paused {
            let _ = signal_group(process.child.id(), libc::SIGCONT);
        }
        let _ = signal_group(process.child.id(), libc::SIGTERM);
        for _ in 0..20 {
            if let Ok(Some(status)) = process.child.try_wait() {
                slot.last_exit = status.code();
                slot.last_detail = process.detail;
                return Ok(status_for_slot(&slot));
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        let _ = signal_group(process.child.id(), libc::SIGKILL);
        let status = process.child.wait().ok();
        slot.last_exit = status.and_then(|status| status.code());
        slot.last_detail = process.detail;
        Ok(status_for_slot(&slot))
    }

    pub fn stop_all(&self) {
        for slot in ["issue", "uat", "task"] {
            let _ = self.stop(slot);
        }
    }
}

fn refresh_slot(slot: &mut ProcessSlot, source: &str, app: &AppHandle, log_path: &Path) {
    let Some(process) = slot.process.as_mut() else {
        return;
    };
    match process.child.try_wait() {
        Ok(Some(status)) => {
            let code = status.code();
            let detail = process.detail.clone();
            emit_log(
                app,
                log_path,
                source,
                "system",
                &format!(
                    "{source} exited with status {}.",
                    code.map_or_else(|| "signal".into(), |code| code.to_string())
                ),
            );
            slot.last_exit = code;
            slot.last_detail = detail;
            slot.process = None;
        }
        Ok(None) => {}
        Err(error) => {
            emit_log(
                app,
                log_path,
                source,
                "system",
                &format!("Could not read process status: {error}"),
            );
        }
    }
}

fn status_for_slot(slot: &ProcessSlot) -> ProcessStatus {
    if let Some(process) = &slot.process {
        ProcessStatus {
            state: if process.paused { "paused" } else { "running" }.into(),
            pid: Some(process.child.id()),
            started_at: Some(process.started_at),
            exit_code: None,
            detail: process.detail.clone(),
        }
    } else {
        ProcessStatus {
            state: "stopped".into(),
            pid: None,
            started_at: None,
            exit_code: slot.last_exit,
            detail: slot.last_detail.clone(),
        }
    }
}

#[cfg(unix)]
fn signal_group(pid: u32, signal: i32) -> Result<(), String> {
    let result = unsafe { libc::kill(-(pid as libc::pid_t), signal) };
    if result == 0 {
        Ok(())
    } else {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            Ok(())
        } else {
            Err(format!("Could not signal process group {pid}: {error}"))
        }
    }
}

#[cfg(not(unix))]
fn signal_group(_pid: u32, _signal: i32) -> Result<(), String> {
    Err("Process pause/resume is currently supported on macOS and Unix hosts.".into())
}

fn stream_lines<R: std::io::Read + Send + 'static>(
    app: AppHandle,
    source: String,
    stream: &'static str,
    reader: R,
    log_path: PathBuf,
) {
    std::thread::spawn(move || {
        for line in BufReader::new(reader).lines().map_while(Result::ok) {
            emit_log(&app, &log_path, &source, stream, &line);
        }
    });
}

fn emit_log(app: &AppHandle, log_path: &Path, source: &str, stream: &str, line: &str) {
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(file, "[{}] [{source}/{stream}] {line}", unix_timestamp());
    }
    let _ = app.emit(
        "automation-log",
        LogEvent {
            source: source.into(),
            line: line.into(),
            stream: stream.into(),
            timestamp: unix_timestamp(),
        },
    );
}

fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
