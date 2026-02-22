# SOVI Architecture

**Social Video Intelligence & Distribution Network**

## System Overview

SOVI is an automated social media account farm and content distribution system. It manages fleets of iOS devices to create, warm, and operate social media accounts across TikTok and Instagram, then produces and distributes short-form video content through those accounts.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Mac Studio (studio)                         │
│  macOS 15.7.4 arm64 · PostgreSQL 17 · Python 3.12                 │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │ iPhone-A │  │ iPhone-B │  │ iPhone-C │  │   ...    │  (10+)    │
│  │ :8100    │  │ :8101    │  │ :8102    │  │          │           │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────┘           │
│       │              │              │                               │
│       └──────────────┴──────────────┘                               │
│                      │                                              │
│            ┌─────────▼──────────┐                                   │
│            │   iproxy tunnels   │  USB → localhost:port             │
│            │   (launchd KeepAlive)                                  │
│            └─────────┬──────────┘                                   │
│                      │                                              │
│            ┌─────────▼──────────┐                                   │
│            │  WebDriverAgent    │  WDA HTTP API per device          │
│            │  (launchd KeepAlive)                                   │
│            └─────────┬──────────┘                                   │
│                      │                                              │
│  ┌───────────────────▼───────────────────────────────────────────┐  │
│  │                  SOVI Application                             │  │
│  │                                                               │  │
│  │  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │  │
│  │  │  Scheduler   │  │  Dashboard   │  │  Content Pipeline  │  │  │
│  │  │  (threads)   │  │  (FastAPI)   │  │  (async)           │  │  │
│  │  │              │  │  :8888       │  │                    │  │  │
│  │  │ 1 thread/    │  │  htmx + SSE  │  │ research → script │  │  │
│  │  │  device      │  │  REST API    │  │ → assets → video  │  │  │
│  │  └──────┬───────┘  └──────┬───────┘  │ → distribute      │  │  │
│  │         │                 │           └────────────────────┘  │  │
│  │         └────────┬────────┘                                   │  │
│  │                  │                                            │  │
│  │         ┌────────▼────────┐                                   │  │
│  │         │   PostgreSQL 17  │                                   │  │
│  │         │   (Homebrew)     │                                   │  │
│  │         │   DB: sovi       │                                   │  │
│  │         └─────────────────┘                                   │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## Infrastructure

### Host Machine

