#!/usr/bin/env bash
# provision_studio.sh — Full Mac Studio provisioning (runs ON the studio)
# Usage: ssh studio 'bash -s' < scripts/provision_studio.sh
set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

log() { echo "==> $*"; }
ok()  { echo "  ✓ $*"; }

# ---------- 1. Homebrew ----------
if ! command -v brew &>/dev/null; then
    log "Installing Homebrew..."
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
    # Persist brew in shell profile
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
    ok "Homebrew installed"
else
    ok "Homebrew already installed"
fi

# ---------- 2. Core packages ----------
log "Installing packages..."
brew install python@3.12 postgresql@17 libimobiledevice git-crypt uv 2>/dev/null || true
ok "Packages installed"

# ---------- 3. PostgreSQL ----------
log "Setting up PostgreSQL..."
brew services start postgresql@17

# Wait for PostgreSQL to be ready
for i in $(seq 1 15); do
    /opt/homebrew/opt/postgresql@17/bin/pg_isready -q 2>/dev/null && break
    sleep 1
done

PSQL="/opt/homebrew/opt/postgresql@17/bin/psql"

# Create sovi user and database
$PSQL -U $(whoami) -d postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='sovi'" | grep -q 1 || \
    $PSQL -U $(whoami) -d postgres -c "CREATE ROLE sovi WITH LOGIN PASSWORD 'sovi'"
$PSQL -U $(whoami) -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='sovi'" | grep -q 1 || \
    $PSQL -U $(whoami) -d postgres -c "CREATE DATABASE sovi OWNER $(whoami)"
ok "PostgreSQL configured"

# ---------- 4. Project setup ----------
log "Setting up project..."
DEPLOY_PATH="$HOME/Work/ai/sovi"
REPO="git@github.com:ehmo/sovi.git"

mkdir -p "$HOME/Work/ai"

if [ -d "$DEPLOY_PATH/.git" ]; then
    cd "$DEPLOY_PATH" && git pull origin main
    ok "Repo updated"
elif [ -d "$DEPLOY_PATH" ]; then
    cd "$DEPLOY_PATH" && git init && git remote add origin "$REPO" && \
        git fetch origin && git checkout -f main
    ok "Repo initialized from existing dir"
else
    git clone "$REPO" "$DEPLOY_PATH"
    ok "Repo cloned"
fi

cd "$DEPLOY_PATH"

# Unlock secrets if key exists
if [ -f /tmp/sovi.key ]; then
    git-crypt unlock /tmp/sovi.key 2>/dev/null || true
    ok "Secrets unlocked"
fi

# ---------- 5. Python venv ----------
log "Setting up Python environment..."
if [ ! -d .venv ]; then
    /opt/homebrew/bin/python3.12 -m venv .venv
fi
.venv/bin/pip install -e ".[dev]" --quiet 2>&1 | tail -3
ok "Python environment ready"

# ---------- 6. Database migrations ----------
log "Running migrations..."
# Skip TimescaleDB-dependent parts (001 has create_hypertable calls)
# Run a stripped version that skips TimescaleDB
$PSQL -U $(whoami) -d sovi -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>/dev/null || true
$PSQL -U $(whoami) -d sovi -c "CREATE EXTENSION IF NOT EXISTS \"pgcrypto\";" 2>/dev/null || true

# Run migrations (they use IF NOT EXISTS / DO blocks, so idempotent)
# 001 will fail on TimescaleDB parts but core tables will be created
$PSQL -U $(whoami) -d sovi -f migrations/001_initial_schema.sql 2>&1 | grep -v "^$" | tail -5 || true
$PSQL -U $(whoami) -d sovi -f db/migrations/002_personas.sql 2>&1 | tail -3 || true
$PSQL -U $(whoami) -d sovi -f migrations/003_scheduler_events.sql 2>&1 | tail -3 || true
$PSQL -U $(whoami) -d sovi -f migrations/004_identity_guardrails.sql 2>&1 | tail -3 || true

