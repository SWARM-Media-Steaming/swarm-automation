#!/usr/bin/env bash
# Installs the SWARM Automation desktop app as a login-time macOS service.
#
# `run_now.sh` is a dev harness (`npm run dev` -> `tauri dev` -> a *debug*
# binary in ./target/debug). Any `cargo build`/`cargo test` in this workspace
# rebuilds that binary, and `tauri-plugin-single-instance` means the next
# launch silently forwards to whatever stale instance is still alive. This
# script builds a *release* .app, installs it to ~/Applications (outside
# ./target), and registers a LaunchAgent that starts it at login and
# relaunches it only if it *crashes* — Quit from the menu-bar menu still stops
# it.
#
# The installed app and the dev build share
#   ~/Library/Application Support/app.swarm.automation/   (config, checkouts, logs)
#   ~/.local/state/swarm-issue-worker/                    (scheduler state)
# so repositories, provider config, and in-flight work carry over.
#
# Issue workers you already started keep running when the dev app is stopped
# (they are independent processes). The installed app re-adopts a running
# scheduler where it can; if the dashboard shows workers as stopped after the
# handover, start them again from the app UI.
#
# Usage:
#   ./scripts/install_swarm_automation.sh              install or update + restart
#   ./scripts/install_swarm_automation.sh --status     show service state
#   ./scripts/install_swarm_automation.sh --uninstall  remove the service + app
#   ./scripts/install_swarm_automation.sh --uninstall --purge   also delete the data dir
#
# Env vars (all optional):
#   SWARM_APP_DIR   where to install the .app (default ~/Applications)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="SWARM Automation"
BUNDLE_ID="app.swarm.automation"
LABEL="$BUNDLE_ID"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_DIR="${SWARM_APP_DIR:-$HOME/Applications}"
APP_PATH="$APP_DIR/$APP_NAME.app"
EXECUTABLE="$APP_PATH/Contents/MacOS/$APP_NAME"
BUILT_APP="$REPO_ROOT/target/release/bundle/macos/$APP_NAME.app"
DEBUG_BIN="$REPO_ROOT/target/debug/swarm-automation"
OUT_LOG="$HOME/Library/Logs/swarm-automation.out.log"
ERR_LOG="$HOME/Library/Logs/swarm-automation.err.log"
DATA_DIR="$HOME/Library/Application Support/$BUNDLE_ID"
DOMAIN="gui/$(id -u)"

# The app shells out to git / gh / python3 and, through the issue-worker
# scheduler, to the provider CLIs (claude / codex / grok). A LaunchAgent
# inherits only a minimal PATH, so give it the usual locations those live in.
SERVICE_PATH="$HOME/.local/bin:$HOME/.grok/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This installer is macOS-only." >&2
    exit 1
fi

# --- helpers -----------------------------------------------------------

# The app is a tray app that traps SIGTERM to hide to the menu bar instead of
# exiting (same reason closing its window doesn't quit it), so `kill` alone
# leaves it running — escalate to SIGKILL. Matches run_now.sh's kill_orphan_app.
stop_dev_app() {
    local pattern="$1" label="$2" pids
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [ -z "$pids" ] && return 0
    echo "   stopping $label ($(echo "$pids" | tr '\n' ' '))"
    pkill -f "$pattern" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        pgrep -f "$pattern" >/dev/null 2>&1 || return 0
        sleep 0.3
    done
    pkill -9 -f "$pattern" 2>/dev/null || true
    return 0
}

launchctl_stop() {
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null \
        || launchctl unload "$PLIST" 2>/dev/null \
        || true
}

launchctl_start() {
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null \
        || launchctl load "$PLIST" 2>/dev/null \
        || true
    launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
}

service_pid() {
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
        | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\).*/\1/p' \
        | head -n1
}

scheduler_running() {
    pgrep -f 'install_swarm_issue_cron.py' >/dev/null 2>&1
}

# --- subcommands ------------------------------------------------------

do_status() {
    echo "SWARM Automation service status"
    echo "  label:        $LABEL"
    echo "  app:          $APP_PATH"
    if [ -x "$EXECUTABLE" ]; then
        local ver
        ver="$(defaults read "$APP_PATH/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo '?')"
        echo "  installed:    yes (version $ver)"
    else
        echo "  installed:    no"
    fi
    if [ -f "$PLIST" ]; then
        echo "  launchagent:  $PLIST"
        local pid
        pid="$(service_pid || true)"
        if [ -n "${pid:-}" ] && [ "$pid" != "0" ]; then
            echo "  running:      yes (pid $pid)"
            ps -o etime=,command= -p "$pid" 2>/dev/null | cut -c1-150 | sed 's/^/    /'
        else
            echo "  running:      no"
        fi
    else
        echo "  launchagent:  not installed"
    fi
    if scheduler_running; then
        echo "  issue worker: scheduler running (pid $(pgrep -f 'install_swarm_issue_cron.py' | head -n1))"
    else
        echo "  issue worker: scheduler not running"
    fi
    local today_log="$DATA_DIR/logs/automation.log"
    if [ -f "$today_log" ]; then
        echo "  --- last automation log lines ---"
        tail -n 6 "$today_log" 2>/dev/null | cut -c1-150 | sed 's/^/    /'
    fi
}

