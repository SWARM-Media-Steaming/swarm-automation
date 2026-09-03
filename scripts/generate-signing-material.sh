#!/usr/bin/env bash
# One-time: generate the signing material the release workflow needs and print
# the `gh secret set` commands to install it on this repository.
#
# Produces two things:
#   1. A self-signed macOS code-signing certificate (p12). Not an Apple
#      Developer ID — the app is NOT notarized. Its only job is a *stable*
#      designated requirement so an in-place self-update keeps the macOS
#      file-access / TCC grants the user already gave. A fresh DMG install
#      still needs one right-click -> Open.
#   2. A Tauri updater signing keypair (minisign). The public half already
#      lives in tauri.conf.json (`plugins.updater.pubkey`); the private half
#      becomes a secret so CI can sign the `.app.tar.gz`.
#
# Re-running rotates BOTH. Rotating the cert changes the designated
# requirement, so every already-installed copy must be reinstalled from a
# fresh DMG once. Rotating the updater key means you must also paste the new
# pubkey into tauri.conf.json and ship that before old clients can verify new
# updates. Only rotate on purpose.
#
# Usage:  scripts/generate-signing-material.sh [--print-only]
#   --print-only   Show the gh commands but do not run them.

set -euo pipefail

REPO="SWARM-Media-Steaming/swarm-automation"
BUNDLE_ID="app.swarm.automation"
IDENTITY_CN="SWARM Automation CI Signing"
PRINT_ONLY="${1:-}"

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
command -v gh >/dev/null || { echo "the GitHub CLI (gh) is required" >&2; exit 1; }
command -v npx >/dev/null || { echo "node/npx is required (for 'tauri signer generate')" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---- 1. macOS self-signed code-signing certificate --------------------------
CERT_PASS="$(openssl rand -hex 24)"
cat >"$WORK/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = ${IDENTITY_CN}
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$WORK/key.pem" -out "$WORK/cert.pem" \
    -days 7300 -config "$WORK/openssl.cnf" >/dev/null 2>&1
openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
    -out "$WORK/identity.p12" -passout "pass:${CERT_PASS}" -name "$IDENTITY_CN" >/dev/null 2>&1
CERT_B64="$(base64 < "$WORK/identity.p12" | tr -d '\n')"

# ---- 2. Tauri updater keypair ----------------------------------------------
UPDATER_PASS="$(openssl rand -hex 24)"
CI=true npx --yes tauri signer generate --ci -p "$UPDATER_PASS" -w "$WORK/updater.key" >/dev/null 2>&1
UPDATER_KEY="$(cat "$WORK/updater.key")"
UPDATER_PUB="$(cat "$WORK/updater.key.pub")"

# Write the new public key into tauri.conf.json so it matches the private key
# CI will sign with. Committing this change is required before the first
# release that clients should accept.
CONF="$(cd "$(dirname "$0")/.." && pwd)/tauri.conf.json"
if command -v jq >/dev/null; then
    tmp="$(mktemp)"
    jq --arg k "$UPDATER_PUB" '.plugins.updater.pubkey = $k' "$CONF" >"$tmp" && mv "$tmp" "$CONF"
    echo "Updated $CONF (plugins.updater.pubkey) — commit this change."
else
    echo "jq not found — manually set plugins.updater.pubkey in tauri.conf.json to:"
    echo "  $UPDATER_PUB"
fi
echo
echo "Run these to install the repository secrets:"
echo
CMDS=$(cat <<EOF
gh secret set APPLE_CERTIFICATE            --repo $REPO --body '$CERT_B64'
gh secret set APPLE_CERTIFICATE_PASSWORD   --repo $REPO --body '$CERT_PASS'
gh secret set APPLE_SIGNING_IDENTITY       --repo $REPO --body '$IDENTITY_CN'
gh secret set TAURI_SIGNING_PRIVATE_KEY          --repo $REPO --body '$UPDATER_KEY'
gh secret set TAURI_SIGNING_PRIVATE_KEY_PASSWORD --repo $REPO --body '$UPDATER_PASS'
EOF
)
echo "$CMDS"
echo

if [ "$PRINT_ONLY" = "--print-only" ]; then
    exit 0
fi
read -r -p "Set these secrets on $REPO now? [y/N] " ANSWER
[ "$ANSWER" = "y" ] || [ "$ANSWER" = "Y" ] || { echo "Skipped. Copy the commands above when ready."; exit 0; }
eval "$CMDS"
echo "Secrets set. The next push to main will publish an updatable release."
