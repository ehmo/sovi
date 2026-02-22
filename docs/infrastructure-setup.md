# Infrastructure Setup

## Prerequisites

- Mac Studio (or any macOS arm64 machine)
- Homebrew
- Python 3.12+ (`/opt/homebrew/bin/python3.12`)
- PostgreSQL 17 (`brew install postgresql@17`)
- iOS devices with USB connection
- iproxy (from `libimobiledevice`: `brew install libimobiledevice`)
- WebDriverAgent (via Appium: `brew install appium`)

## Initial Setup

### 1. Database

```bash
# Start PostgreSQL
brew services start postgresql@17

# Create database and user
/opt/homebrew/opt/postgresql@17/bin/psql -U noh -d postgres
CREATE USER sovi WITH PASSWORD 'sovi';
CREATE DATABASE sovi OWNER noh;
GRANT ALL PRIVILEGES ON DATABASE sovi TO sovi;
\q

# Run migrations (as table owner)
/opt/homebrew/opt/postgresql@17/bin/psql -U noh -d sovi -f migrations/001_initial_schema.sql
/opt/homebrew/opt/postgresql@17/bin/psql -U noh -d sovi -f migrations/003_scheduler_events.sql

# Grant permissions to sovi user
/opt/homebrew/opt/postgresql@17/bin/psql -U noh -d sovi -c "
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sovi;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sovi;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sovi;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO sovi;
"
```

**Note:** Migration 002 (continuous aggregates) requires TimescaleDB which is not installed. Skip it.

### 2. Python Environment

```bash
cd ~/Work/ai/sovi
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

### 3. Environment Variables

```bash
cp .env.example .env
# Edit .env with actual API keys
# Or use the interactive setup:
bash scripts/setup_keys.sh
```

### 4. Device Setup

For each iOS device:

```bash
# 1. Find UDID
idevice_id -l

# 2. Register in database
sovi devices add --name iPhone-C --udid 00008140-XXXX --wda-port 8102

# 3. Generate launchd plists
sovi devices setup --name iPhone-C

# 4. Load services
launchctl load ~/Library/LaunchAgents/com.sovi.iproxy-iphone-c.plist
launchctl load ~/Library/LaunchAgents/com.sovi.wda-iphone-c.plist

# 5. Verify
curl http://localhost:8102/status
```

### 5. Dashboard Service

The dashboard plist at `~/Library/LaunchAgents/com.sovi.dashboard.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "...">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sovi.dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/noh/Work/ai/sovi/.venv/bin/python</string>
        <string>-m</string>
        <string>sovi</string>
        <string>server</string>
        <string>--port</string>
        <string>8888</string>
        <string>--host</string>
        <string>0.0.0.0</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/noh/Work/ai/sovi</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/noh/Work/ai/sovi/output/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/noh/Work/ai/sovi/output/dashboard.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.sovi.dashboard.plist
# Verify
curl http://localhost:8888/api/overview
```

### 6. Verify Installation

```bash
sovi health
```

## Service Management

### Start/Stop Services

```bash
# Dashboard
launchctl load ~/Library/LaunchAgents/com.sovi.dashboard.plist
launchctl unload ~/Library/LaunchAgents/com.sovi.dashboard.plist

# iproxy tunnels
launchctl load ~/Library/LaunchAgents/com.sovi.iproxy-iphone-a.plist
launchctl unload ~/Library/LaunchAgents/com.sovi.iproxy-iphone-a.plist

# Check status
launchctl list | grep sovi
launchctl list com.sovi.dashboard
```

### Logs

| Service | Log Path |
|---------|----------|
| Dashboard | `~/Work/ai/sovi/output/dashboard.log` |
| Dashboard errors | `~/Work/ai/sovi/output/dashboard.err` |
| iproxy | `/tmp/com.sovi.iproxy-{name}.log` |
| WDA | `/tmp/com.sovi.wda-{name}.log` |
| Legacy warming | `/tmp/sovi-warming.log` |

### Code Deployment

The project lives at `~/Work/ai/sovi` on studio. To deploy changes from a dev machine:

```bash
rsync -avz --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
    /local/path/sovi/ studio:~/Work/ai/sovi/

# If dependencies changed:
ssh studio "cd ~/Work/ai/sovi && .venv/bin/pip install -e '.[dev]'"

# Restart dashboard to pick up code changes:
ssh studio "launchctl unload ~/Library/LaunchAgents/com.sovi.dashboard.plist && \
            launchctl load ~/Library/LaunchAgents/com.sovi.dashboard.plist"
```

## Adding a New Device

1. Connect iPhone via USB
2. Trust the computer on the device
3. Get UDID: `idevice_id -l`
4. Choose a WDA port (8100, 8101, 8102, ...)
5. Register: `sovi devices add --name iPhone-X --udid ... --wda-port 810X`
6. Generate plists: `sovi devices setup --name iPhone-X`
7. Load services: `launchctl load ~/Library/LaunchAgents/com.sovi.iproxy-iphone-x.plist && launchctl load ~/Library/LaunchAgents/com.sovi.wda-iphone-x.plist`
8. Sign into App Store on the device
9. Verify: `curl http://localhost:810X/status`

## Troubleshooting

### WDA Not Responding

```bash
# Check iproxy
launchctl list com.sovi.iproxy-iphone-a
cat /tmp/com.sovi.iproxy-iphone-a.log

# Check WDA
launchctl list com.sovi.wda-iphone-a
cat /tmp/com.sovi.wda-iphone-a.log

# Manual WDA test
curl http://localhost:8100/status
```

### Database Connection Issues

```bash
# Check PostgreSQL is running
brew services list | grep postgresql

# Test connection
/opt/homebrew/opt/postgresql@17/bin/psql -U sovi -d sovi -c "SELECT 1"

# If permission errors, run DDL as noh user
/opt/homebrew/opt/postgresql@17/bin/psql -U noh -d sovi
```

### Dashboard Not Starting

```bash
# Check logs
tail -50 ~/Work/ai/sovi/output/dashboard.err

# Test manually
cd ~/Work/ai/sovi && .venv/bin/python -m sovi server --port 8888

# Common issue: port already in use
lsof -i :8888
```
