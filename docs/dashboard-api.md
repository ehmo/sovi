# Dashboard & REST API

## Overview

FastAPI application serving both an htmx-powered web UI and a JSON REST API. Runs on port 8888 as a launchd KeepAlive service (`com.sovi.dashboard`).

**Stack:** FastAPI + Jinja2 + htmx + SSE + dark CSS theme

## Startup

```python
# Lifespan: init DB pool on startup, close on shutdown
@asynccontextmanager
async def lifespan(app):
    await init_pool()
    yield
    await close_pool()
```

## Routes

### Overview (`/`)

| Endpoint | Method | Returns |
|----------|--------|---------|
| `/` | GET | HTML overview page |
| `/api/overview` | GET | JSON fleet stats |

**Fleet stats payload:**
```json
{
  "total_accounts": 0,
  "active_devices": 2,
  "error_count": 0,
  "sessions_today": 0,
  "accounts_by_platform": [...],
  "devices_by_status": [{"status": "active", "cnt": 2}],
  "recent_events": [...],
  "niches": [{"name": "...", "slug": "..."}]
}
```

### Accounts (`/accounts`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/accounts` | GET | HTML accounts page |
| `/api/accounts` | GET | JSON list (filterable by platform, state, niche) |
| `/api/accounts/{id}` | GET | Account detail + recent events |
| `/api/accounts` | POST | Create account |
| `/api/accounts/{id}` | PATCH | Update account |
| `/api/accounts/{id}/retry-login` | POST | Retry failed login |

**Query params:** `?platform=tiktok&state=warming_p1&niche=motivation`

### Devices (`/devices`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/devices` | GET | HTML devices page |
| `/api/devices` | GET | JSON device list |
| `/api/devices` | POST | Register new device |
| `/api/devices/{id}/sessions` | GET | Recent events for device |

### Events (`/events`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/events` | GET | HTML events page |
| `/api/events` | GET | Filtered event list |
| `/api/events/unresolved` | GET | Unresolved events only |
| `/api/events/{id}/resolve` | POST | Mark event resolved |
| `/api/logs/stream` | GET | **SSE endpoint** (real-time) |

**Event query params:** `?severity=error&category=scheduler&device_id=...&account_id=...&resolved=false`

**SSE stream:** Polls `system_events` every 2 seconds, sends new events as `data:` messages. Connect via htmx SSE extension or EventSource.

### Scheduler (`/api/scheduler`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/scheduler/status` | GET | Scheduler state + thread info |
| `/api/scheduler/start` | POST | Start all device threads |
| `/api/scheduler/stop` | POST | Graceful shutdown |

**Status payload:**
```json
{
  "running": true,
  "device_count": 2,
  "sessions_per_day_target": 32,
  "threads": {
    "device-uuid": {
      "device_name": "iPhone-A",
      "current_task": "warming:tiktok/grind4829",
      "current_account": "grind4829",
      "sessions_today": 5,
      "last_session_at": "2026-02-17T10:30:00Z",
      "running": true,
      "alive": true,
      "error": null
    }
  }
}
```

### Settings (`/settings`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/settings` | GET | HTML settings page |
| `/api/settings/keys` | GET | API key status (configured/missing booleans) |
| `/api/settings/test/{service}` | GET | Connection test (db, captcha, sms, imap) |

## Web UI

### Templates

All templates extend `base.html` which provides:
- Navigation sidebar (Overview, Accounts, Devices, Events, Settings)
- CDN includes for htmx and SSE extension
- Dark theme CSS

| Template | Features |
|----------|----------|
| `overview.html` | Stat cards, scheduler start/stop, recent events, niches. Auto-refreshes every 30s. |
| `accounts.html` | Filterable table with platform/state badges, warming day, followers. Auto-refreshes every 10s. |
| `devices.html` | Device table with status badges, scheduler thread info. Auto-refreshes every 10s. |
| `events.html` | SSE connect button, event history with resolve buttons. |
| `settings.html` | API key status (green/red dots), connection test buttons. |

### Styling (`static/style.css`)

Dark theme with CSS variables:
- Background: `#0d1117`
- Surface: `#161b22`
- Border: `#30363d`
- Text: `#c9d1d9`
- Accent: `#58a6ff`

Components: 200px fixed sidebar, card grid, styled tables, color-coded badges (green=active, red=error, yellow=warning, orange=flagged), buttons, SSE event stream area.

## Usage

### Start Server

```bash
# Via CLI
sovi server --port 8888 --host 0.0.0.0

# Via launchd (auto-start on boot, auto-restart on crash)
launchctl load ~/Library/LaunchAgents/com.sovi.dashboard.plist
```

### API for LLM Agents

The REST API is designed for both human dashboard and LLM agent consumption:

```bash
# Get fleet overview
curl http://studio:8888/api/overview

# Check unresolved errors
curl http://studio:8888/api/events/unresolved

# Start scheduler
curl -X POST http://studio:8888/api/scheduler/start

# Stream events in real-time
curl http://studio:8888/api/logs/stream  # SSE
```