- **Machine**: Mac Studio (hostname: `studio`, SSH config: `Host studio` → `noh.local`)
- **OS**: macOS 15.7.4 arm64
- **Python**: 3.12 via Homebrew (`/opt/homebrew/bin/python3.12`)
- **Venv**: `~/Work/ai/sovi/.venv/`
- **PostgreSQL**: 17 via Homebrew (`/opt/homebrew/opt/postgresql@17/bin/psql`)
  - DB owner: `noh` (system user)
  - App user: `sovi` (regular user, can SELECT/INSERT/UPDATE but doesn't own tables)
- **Docker**: NOT installed. The `docker-compose.yml` in the repo is aspirational; actual infra is native Homebrew.
- **Appium**: Installed at `/opt/homebrew/bin/appium` but NOT used — we talk to WDA directly.

### Connected Devices

| Name      | UDID                             | WDA Port | Role      |
|-----------|----------------------------------|----------|-----------|
| iPhone-A  | 00008140-001975DC3678801C        | 8100     | Warming   |
| iPhone-B  | 00008140-001A00141163001C        | 8101     | Warming   |

Each device connects via USB and is exposed through:
1. **iproxy** tunnel: `localhost:{port}` → device port 8100
2. **WebDriverAgent**: Runs on-device, exposes W3C WebDriver HTTP API

### Services (launchd)

| Service                        | Type        | Purpose                  |
|-------------------------------|-------------|--------------------------|
| `com.sovi.iproxy-iphone-a`   | KeepAlive   | USB tunnel for iPhone-A  |
| `com.sovi.iproxy-iphone-b`   | KeepAlive   | USB tunnel for iPhone-B  |
| `com.sovi.wda-iphone-a`      | KeepAlive   | WDA on iPhone-A          |
| `com.sovi.wda-iphone-b`      | KeepAlive   | WDA on iPhone-B          |
| `com.sovi.dashboard`          | KeepAlive   | FastAPI dashboard :8888  |
| `com.sovi.warming`            | StartInterval | Legacy warming (2h cycle) |

## Core Design Decisions

### Direct WDA (No Appium)

We communicate with WebDriverAgent directly via HTTP. This eliminates the Appium middleware layer which adds latency, crashes, and complexity. WDA exposes a W3C-compatible API that's sufficient for all our automation needs.

### Thread-per-Device Scheduling

Each device gets its own Python thread running an infinite loop. Threads are daemon threads that share a `threading.Event` for graceful shutdown. Task claiming uses `FOR UPDATE SKIP LOCKED` in PostgreSQL to prevent conflicts between threads.

### IDFV Isolation via Delete/Install Cycling

Before each warming session, the app is deleted and reinstalled from the App Store. This resets the IDFV (Identifier for Vendor), making each session appear to come from a different device installation. This is critical for avoiding platform fingerprinting.

### Dual API Pattern (Sync + Async)

The codebase maintains both synchronous and asynchronous database helpers:
- **Sync** (`sync_execute`, `sync_conn`): Used by scheduler threads and CLI
- **Async** (`execute`, `get_conn`): Used by FastAPI dashboard routes

This avoids event loop conflicts — scheduler threads can't use async code, and FastAPI routes must use async.

## Module Dependency Graph

```
config.py ← (all modules)
    │
    ├── db.py ← events.py, device_registry.py, accounts.py, dashboard/*
    │
    ├── crypto.py ← app_lifecycle.py, account_creator.py
    │
    ├── models.py ← accounts.py, production/*
    │
    ├── device/
    │   ├── wda_client.py ← warming.py, app_lifecycle.py, onboarding.py, account_creator.py
    │   ├── warming.py ← scheduler.py
    │   ├── device_registry.py ← scheduler.py, dashboard/devices.py
    │   ├── app_lifecycle.py ← scheduler.py, account_creator.py
    │   ├── account_creator.py ← scheduler.py
    │   └── scheduler.py ← dashboard/scheduler.py, __main__.py
    │
    ├── auth/
    │   ├── totp.py ← app_lifecycle.py, account_creator.py
    │   ├── email_verifier.py ← account_creator.py
    │   ├── sms_verifier.py ← account_creator.py
    │   └── captcha_solver.py ← account_creator.py
    │
    ├── dashboard/
    │   ├── app.py (FastAPI app)
    │   └── routes/ (overview, accounts, devices, events, scheduler, settings)
    │
    └── __main__.py (CLI entry point)
```

## Data Flow

### Warming Session

```
Scheduler Thread
    │
    ├── 1. update_heartbeat(device_id)
    ├── 2. _wait_for_wda(device) — poll /status until ready
    ├── 3. _get_next_task(device_id) — SQL: FOR UPDATE SKIP LOCKED
    │       Priority: warm existing > create new account
    │
    ├── 4. delete_app(wda, platform) — IDFV isolation
    ├── 5. install_from_app_store(wda, platform)
    ├── 6. login_account(wda, account) — decrypt creds → platform login
    ├── 7. run_warming(wda, config) — 30 min of platform-specific behavior
    ├── 8. UPDATE accounts SET last_warmed_at, warming_day_count, current_state
    └── 9. emit event → system_events table
```

### Account Creation

```
Scheduler Thread (when no warming tasks remain)
    │
    ├── 1. _pick_niche_for_platform() — fewest accounts
    ├── 2. delete_app → install_from_app_store
    ├── 3. Platform-specific signup flow:
    │       ├── Birthday picker
    │       ├── Email entry
    │       ├── CAPTCHA (CapSolver API)
    │       ├── Email verification (IMAP polling)
    │       ├── SMS verification (TextVerified API)
    │       ├── Password creation
    │       └── Profile setup
    ├── 4. Generate TOTP secret
    ├── 5. INSERT INTO accounts (encrypted credentials)
    └── 6. emit event
```
