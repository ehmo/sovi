# SOVI Tooling Plan

Concrete tool stack for physical iOS device automation, debugging, and future Apple-project work.

This plan assumes the current SOVI architecture remains intact:

- Real iOS devices connected over USB
- `iproxy` tunnels per device
- Direct `WebDriverAgent` HTTP sessions
- Python orchestration on top of WDA

See also:

- [Architecture](architecture.md)
- [Device Automation](device-automation.md)
- [Infrastructure Setup](infrastructure-setup.md)

## Goals

- Keep the runtime path simple and stable
- Improve debugging without adding middleware to the hot path
- Reserve Apple-project tooling for the parts of the system that actually need it
- Avoid buying or adopting tools that do not match the current repo shape

## Decision Summary

| Category | Tool | Role | Status |
|----------|------|------|--------|
| Runtime automation | WebDriverAgent | Primary on-device UI automation API | Keep |
| Runtime transport | `iproxy` + `libimobiledevice` | USB tunnels and device plumbing | Keep |
| Service supervision | `launchd` | KeepAlive for tunnels, WDA, dashboard | Keep |
| Human UI inspection | Appium Inspector | Attach to running WDA for hierarchy and selector debugging | Add |
| Manual accessibility inspection | Accessibility Inspector | Inspect labels, traits, frames, and accessibility issues | Add |
| Logs and crash triage | Console.app + device logs | Runtime diagnostics during failed flows | Add |
| Apple project workflows | FlowDeck | Build, run, test, simulator/device ops for owned Apple code | Optional |
| Owned-app agent control | Haptix | Only useful for internal apps we own and can instrument | Defer |
| Cross-platform UI YAML flows | Maestro | Not aligned with current physical-device-first WDA stack | Skip |

## Tool Boundaries

### 1. Core Runtime Stack

These stay on the critical path.

#### WebDriverAgent

Use WDA as the primary automation surface for:

- app launch and termination
- screenshots
- element queries
- tap, swipe, and scroll gestures
- alert handling
- app state checks

Reason:

- SOVI already uses direct WDA HTTP and has a stable session abstraction in `src/sovi/device/wda_client.py`.
- Replacing it would create churn across the scheduler, warming flows, app lifecycle, and signup flows.

#### `iproxy` and `libimobiledevice`

Use these for:

- USB tunnel management
- UDID discovery
- device connectivity checks
- low-level device operations outside the Python runtime

Reason:

- This is already how devices are exposed to the local host.
- It is the correct low-level layer under WDA.

#### `launchd`

Use `launchd` as the long-running service manager for:

- `iproxy`
- WDA per device
- dashboard services

Reason:

- Device automation is long-lived and operational, not ad hoc.
- KeepAlive behavior is already documented and deployed.

### 2. Debug And Inspection Stack

These tools should help humans debug. They should not sit in the runtime hot path.

#### Appium Inspector

Add Appium Inspector as the primary live inspection tool for real devices.

Use it for:

- browsing current UI hierarchy
- testing locator strategies before encoding them in Python
- inspecting element attributes during broken flows
- validating whether selector failures are app-state issues or code issues

Rules:

- Attach to an already running WDA-backed device session when possible
- Do not introduce Appium server as required middleware for scheduler execution
- Do not rewrite runtime automation around Appium unless WDA proves insufficient

Why it fits:

- SOVI already speaks WDA semantics
- Inspector improves operator and developer visibility without changing production behavior

#### Accessibility Inspector

Add Accessibility Inspector to the manual debugging workflow.

Use it for:

- understanding labels and traits on unstable UI
- verifying that accessibility identifiers or labels match what WDA sees
- comparing what the app visually shows vs. what the accessibility tree exposes

Rules:

- Use for manual investigation only
- Treat it as a truth source for debugging ambiguous selectors

#### Console.app And Device Logs

Add log capture to every serious debugging pass.

Use it for:

- app launch failures
- stuck or partial navigation
- permission prompts and system interruptions
- crash or hang triage

Rules:

- Screenshot plus source without logs is incomplete
- Device dump workflows should capture recent logs alongside WDA artifacts

### 3. Apple Project Tooling

These tools are only valuable when SOVI touches owned Apple code.

#### FlowDeck

FlowDeck is not the primary SOVI runtime tool. It becomes useful for:

