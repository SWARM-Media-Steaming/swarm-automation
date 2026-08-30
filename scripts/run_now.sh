#!/usr/bin/env bash
# Builds and runs the SWARM Automation desktop app from source for manual
# testing — the same thing `npm run dev` does, wrapped so a fresh checkout
# "just works" and so Ctrl+C actually leaves nothing running behind.
#
# Why a script and not just `npm run dev`:
#   - First run on a clean checkout has no node_modules, so `npm run dev`
#     fails with "tauri: command not found". This installs deps first when
#     they're missing.
#   - `tauri dev` hands off to `cargo`, which launches the real app binary
#     (target/debug/swarm-automation). Closing the app window only *hides*
#     it to the menu bar (see README) — it does not exit — so a plain
#     Ctrl+C on `tauri dev` can leave that binary alive and, because the
#     app registers tauri-plugin-single-instance, the *next* run then
#     silently forwards to the orphan instead of starting the code you
#     just changed. cleanup() below kills any leftover app process on exit.
#   - Picks up the rustup toolchain the same way the media server's
#     scripts/run_now.sh does, so a non-login shell still finds cargo.
#
# This app binds no network ports of its own; it only supervises child
# processes you start from its UI. Those child workers are deliberately
# left running when the app exits — quit them from the app's menu-bar
# "Quit and stop workers" item, not from here.
#
# Env vars (all optional):
#   RUST_LOG   log filter for the Rust backend (default "info")

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -d "$HOME/.rustup/toolchains/stable-aarch64-apple-darwin/bin" ]; then
    export PATH="$HOME/.rustup/toolchains/stable-aarch64-apple-darwin/bin:$PATH"
fi
export RUST_LOG="${RUST_LOG:-info}"

for tool in node npm cargo; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "Missing required tool: $tool. See README.md 'Run from source'." >&2
        exit 1
    }
done

# `tauri dev`'s child app binary. Resolved once so cleanup() and the
# pre-flight check agree on exactly what to look for.
APP_BIN="$PWD/target/debug/swarm-automation"

kill_orphan_app() {
    # pkill -f against the absolute path: narrow enough not to match this
    # script or an unrelated "swarm-automation" checkout, broad enough to
    # catch the binary whether cargo or launchd reparented it.
    #
    # SIGTERM then SIGKILL: this is a tray app that traps SIGTERM to hide
    # to the menu bar instead of exiting (same reason closing its window
    # doesn't quit it), so a plain `kill` leaves it running. Escalate.
    pkill -f "$APP_BIN" 2>/dev/null || return 0
    echo "   (stopping a leftover SWARM Automation app process)"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        pgrep -f "$APP_BIN" >/dev/null 2>&1 || return 0
        sleep 0.2
    done
    pkill -9 -f "$APP_BIN" 2>/dev/null || true
    return 0
}

_cleaned_up=""
cleanup() {
    # INT/TERM fire this and then EXIT fires it again; only sweep once.
    [ -n "$_cleaned_up" ] && return 0
    _cleaned_up=1
    echo
    echo "Stopping..."
    # Kill the whole `tauri dev` -> cargo -> app process tree we started,
    # then sweep any app binary that outlived it (window "hidden", not
    # quit). `wait` reaps the direct child so the shell doesn't report it
    # as terminated-by-signal noise.
    [ -n "${dev_pid:-}" ] && kill "$dev_pid" 2>/dev/null || true
    wait 2>/dev/null || true
    sleep 0.3
    kill_orphan_app
}
trap cleanup EXIT INT TERM

# Self-healing pre-flight: a previous run that didn't exit through cleanup()
# (terminal closed, machine slept, `kill -9`) can leave the single-instance
# app alive, which would hijack this run. Clear it before we start.
echo "==> Checking for a leftover SWARM Automation app process..."
kill_orphan_app

if [ ! -d node_modules ]; then
    echo "==> Installing npm dependencies (first run)..."
    npm install
fi

echo "==> Starting SWARM Automation (tauri dev, RUST_LOG=$RUST_LOG)..."
echo "    Closing the window hides it to the menu bar; press Ctrl+C here to"
echo "    fully stop the app. Child workers you start keep running — quit"
echo "    them from the app's menu-bar menu."
echo
npm run dev &
dev_pid=$!
wait "$dev_pid"
