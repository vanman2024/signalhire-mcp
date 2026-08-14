#!/bin/bash
# Deploy the SignalHire MCP server to a DigitalOcean droplet.
#
# What changed from the previous version of this script
# -----------------------------------------------------
# It used to deploy a *callback-only* service: `requirements.callback.txt` and
# `ExecStart=... python start-callback.py`. That start script was never in this
# repository — it existed only on the droplet — so the deployed behaviour could
# not be read, reviewed, or reproduced from source. And because it ran alone,
# the MCP server had no way to see anything it received.
#
# This deploys ONE process that serves:
#     /mcp/                             the MCP endpoint
#     /signalhire/callback/{tenant}     SignalHire's webhook
#     /health                           liveness + inbox depth
#
# Same port, same process, shared durable store. That is the fix.

set -euo pipefail

DROPLET_NAME="${DROPLET_NAME:-signalhire-callback}"
DEPLOY_DIR="/opt/signalhire-mcp"
SERVICE_NAME="signalhire-mcp"
ENV_FILE="/etc/signalhire/.env"
DATA_DIR="/var/lib/signalhire-mcp"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${GREEN}SignalHire MCP — droplet deployment${NC}"
echo "----------------------------------------"

# --- preflight --------------------------------------------------------------

require() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo -e "${RED}ERROR: $name is not set${NC}" >&2
    return 1
  fi
}

MISSING=0
require SIGNALHIRE_API_KEY || MISSING=1
require SIGNALHIRE_PUBLIC_BASE_URL || MISSING=1
require SIGNALHIRE_CALLBACK_SECRET || MISSING=1

if [ "$MISSING" -ne 0 ]; then
  cat >&2 <<'USAGE'

Required environment:

  SIGNALHIRE_API_KEY          Your SignalHire API key.
  SIGNALHIRE_PUBLIC_BASE_URL  The public HTTPS base URL of this server, e.g.
                              https://signalhire.example.com
                              SignalHire POSTs results here. Without it every
                              reveal is billed and then discarded.
  SIGNALHIRE_CALLBACK_SECRET  Shared secret on the callback URL.
                              Generate with: openssl rand -hex 32

Strongly recommended:

  SIGNALHIRE_AUTH_MODE        platform | jwt | none
                              The server REFUSES to serve HTTP without this.
                              Every reveal tool spends a credit, so an
                              unauthenticated endpoint bills your account.

Optional:

  SIGNALHIRE_TENANTS          Delivery adapter config (JSON).
  SIGNALHIRE_ALLOWED_HOSTS    Hostnames this server answers to, behind a proxy.
  STAFFHIVE_RELAY_SECRET      Referenced by SIGNALHIRE_TENANTS.
  CATS_MCP_TOKEN              Referenced by SIGNALHIRE_TENANTS.

Example:

  export SIGNALHIRE_API_KEY='...'
  export SIGNALHIRE_PUBLIC_BASE_URL='https://signalhire.example.com'
  export SIGNALHIRE_CALLBACK_SECRET="$(openssl rand -hex 32)"
  export SIGNALHIRE_AUTH_MODE='platform'
  bash deploy-to-droplet.sh

USAGE
  exit 1
fi

if [ "${SIGNALHIRE_PUBLIC_BASE_URL#https://}" = "$SIGNALHIRE_PUBLIC_BASE_URL" ]; then
  echo -e "${YELLOW}WARNING: SIGNALHIRE_PUBLIC_BASE_URL is not https.${NC}"
  echo "SignalHire requires a valid TLS certificate to deliver callbacks."
fi

if [ -z "${SIGNALHIRE_AUTH_MODE:-}" ]; then
  echo -e "${YELLOW}WARNING: SIGNALHIRE_AUTH_MODE is unset.${NC}"
  echo "The server will refuse to start on HTTP. Set it to platform, jwt, or none."
fi

echo -e "${GREEN}OK${NC} preflight"

# --- 1. environment file ----------------------------------------------------

echo ""
echo "1/6  Writing environment file"

TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_ENV"' EXIT

