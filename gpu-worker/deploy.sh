#!/usr/bin/env bash
# Deploy the Football-IQ GPU worker to Fly.io.
#
# Required environment variables:
#   CLOUDFLARE_API_TOKEN      - the Cloudflare API token value
#   CLOUDFLARE_API_TOKEN_ID   - the token ID shown in the Cloudflare dashboard
#   WORKER_PASSWORD           - password for worker@football-iq.fly.dev
#                               (backend will be updated to match)
#
# Optional:
#   BACKEND_APP               - Fly backend app name (default: football-iq-backend)

set -euo pipefail

APP="football-iq-gpu-worker"
BACKEND_APP="${BACKEND_APP:-football-iq-backend}"
ACCOUNT_ID="4607053406491c18132943949b972140"

test -n "${CLOUDFLARE_API_TOKEN:-}"
test -n "${CLOUDFLARE_API_TOKEN_ID:-}"
test -n "${WORKER_PASSWORD:-}"

R2_SECRET_ACCESS_KEY=$(printf '%s' "$CLOUDFLARE_API_TOKEN" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest())')
R2_ACCESS_KEY_ID="$CLOUDFLARE_API_TOKEN_ID"

backend_secrets=$(mktemp)
cat > "$backend_secrets" <<EOF
SEED_WORKER_EMAIL=worker@football-iq.fly.dev
SEED_WORKER_PASSWORD=${WORKER_PASSWORD}
SEED_USERS_ON_STARTUP=true
SEED_RESET_PASSWORDS=true
EOF

echo "Syncing worker password on backend $BACKEND_APP (SEED_RESET_PASSWORDS=true)..."
flyctl secrets import -a "$BACKEND_APP" < "$backend_secrets"

cat > "$backend_secrets" <<EOF
SEED_RESET_PASSWORDS=false
EOF
echo "Disabling password reset on backend $BACKEND_APP..."
flyctl secrets import -a "$BACKEND_APP" < "$backend_secrets"

rm -f "$backend_secrets"

echo "Creating GPU worker app $APP (if it doesn't exist)..."
flyctl apps create "$APP" --org personal || true

gpu_secrets=$(mktemp)
cat > "$gpu_secrets" <<EOF
WORKER_EMAIL=worker@football-iq.fly.dev
WORKER_PASSWORD=${WORKER_PASSWORD}
R2_ACCESS_KEY_ID=${R2_ACCESS_KEY_ID}
R2_SECRET_ACCESS_KEY=${R2_SECRET_ACCESS_KEY}
R2_ENDPOINT_URL=https://${ACCOUNT_ID}.r2.cloudflarestorage.com
STORAGE_BACKEND=r2
EOF

echo "Setting GPU worker secrets..."
flyctl secrets import -a "$APP" < "$gpu_secrets"
rm -f "$gpu_secrets"

echo "Deploying $APP..."
flyctl deploy -a "$APP"
