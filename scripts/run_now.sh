#!/usr/bin/env bash
# Builds and runs the hosted SWARM Automation web app (`web/`, the axum
# backend that also serves `ui/`) locally for manual testing.
#
# The web app replaces the Tauri desktop client; this script no longer starts
# the desktop. The backend serves the same `ui/` assets on one origin, so
# opening the printed URL in a browser is the whole app.
#
# First run creates web/.env.local (git-ignored) with generated dev secrets
# (SWARM_WEB_LOCAL_KEY, the webhook secret). You only fill in the GitHub App
# credentials there to make sign-in work:
#
#   SWARM_WEB_GITHUB_CLIENT_ID       the GitHub App's client id
#   SWARM_WEB_GITHUB_CLIENT_SECRET   the GitHub App's client secret
#   SWARM_WEB_GITHUB_APP_SLUG        (optional) for the "install the app" link
#
# In the GitHub App settings, set the callback URL to:
#   http://127.0.0.1:8080/api/v1/auth/github/callback   (or your PORT/HOST)
#
# Without credentials the server still starts (UI, /api/v1/health, static
# assets) but "Sign in with GitHub" will fail. The store is in-memory, so
# sign-ins, keys and usage are lost on every restart.
#
# Env vars (all optional, override web/.env.local):
#   HOST       interface to bind (default 127.0.0.1)
#   PORT       port to bind (default 8080)
#   RUST_LOG   log filter (default "info")
#   SWARM_WEB_*  any backend setting, see docs/web-architecture.md

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$PWD"

if [ -d "$HOME/.rustup/toolchains/stable-aarch64-apple-darwin/bin" ]; then
    export PATH="$HOME/.rustup/toolchains/stable-aarch64-apple-darwin/bin:$PATH"
fi

command -v cargo >/dev/null 2>&1 || {
    echo "Missing required tool: cargo (install Rust: https://rustup.rs)." >&2
    exit 1
}
command -v openssl >/dev/null 2>&1 || {
    echo "Missing required tool: openssl (used to generate dev secrets)." >&2
    exit 1
}

ENV_FILE="$ROOT/web/.env.local"

if [ ! -f "$ENV_FILE" ]; then
    echo "==> Creating $ENV_FILE with generated dev secrets..."
    umask 077
    cat >"$ENV_FILE" <<EOF
# Local development settings for scripts/run_now.sh. Git-ignored; never commit.
SWARM_WEB_LOCAL_KEY=$(openssl rand -base64 32)
SWARM_WEB_GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 24)
SWARM_WEB_INTERNAL_TOKEN=$(openssl rand -hex 24)

# Fill these in from your GitHub App to enable sign-in:
SWARM_WEB_GITHUB_CLIENT_ID=
SWARM_WEB_GITHUB_CLIENT_SECRET=
SWARM_WEB_GITHUB_APP_SLUG=
EOF
fi

# Values already in the environment win over the file.
caller_env="$(env | grep -E '^(SWARM_WEB_[A-Z_]+|HOST|PORT)=' || true)"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
while IFS= read -r line; do
    [ -n "$line" ] && export "$line"
done <<<"$caller_env"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
export SWARM_WEB_BIND="${SWARM_WEB_BIND:-$HOST:$PORT}"
export SWARM_WEB_PUBLIC_URL="${SWARM_WEB_PUBLIC_URL:-http://$SWARM_WEB_BIND}"
export SWARM_WEB_UI_DIR="${SWARM_WEB_UI_DIR:-$ROOT/ui}"
export RUST_LOG="${RUST_LOG:-info}"

if [ -z "${SWARM_WEB_GITHUB_CLIENT_ID:-}" ] || [ -z "${SWARM_WEB_GITHUB_CLIENT_SECRET:-}" ]; then
    echo "!! GitHub App credentials are not set in $ENV_FILE." >&2
    echo "   The server will start, but sign-in will not work until you set" >&2
    echo "   SWARM_WEB_GITHUB_CLIENT_ID and SWARM_WEB_GITHUB_CLIENT_SECRET." >&2
    export SWARM_WEB_GITHUB_CLIENT_ID="${SWARM_WEB_GITHUB_CLIENT_ID:-unconfigured}"
    export SWARM_WEB_GITHUB_CLIENT_SECRET="${SWARM_WEB_GITHUB_CLIENT_SECRET:-unconfigured}"
fi

echo "==> Building and starting swarm-web..."
echo "    URL:       $SWARM_WEB_PUBLIC_URL"
echo "    Callback:  $SWARM_WEB_PUBLIC_URL/api/v1/auth/github/callback"
echo "    UI dir:    $SWARM_WEB_UI_DIR"
echo "    Ctrl+C to stop. State is in memory and is lost on exit."
echo

cd "$ROOT/web"
# exec: cargo (and the server it launches) receive Ctrl+C directly, and
# nothing is left running behind this script.
exec cargo run --locked
