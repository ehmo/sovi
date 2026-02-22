# SOVI Documentation

**Social Video Intelligence & Distribution Network**

Automated social media account farm and content distribution system. Manages fleets of iOS devices to create, warm, and operate TikTok and Instagram accounts, then produces and distributes short-form video content through those accounts.

## Documents

| Document | Description |
|----------|-------------|
| [Architecture](architecture.md) | System overview, infrastructure, design decisions, module dependencies, data flows |
| [Device Automation](device-automation.md) | WDA client, warming classes, app lifecycle, account creator, device registry, scheduler |
| [Warming Strategy](warming-strategy.md) | Warming phases, IDFV isolation, human emulation, timing, scaling |
| [Auth System](auth-system.md) | TOTP, email verification, SMS verification, CAPTCHA solving, credential encryption |
| [Database Schema](database-schema.md) | All tables, columns, indexes, migrations, connection details |
| [Dashboard & API](dashboard-api.md) | FastAPI routes, REST API endpoints, htmx UI, SSE streaming |
| [Event System](event-system.md) | Structured event logging, categories, severity levels, querying, resolution |
| [Content Pipeline](content-pipeline.md) | Research, scriptwriting, asset generation, video assembly, distribution |
| [CLI Reference](cli-reference.md) | All commands, options, configuration variables |
| [Infrastructure Setup](infrastructure-setup.md) | Installation, device setup, service management, deployment, troubleshooting |
| [gRPC Protocol](grpc-protocol.md) | Aspirational device daemon protocol definition |

## Quick Start

```bash
# On studio
ssh studio
cd ~/Work/ai/sovi

# Check system health
.venv/bin/python -m sovi health

# Dashboard (already running as launchd service)
curl http://localhost:8888/api/overview

# Start scheduler
.venv/bin/python -m sovi scheduler start

# List accounts
.venv/bin/python -m sovi accounts list

# List devices
.venv/bin/python -m sovi devices list
```

## Key Numbers

| Metric | Value |
|--------|-------|
| Warming session | 30 min |
| Session overhead | 15 min |
| Sessions/device/day | 32 |
| Current devices | 2 |
| Active niches | 5 |
| Supported platforms | TikTok, Instagram |
| Dashboard port | 8888 |
| WDA ports | 8100, 8101 |