cat > "$TMP_ENV" <<EOF
# SignalHire MCP server configuration.
# Written by deploy-to-droplet.sh. Contains credentials; mode 600.

SIGNALHIRE_API_KEY=${SIGNALHIRE_API_KEY}
SIGNALHIRE_PUBLIC_BASE_URL=${SIGNALHIRE_PUBLIC_BASE_URL}
SIGNALHIRE_CALLBACK_SECRET=${SIGNALHIRE_CALLBACK_SECRET}

SIGNALHIRE_TRANSPORT=http
SIGNALHIRE_HOST=0.0.0.0
SIGNALHIRE_PORT=8000
SIGNALHIRE_MCP_PATH=/mcp/
SIGNALHIRE_DATA_DIR=${DATA_DIR}

SIGNALHIRE_AUTH_MODE=${SIGNALHIRE_AUTH_MODE:-}
SIGNALHIRE_AUTH_JWKS_URI=${SIGNALHIRE_AUTH_JWKS_URI:-}
SIGNALHIRE_AUTH_ISSUER=${SIGNALHIRE_AUTH_ISSUER:-}
SIGNALHIRE_AUTH_AUDIENCE=${SIGNALHIRE_AUTH_AUDIENCE:-}
SIGNALHIRE_ALLOWED_HOSTS=${SIGNALHIRE_ALLOWED_HOSTS:-}

SIGNALHIRE_TENANTS=${SIGNALHIRE_TENANTS:-}
SIGNALHIRE_MOUNTS=${SIGNALHIRE_MOUNTS:-}

STAFFHIVE_RELAY_SECRET=${STAFFHIVE_RELAY_SECRET:-}
CATS_MCP_TOKEN=${CATS_MCP_TOKEN:-}

LOG_LEVEL=${LOG_LEVEL:-INFO}
EOF

doctl compute ssh "$DROPLET_NAME" --ssh-command "sudo mkdir -p /etc/signalhire"
doctl compute scp "$TMP_ENV" "$DROPLET_NAME:/tmp/signalhire.env"
doctl compute ssh "$DROPLET_NAME" --ssh-command \
  "sudo mv /tmp/signalhire.env $ENV_FILE && sudo chmod 600 $ENV_FILE && sudo chown root:root $ENV_FILE"

echo -e "${GREEN}OK${NC} $ENV_FILE (mode 600)"

# --- 2. durable inbox directory --------------------------------------------

echo ""
echo "2/6  Creating the durable inbox directory"

# The service refuses to start if this is not writable, on purpose: a callback
# that cannot be persisted is a reveal that was billed and then lost.
doctl compute ssh "$DROPLET_NAME" --ssh-command \
  "sudo mkdir -p $DATA_DIR && sudo chown root:root $DATA_DIR && sudo chmod 700 $DATA_DIR"

echo -e "${GREEN}OK${NC} $DATA_DIR"

# --- 3. systemd unit --------------------------------------------------------

echo ""
echo "3/6  Installing the systemd unit"

TMP_UNIT="$(mktemp)"
cat > "$TMP_UNIT" <<EOF
[Unit]
Description=SignalHire MCP Server (MCP endpoint + callback receiver)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$DEPLOY_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$DEPLOY_DIR/venv/bin/python -m signalhire_mcp.app
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
# The inbox must be writable; that is the whole point of the service.
ReadWritePaths=$DEPLOY_DIR $DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

doctl compute scp "$TMP_UNIT" "$DROPLET_NAME:/tmp/$SERVICE_NAME.service"
rm -f "$TMP_UNIT"
doctl compute ssh "$DROPLET_NAME" --ssh-command \
  "sudo mv /tmp/$SERVICE_NAME.service /etc/systemd/system/$SERVICE_NAME.service && sudo chmod 644 /etc/systemd/system/$SERVICE_NAME.service"

echo -e "${GREEN}OK${NC} /etc/systemd/system/$SERVICE_NAME.service"

# --- 4. code ----------------------------------------------------------------

echo ""
echo "4/6  Syncing code"