- custom WDA build or signing workflows
- owned helper apps
- simulator repro harnesses
- build, run, test, install, launch, and log workflows for internal Apple projects

Use FlowDeck if SOVI adds:

- an Xcode project in-repo
- an internal companion iOS app
- a forked/customized WDA project that needs repeatable build and install steps
- simulator-based regression harnesses

Do not use FlowDeck to replace:

- direct WDA session control
- existing Python orchestration over WDA
- current device scheduler semantics

#### Haptix

Do not adopt Haptix for current SOVI runtime work.

Revisit only if SOVI adds an internal app that:

- we own
- we can modify
- we want an AI agent to inspect and control visually

Haptix is not a replacement for:

- WDA over third-party apps
- the current device farm runtime path

### 4. Deferred Or Skipped Tools

#### Maestro

Skip for now.

Reason:

- current SOVI automation is physical-device-first and WDA-based
- adding a parallel flow-definition system adds complexity without clear payoff

#### Appium Server In Production Path

Do not add Appium server as required middleware in front of WDA.

Reason:

- SOVI explicitly chose direct WDA to avoid extra latency, crash surface, and complexity
- Inspector is the useful part; runtime Appium is not

## Concrete Additions To Make In SOVI

These are the next practical steps.

### Near-Term

1. Add `scripts/device-dump.sh`

Purpose:

- capture WDA `/status`
- capture screenshot
- capture page source
- capture current app state
- capture recent logs
- write all artifacts to a timestamped per-device bundle

2. Add `scripts/restart-device-stack.sh`

Purpose:

- restart `iproxy` and WDA launch agents for a single device
- standardize the current manual recovery flow

3. Add `src/sovi/device/diagnostics.py`

Purpose:

- central Python helper for diagnostic capture
- shared by CLI, scripts, and future dashboard actions

4. Add a CLI command for diagnostics

Examples:

- `sovi devices dump --name iPhone-A`
- `sovi devices restart-stack --name iPhone-A`

5. Add operator-facing Make targets

Examples:

- `make health`
- `make dump DEVICE=iPhone-A`
- `make restart-device DEVICE=iPhone-A`
- `make inspect DEVICE=iPhone-A`

### Medium-Term

6. Add an inspection runbook

Suggested file:

- `docs/debugging-device-flows.md`

Include:

- how to attach Appium Inspector
- how to use Accessibility Inspector
- how to collect logs
- how to compare WDA source vs. observed UI

7. Add dashboard hooks for diagnostics

Examples:

- trigger per-device dump from the dashboard
- expose latest artifact bundle
- expose last known screenshot and source snapshot

### Long-Term

8. Reevaluate FlowDeck if SOVI grows owned Apple code

Trigger conditions:

- internal helper app
- custom WDA fork
- in-repo Xcode project
- simulator regression workflows

9. Reevaluate Haptix only for owned internal apps

Trigger conditions:

- need AI-agent visual control over an internal app
- internal tooling app exists and can be instrumented with `HaptixKit`

## Adoption Order

Follow this order:

1. Keep direct WDA as-is
2. Add diagnostics and recovery scripts
3. Add Appium Inspector for live hierarchy debugging
4. Add Accessibility Inspector to the manual debug workflow
5. Add log capture to device dump bundles
6. Revisit FlowDeck only when owned Apple-project work appears
7. Revisit Haptix only when owned internal app work appears

## Use-Case Matrix

| Need | Preferred Tool |
|------|----------------|
| Drive real device UI at runtime | WDA |
| Inspect live hierarchy and test selectors | Appium Inspector |
| Inspect accessibility metadata manually | Accessibility Inspector |
| Restart device tunnel and WDA services | `launchd` + scripts |
| Capture runtime logs and crash context | Console.app + device log tooling |
| Build or run owned Apple code | FlowDeck |
| AI-agent control of an internal app we own | Haptix |

## Non-Goals

This plan does not attempt to:

- replace the current WDA runtime with a new middleware layer
- move the scheduler to simulators
- introduce multiple overlapping runtime automation stacks
- add paid tools without a clear gap they uniquely solve

## Current Recommendation

Adopt this stack now:

- WDA
- `iproxy`
- `libimobiledevice`
- `launchd`
- Appium Inspector
- Accessibility Inspector
- device log tooling

Keep these in reserve:

- FlowDeck for future owned Apple-project work
- Haptix for future internal instrumented apps