do_uninstall() {
    echo "==> Stopping and removing the SWARM Automation service ..."
    launchctl_stop
    stop_dev_app "$EXECUTABLE" "installed app"
    if [ -f "$PLIST" ]; then
        rm -f "$PLIST" && echo "   removed $PLIST"
    else
        echo "   no LaunchAgent to remove"
    fi
    if [ -d "$APP_PATH" ]; then
        rm -rf "$APP_PATH" && echo "   removed $APP_PATH"
    else
        echo "   no installed app to remove"
    fi
    if [ "${PURGE:-0}" = "1" ]; then
        rm -rf "$DATA_DIR" && echo "   removed $DATA_DIR"
        echo "   NB: ~/.local/state/swarm-issue-worker/ (scheduler state) left in place"
    else
        echo "   kept $DATA_DIR (config, checkouts) — re-run with --purge to delete it"
    fi
    if scheduler_running; then
        echo "   NB: an issue-worker scheduler is still running; stop it from the app"
        echo "       before uninstall, or: pkill -f install_swarm_issue_cron.py"
    fi
    echo "Done."
}

do_install() {
    echo "==> Preflight ..."
    for tool in cargo node npm; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "Missing required tool: $tool" >&2
            exit 1
        }
    done
    for tool in git gh python3; do
        command -v "$tool" >/dev/null 2>&1 \
            || echo "   warning: '$tool' not on PATH — issue workers need it at runtime"
    done

    local tauri_cli="$REPO_ROOT/node_modules/.bin/tauri"
    if [ ! -x "$tauri_cli" ]; then
        echo "==> Installing npm dependencies (npm ci) ..."
        ( cd "$REPO_ROOT" && npm ci )
    fi

    echo
    echo "==> Building the release app — this takes several minutes ..."
    ( cd "$REPO_ROOT" && "$tauri_cli" build )
    if [ ! -d "$BUILT_APP" ]; then
        echo "Build did not produce $BUILT_APP" >&2
        exit 1
    fi

    echo
    echo "==> Retiring the dev app ..."
    # Kill the debug binary by its absolute path (unambiguous — never matches
    # another checkout or the media server's run_now.sh). `tauri dev` -> cargo
    # notices its child exit and unwinds the whole npm/tauri/run_now.sh chain
    # on its own.
    stop_dev_app "$DEBUG_BIN" 'debug app binary'
    sleep 1

    echo
    echo "==> Installing $APP_NAME.app to $APP_DIR ..."
    mkdir -p "$APP_DIR"
    launchctl_stop
    stop_dev_app "$EXECUTABLE" 'previous installed app'
    rm -rf "$APP_PATH"
    cp -R "$BUILT_APP" "$APP_PATH"
    xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

    echo "==> Writing the LaunchAgent ($PLIST) ..."
    mkdir -p "$(dirname "$PLIST")" "$HOME/Library/Logs"
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>Program</key>
    <string>$EXECUTABLE</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$SERVICE_PATH</string>
        <key>RUST_LOG</key>
        <string>info</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$OUT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$ERR_LOG</string>
</dict>
</plist>
PLIST_EOF

    echo "==> Starting the service ..."
    launchctl_start
    sleep 2

    local pid
    pid="$(service_pid || true)"
    echo
    echo "--------------------------------------------------------------------"
    if [ -n "${pid:-}" ] && [ "$pid" != "0" ]; then
        echo "SWARM Automation is installed and running (pid $pid)."
    else
        echo "SWARM Automation is installed. No pid reported yet — check:"
        echo "   ./scripts/install_swarm_automation.sh --status"
        echo "   tail -f \"$ERR_LOG\""
    fi
    echo
    echo "  App:        $APP_PATH"
    echo "  Dashboard:  click the SWARM Automation tray icon (menu bar)"
    echo "  Data dir:   $DATA_DIR"
    echo "  Logs:       $DATA_DIR/logs/automation.log   |   $ERR_LOG (stderr)"
    if [ -x "$EXECUTABLE" ] && ! pgrep -f "$EXECUTABLE" >/dev/null 2>&1; then
        echo
        echo "  If no tray icon appears in a few seconds, run once:"
        echo "     open \"$APP_PATH\""
    fi
    if scheduler_running; then
        echo
        echo "  An issue-worker scheduler is still running from before. Open the"
        echo "  dashboard and confirm it shows as active; if not, start workers"
        echo "  again from the app."
    fi
    echo
    echo "  Stop the service:   launchctl bootout $DOMAIN/$LABEL"
    echo "  Update after a code change:   ./scripts/install_swarm_automation.sh"
    echo "  Uninstall:          ./scripts/install_swarm_automation.sh --uninstall"
    echo
    echo "  Stop using run_now.sh for the always-on control center — cargo"
    echo "  builds in this repo no longer affect the installed app."
    echo "--------------------------------------------------------------------"
}

# --- arg parsing -----------------------------------------------------

ACTION="install"
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --status)    ACTION="status" ;;
        --uninstall) ACTION="uninstall" ;;
        --purge)     PURGE=1 ;;
        -h|--help)
            sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg (see --help)" >&2
            exit 1
            ;;
    esac
done

case "$ACTION" in
    status)    do_status ;;
    uninstall) do_uninstall ;;
    install)   do_install ;;
esac