DROPLET_IP="$(doctl compute droplet get "$DROPLET_NAME" --format PublicIPv4 --no-header)"
if [ -z "$DROPLET_IP" ]; then
  echo -e "${RED}Could not resolve the droplet IP for $DROPLET_NAME${NC}" >&2
  exit 1
fi

rsync -az --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='.pytest_cache' \
  ./ "root@$DROPLET_IP:$DEPLOY_DIR/"

echo -e "${GREEN}OK${NC} $DEPLOY_DIR"

# --- 5. dependencies --------------------------------------------------------

echo ""
echo "5/6  Installing dependencies"

# --prerelease=allow is required: fastmcp 4 is a prerelease. Exact pins in
# pyproject.toml keep that permission from pulling prerelease pydantic/httpx.
doctl compute ssh "$DROPLET_NAME" --ssh-command "
  set -e
  cd $DEPLOY_DIR
  python3 -m venv venv 2>/dev/null || true
  ./venv/bin/pip install --quiet --upgrade pip
  ./venv/bin/pip install --quiet --pre -e .
"

echo -e "${GREEN}OK${NC} dependencies installed"

# --- 6. restart and verify --------------------------------------------------

echo ""
echo "6/6  Restarting and verifying"

doctl compute ssh "$DROPLET_NAME" --ssh-command "
  sudo systemctl daemon-reload
  sudo systemctl enable $SERVICE_NAME
  sudo systemctl restart $SERVICE_NAME
"

# The old script slept 3 seconds and hoped. Poll instead: a service that takes
# four seconds to bind is healthy, and reporting it as failed sends people
# looking for a problem that is not there.
echo -n "Waiting for health"
HEALTHY=0
for _ in $(seq 1 20); do
  if curl -sf --max-time 3 "http://$DROPLET_IP:8000/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  echo -n "."
  sleep 1
done
echo ""

if [ "$HEALTHY" -ne 1 ]; then
  echo -e "${RED}Health check failed.${NC}"
  echo ""
  echo "Recent logs:"
  doctl compute ssh "$DROPLET_NAME" --ssh-command "sudo journalctl -u $SERVICE_NAME -n 40 --no-pager"
  exit 1
fi

echo -e "${GREEN}OK${NC} service is healthy"
curl -s --max-time 5 "http://$DROPLET_IP:8000/health" || true
echo ""

cat <<SUMMARY

----------------------------------------
Deployment complete.

Endpoints (one process, one port):
  MCP        http://$DROPLET_IP:8000/mcp/
  Callback   http://$DROPLET_IP:8000/signalhire/callback/{tenant}
  Health     http://$DROPLET_IP:8000/health

Verify the callback path end to end:
  curl -sS -X POST \\
    "\$SIGNALHIRE_PUBLIC_BASE_URL/signalhire/callback/default?secret=\$SIGNALHIRE_CALLBACK_SECRET" \\
    -H 'Content-Type: application/json' -H 'Request-Id: smoke-test' \\
    -d '[{"item":"smoke@test","status":"failed"}]'
  # Expect: {"status":"accepted","event_id":"...","items":1}

Management:
  Logs      doctl compute ssh $DROPLET_NAME --ssh-command 'sudo journalctl -u $SERVICE_NAME -f'
  Restart   doctl compute ssh $DROPLET_NAME --ssh-command 'sudo systemctl restart $SERVICE_NAME'
  Status    doctl compute ssh $DROPLET_NAME --ssh-command 'sudo systemctl status $SERVICE_NAME'
  Inbox     doctl compute ssh $DROPLET_NAME --ssh-command 'ls $DATA_DIR/events/pending | wc -l'

Still to do:
  1. Put TLS in front (nginx or caddy). SignalHire needs a valid certificate,
     and SIGNALHIRE_PUBLIC_BASE_URL must match it.
  2. Restrict port 8000 to the proxy once TLS terminates there.
  3. Confirm SIGNALHIRE_AUTH_MODE reflects who actually authenticates callers.
----------------------------------------
SUMMARY
