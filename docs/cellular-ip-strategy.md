# Cellular IP Strategy

Replace proxy services with real cellular data plans on each iPhone. Mobile IPs from carrier CGNAT pools are inherently trusted by platforms (TikTok, Instagram are mobile-first apps). Toggling airplane mode assigns a new IP from the carrier's pool automatically.

## Why Cellular Beats Proxies

| Factor | Residential Proxies | Real Cellular |
|--------|-------------------|---------------|
| IP trust level | Medium (detectable) | Highest (genuine mobile) |
| Monthly cost (8 devices) | $300-500 | $200-240 |
| IP rotation | API call / timed | Airplane mode toggle |
| Complexity | Proxy config per device | SIM + airplane toggle |
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

Airplane mode toggle is done via WDA by navigating Control Center:

```
[swipe down from top-right] -> [tap airplane icon] -> [wait 3s] -> [tap airplane icon again] -> [wait for connectivity]
```

This is integrated into the scheduler's session flow as Step 0, before app deletion:

```
Session N:
  0. [airplane mode ON → OFF] → new IP
  1. [delete app] → new IDFV
  2. [install app]
  3. [login account_X]
  4. [warm 30 min]
```

Each session gets: **new IP** + **new IDFV** = maximum isolation.

### Connectivity Verification

After airplane mode toggle, verify the device has connectivity before proceeding:
1. Poll a lightweight endpoint (e.g., generate.apple.com/204) via WDA/Safari
2. Alternatively, just wait 5-8s (cellular reconnection is fast)
3. If no connectivity after 15s, retry the toggle

### Timing Budget

| Step | Duration |
|------|----------|
| Airplane mode ON | ~1s |
| Wait | 3s |
| Airplane mode OFF | ~1s |
| Cellular reconnection | 3-8s |
| **Total** | **~8-13s** |

This fits within the existing 15-min overhead budget.

## Session Flow (Updated)

```
Scheduler Thread
    |
    +-- 1. update_heartbeat(device_id)
    +-- 2. _wait_for_wda(device) — poll /status until ready
    +-- 3. _get_next_task(device_id) — SQL: FOR UPDATE SKIP LOCKED
    |
    +-- 4. toggle_airplane_mode(wda)     <-- NEW: rotate IP
    +-- 5. delete_app(wda, platform)      — IDFV isolation
    +-- 6. install_from_app_store(wda, platform)
    +-- 7. login_account(wda, account) — decrypt creds -> platform login
    +-- 8. run_warming(wda, config) — 30 min of platform-specific behavior
    +-- 9. UPDATE accounts SET last_warmed_at, warming_day_count, current_state
    +-- 10. emit event -> system_events table
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

All devices **must** have Wi-Fi disabled to ensure traffic routes through cellular. If Wi-Fi is on, the phone will prefer Wi-Fi and the airplane mode IP rotation won't work (Wi-Fi IP stays the same).

The airplane mode toggle also disables Wi-Fi, and when airplane mode is turned back off, only cellular reconnects (Wi-Fi stays off if it was off before).
