# Cellular IP Strategy

Replace proxy services with real cellular data plans on each iPhone. Mobile IPs from carrier CGNAT pools are inherently trusted by platforms (TikTok, Instagram are mobile-first apps). Cycling cellular data between tasks assigns a new IP from the carrier's pool automatically.

## Why Cellular Beats Proxies

| Factor | Residential Proxies | Real Cellular |
|--------|-------------------|---------------|
| IP trust level | Medium (detectable) | Highest (genuine mobile) |
| Monthly cost (8 devices) | $300-500 | $200-240 |
| IP rotation | API call / timed | Cellular-data reset |
| Complexity | Proxy config per device | SIM + cellular-only guard |
| Detection risk | Proxy ASN fingerprinting | None — indistinguishable from real user |

## Carrier Selection

Split across 2-3 carriers for IP pool diversity. Same-carrier clusters from one ASN can be pattern-detected.

### Recommended Split (8 phones)

| Carrier | Network | Lines | Plan | Per Line | Total |
|---------|---------|-------|------|----------|-------|
| US Mobile (GSM) | T-Mobile | 4 | Unlimited | $25/mo | $100 |
| US Mobile (Warp) | Verizon | 2 | Unlimited | $25/mo | $50 |
| Tello or Red Pocket | T-Mobile or AT&T | 2 | Unlimited | $25/mo | $50 |
| **Total** | | **8** | | | **$200/mo** |

### Why Unlimited is Required

32 sessions/device/day x 30 min = 16 hours of video consumption per device per day. At 3-5 MB/min for mobile video streaming = **90-150 GB/month per device**. Low-data plans won't work.

### Carrier Comparison

| Carrier | Network | Unlimited Price | Multi-Line Dashboard | eSIM | Throttle Point |
|---------|---------|-----------------|---------------------|------|----------------|
| **US Mobile** | T-Mobile or Verizon | $25/mo | Yes | Yes | 75GB premium data |
| Mint Mobile | T-Mobile | $30/mo (annual) | No | Yes | 40GB |
| Tello | T-Mobile | $25/mo | No | Yes | None (deprioritized always) |
| Visible | Verizon | $25/mo | No | Yes | None (deprioritized always) |
| Red Pocket | AT&T/T-Mobile | $25/mo | No | Some | Varies |
| T-Mobile Connect | T-Mobile | $35/mo | No | Yes | 50GB |

**US Mobile** is preferred: multi-line dashboard, carrier choice per line, eSIM, decent premium data cap.

### Deprioritization Considerations

MVNOs get deprioritized during network congestion. This can cause:
- Slow app installs from App Store (30-90s becomes 2-5 min)
- Video buffering during warming sessions
- WDA timeouts if connectivity drops

Mitigation: premium data tiers, off-peak scheduling, retry logic on install failures.

## IP Rotation via Airplane Mode

### How It Works

When airplane mode is toggled off, the phone reconnects to the carrier tower and receives a new IP from the carrier's CGNAT (Carrier-Grade NAT) pool. This is:
- Automatic and free
- Produces IPs indistinguishable from any other mobile user
- Different IP each time (carriers have large pools)

### Implementation

The runtime now uses cellular-data resets instead of airplane-mode rotation. Between tasks, Control Center is used to disable cellular data for 60 seconds, then restore it and prove the carrier path is back:

```
[swipe down from top-right] -> [tap cellular icon OFF] -> [wait 60s] -> [tap cellular icon ON] -> [wait for connectivity]
```

This is integrated into a hardened device preflight and a continuous network guard. Every task first proves the phone is in the expected radio state, and only between-task reset windows are allowed to cycle the carrier session:

```
Session N:
  0. [verify airplane OFF + cellular ON + Wi-Fi OFF]
  1. [optional cellular data OFF 60s → ON] → new carrier session / IP
  2. [delete app] → new IDFV
  3. [install app]
  4. [login account_X]
  5. [warm / create / sign up]
```

Each session gets: **new IP** + **new IDFV** = maximum isolation.

### Connectivity Verification

Before any persona-facing task, verify the radio state in Control Center:
1. Airplane mode is OFF
2. Cellular data is ON
3. Wi-Fi is OFF
4. A lightweight Safari probe succeeds over carrier data

For between-task reset flows, do not assume success after tapping. The runtime must:
1. Verify airplane mode started OFF
2. Turn cellular data OFF
3. Wait 60 seconds
4. Turn cellular data back ON
5. Re-check airplane OFF, cellular ON, and Wi-Fi OFF
6. Prove the carrier path is reachable before continuing

### Timing Budget

| Step | Duration |
|------|----------|
| Cellular data OFF | ~1s |
| Wait | 60s |
| Cellular data ON | ~1s |
| Cellular reconnection | 3-10s |
| Carrier probe | 4-10s |
| **Total** | **~68-82s** |

This fits within the existing 15-min overhead budget.

## Session Flow (Updated)

```
Scheduler Thread
    |
    +-- 1. update_heartbeat(device_id)
    +-- 2. _wait_for_wda(device) — poll /status until ready
    +-- 3. _get_next_task(device_id) — SQL: FOR UPDATE SKIP LOCKED
    |
    +-- 4. ensure_airplane_mode_off()             <-- hard guard
    +-- 5. ensure_cellular_data_on()              <-- hard guard
    +-- 6. ensure_wifi_off()                      <-- hard guard
    +-- 7. probe_cellular_connectivity()          <-- hard guard
    +-- 8. reset_cellular_data_connection()?      <-- seeder/email reset only
    +-- 9. delete_app(wda, platform)              — IDFV isolation
    +-- 10. install_from_app_store(wda, platform)
    +-- 11. login_account(wda, account) — decrypt creds -> platform login
    +-- 12. run_warming(wda, config) — 30 min of platform-specific behavior
    +-- 13. UPDATE accounts SET last_warmed_at, warming_day_count, current_state
    +-- 14. emit event -> system_events table
```

## Physical Setup

### eSIM vs Physical SIM

- **eSIM**: simpler, no physical SIM swapping. iPhone XS and later support eSIM.
- **Physical SIM**: works on all iPhones. Use nano-SIM.
- Can use both simultaneously for redundancy (eSIM primary, physical SIM fallback).

### Per-Device Setup

1. Insert SIM / activate eSIM
2. Settings -> Cellular -> enable cellular data
3. Disable Wi-Fi (Settings -> Wi-Fi -> OFF) so all traffic routes through cellular
4. Disable auto-join for any known Wi-Fi networks
5. Verify: Settings -> Cellular -> shows carrier name and signal bars

### Important: Disable Wi-Fi

All devices **must** have Wi-Fi disabled to ensure traffic routes through cellular. If Wi-Fi is on, the phone will prefer Wi-Fi and the carrier reset will not change the active network path.

Do not rely on the toggle alone. The runtime now runs a continuous network guard during idle/cooldown windows, re-checks airplane mode, cellular data, and Wi-Fi before work, and aborts the task if it cannot prove the device is back on healthy cellular-only networking.