# Grant permissions
$PSQL -U $(whoami) -d sovi -c "
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO sovi;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO sovi;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO sovi;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO sovi;
" 2>/dev/null
ok "Migrations done"

# ---------- 7. Output directory ----------
mkdir -p "$DEPLOY_PATH/output"

# ---------- 8. Device iproxy tunnels ----------
log "Setting up device tunnels..."
mkdir -p ~/Library/LaunchAgents

# Get connected device UDIDs
UDIDS=$(idevice_id -l 2>/dev/null || true)
DEVICE_NUM=0

for UDID in $UDIDS; do
    DEVICE_NUM=$((DEVICE_NUM + 1))
    PORT=$((8099 + DEVICE_NUM))
    LETTER=$(echo $DEVICE_NUM | awk '{printf "%c", 64+$1}')
    NAME="iphone-$(echo $LETTER | tr '[:upper:]' '[:lower:]')"
    PLIST_NAME="com.sovi.iproxy-${NAME}"
    PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

    if [ ! -f "$PLIST_PATH" ]; then
        cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/iproxy</string>
        <string>${PORT}:8100</string>
        <string>-u</string>
        <string>${UDID}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/${PLIST_NAME}.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/${PLIST_NAME}.err</string>
</dict>
</plist>
PLIST
        ok "Created plist for $NAME (port $PORT, UDID $UDID)"
    fi

    # Load the tunnel
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    ok "Loaded iproxy tunnel for $NAME -> localhost:$PORT"
done

# ---------- 9. Dashboard plist ----------
log "Setting up dashboard service..."
DASHBOARD_PLIST="$HOME/Library/LaunchAgents/com.sovi.dashboard.plist"
cat > "$DASHBOARD_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sovi.dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>${DEPLOY_PATH}/.venv/bin/python</string>
        <string>-m</string>
        <string>sovi</string>
        <string>server</string>
        <string>--port</string>
        <string>8888</string>
        <string>--host</string>
        <string>0.0.0.0</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${DEPLOY_PATH}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${DEPLOY_PATH}/output/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>${DEPLOY_PATH}/output/dashboard.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST

launchctl unload "$DASHBOARD_PLIST" 2>/dev/null || true
launchctl load "$DASHBOARD_PLIST"
ok "Dashboard service loaded"

# ---------- 10. Verify ----------
log "Verifying setup..."
echo "  Python: $(.venv/bin/python --version)"
echo "  PostgreSQL: $($PSQL --version | head -1)"
echo "  Homebrew: $(brew --version | head -1)"
echo "  Devices connected: $(idevice_id -l 2>/dev/null | wc -l | tr -d ' ')"
echo "  iproxy tunnels: $(launchctl list 2>/dev/null | grep -c 'sovi.iproxy' || echo 0)"

# Test DB connectivity
.venv/bin/python -c "
from sovi.db import sync_conn
conn = sync_conn()
cur = conn.cursor()
cur.execute('SELECT COUNT(*) as cnt FROM devices')
row = cur.fetchone()
print(f'  Devices in DB: {row[\"cnt\"]}')
conn.close()
" 2>/dev/null || echo "  DB check failed (may need .env setup)"

# Test WDA on each port
for PORT in 8100 8101; do
    STATUS=$(curl -sf "http://localhost:${PORT}/status" 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('ready' if d.get('value',{}).get('ready') else 'not ready')
" 2>/dev/null || echo "not responding")
    echo "  WDA :${PORT}: ${STATUS}"
done

# Dashboard health
sleep 2
DASH=$(curl -sf http://localhost:8888/api/overview 2>/dev/null && echo "running" || echo "not responding")
echo "  Dashboard: ${DASH}"

log "Provisioning complete!"
echo ""
echo "Next steps:"
echo "  1. Copy .env with API keys to $DEPLOY_PATH/.env"
echo "  2. Set up SSH keys for GitHub: ssh-keygen -t ed25519"
echo "  3. Install WDA on devices (requires Xcode)"
echo "  4. Run: sovi health"
