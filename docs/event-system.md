# Event System

## Overview

All SOVI subsystems emit structured events to the `system_events` PostgreSQL table. Events are the primary observability mechanism — consumed by the dashboard UI, REST API, SSE stream, and LLM agents.

## Event Structure

```sql
CREATE TABLE system_events (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    category    TEXT NOT NULL,          -- scheduler, account, device, auth
    severity    TEXT NOT NULL,          -- info, warning, error, critical
    event_type  TEXT NOT NULL,          -- warming_complete, login_failed, etc.
    device_id   UUID REFERENCES devices(id),
    account_id  UUID REFERENCES accounts(id),
    message     TEXT NOT NULL,          -- Human-readable description
    context     JSONB DEFAULT '{}',     -- Structured metadata
    resolved    BOOLEAN DEFAULT false,
    resolved_by TEXT,                   -- human, llm_agent, auto
    resolved_at TIMESTAMPTZ
);
```

## Event Categories

### scheduler

| Event Type | Severity | When |
|------------|----------|------|
| scheduler_started | info | Scheduler begins with N devices |
| scheduler_stopping | info | Stop requested |
| scheduler_stopped | info | All threads stopped |
| no_devices | warning | Started but no active devices found |
| warming_started | info | Warming session begins |
| warming_complete | info | Warming session finished |
| warming_failed | error | Warming session threw exception |
| install_failed | error | App Store install timed out |
| login_failed | error | Account login failed |
| creation_started | info | Account creation begins |
| creation_skipped | warning | Creation skipped (email provider not configured) |
| device_loop_error | error | Unhandled exception in device thread |

### device

| Event Type | Severity | When |
|------------|----------|------|
| device_disconnected | critical | WDA not responding after timeout |
| app_deleted | info | App successfully deleted (IDFV reset) |
| app_delete_failed | error | Failed to delete app |
| app_installed | info | App installed from App Store |
| install_failed | error | Install timed out or failed |

### account

| Event Type | Severity | When |
|------------|----------|------|
| login_success | info | Platform login completed |
| login_failed | error | Login threw exception |
| account_creation_started | info | Signup flow begins |
| account_created | info | Account successfully created and stored in DB |
| account_creation_failed | error | Signup flow failed |

### auth

| Event Type | Severity | When |
|------------|----------|------|
| captcha_failed | warning | CAPTCHA solve timed out or task creation failed |

## Emitting Events

### Sync (for scheduler threads)

```python
from sovi import events

event_id = events.emit(
    "scheduler",           # category
    "info",               # severity
    "warming_complete",    # event_type
    f"Warmed tiktok/grind4829: 45 videos",  # message
    device_id=device_id,   # optional
    account_id=account_id, # optional
    context={              # optional JSONB
        "platform": "tiktok",
        "videos_watched": 45,
        "likes": 3,
        "duration_min": 30.2,
        "phase": "LIGHT",
        "warming_day": 5,
    },
)
```

### Async (for dashboard)

```python
event_id = await events.async_emit(
    "account", "error", "login_failed",
    f"Instagram login failed for user@example.com",
    device_id=device_id,
    account_id=account_id,
    context={"platform": "instagram", "step": "login"},
)
```

## Querying Events

### Unresolved Events

```python
# Sync
events_list = events.get_unresolved(severity="error", category="scheduler", limit=50)

# Async
events_list = await events.async_get_unresolved(severity="error")
```

### Flexible Query

```python
events_list = await events.async_get_events(
    severity="error",
    category="scheduler",
    device_id="uuid-here",
    resolved=False,
    limit=100,
    after_id=12345,  # For pagination
)
```

### Resolving Events

```python
# Sync
events.resolve(event_id=42, resolved_by="human")

# Async
await events.async_resolve(event_id=42, resolved_by="llm_agent")
```

## Dashboard Integration

### REST API

```bash
# All events (filtered)
GET /api/events?severity=error&category=scheduler&resolved=false

# Unresolved only
GET /api/events/unresolved

# Resolve an event
POST /api/events/42/resolve
```

### SSE Stream

```bash
GET /api/logs/stream
```

Returns Server-Sent Events, polling every 2 seconds:

```
data: {"id": 42, "timestamp": "...", "category": "scheduler", "severity": "error", ...}

data: {"id": 43, "timestamp": "...", "category": "device", "severity": "critical", ...}
```

Connect from JavaScript:
```javascript
const source = new EventSource('/api/logs/stream');
source.onmessage = (e) => {
    const event = JSON.parse(e.data);
    console.log(event);
};
```

## Context Field Conventions

The `context` JSONB field should include structured data relevant to the event:

```json
// warming_complete
{
    "platform": "tiktok",
    "videos_watched": 45,
    "likes": 3,
    "follows": 1,
    "duration_min": 30.2,
    "phase": "LIGHT",
    "new_state": "warming_p2",
    "warming_day": 5
}

// login_failed
{
    "platform": "tiktok",
    "username": "grind4829",
    "email": "user@example.com",
    "step": "login"
}

// device_disconnected
{
    "device_name": "iPhone-A",
    "wda_port": 8100
}

// captcha_failed
{
    "platform": "tiktok",
    "solver": "capsolver",
    "type": "slide",
    "task_id": "abc123"
}
```
