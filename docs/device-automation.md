# Device Automation

## WDA Client (`src/sovi/device/wda_client.py`)

Direct HTTP client for WebDriverAgent — no Appium middleware.

### WDADevice

```python
@dataclass
class WDADevice:
    name: str          # Human-readable name (e.g., "iPhone-A")
    udid: str          # Device UDID
    wda_port: int      # iproxy local port → device port 8100

    @property
    def base_url(self) -> str:  # http://localhost:{wda_port}
```

### WDASession

Manages a WDA session on a single device. Key methods:

| Method | Description |
|--------|-------------|
| `connect()` | Create WDA session, cache screen size |
| `disconnect()` | Delete WDA session |
| `is_ready()` | Check WDA `/status` endpoint |
| `screen_size()` | Get device screen dimensions (cached) |
| `screenshot(save_path?)` | Capture PNG screenshot |
| `launch_app(bundle_id)` | Activate/bring app to foreground |
| `terminate_app(bundle_id)` | Kill app |
| `app_state(bundle_id)` | 1=not running, 2=bg, 3=suspended, 4=foreground |
| `find_element(using, value)` | Find single element |
| `find_elements(using, value)` | Find multiple elements |
| `element_click(element_id)` | Click element |
| `element_value(element_id, text)` | Type into element |
| `tap(x, y)` | Tap at coordinates (W3C Actions) |
| `double_tap(x, y)` | Double-tap (for likes) |
| `swipe(sx, sy, ex, ey, duration)` | Custom swipe gesture |
| `swipe_up(duration)` | Swipe up (scroll down / next video) |
| `swipe_down(duration)` | Swipe down (scroll up) |
| `get_alert_text()` | Get system alert text |
| `accept_alert()` / `dismiss_alert()` | Handle system alerts |
| `press_button(name)` | Hardware button: home, volumeUp, volumeDown |
| `source()` | Get page XML source |

**Element finding strategies:**
- `accessibility id` — Most reliable, preferred
- `predicate string` — iOS NSPredicate (e.g., `label == "Follow" AND type == "XCUIElementTypeButton"`)
- `class chain` — XCUITest class chains (e.g., `**/XCUIElementTypeSearchField`)
- `xpath` — Slowest, avoid if possible

**Timeout design:**
- Main client: 60s timeout (for heavy operations like page source)
- Gesture client: 10s timeout (swipes/taps execute fast but WDA can be slow to respond)

### DeviceAutomation

High-level helper on top of WDASession:

| Method | Description |
|--------|-------------|
| `launch(app_name)` | Launch by name (resolves bundle ID), dismiss popups |
| `dismiss_popups(max_attempts)` | Dismiss system alerts + in-app modals |
| `like_current()` | Double-tap center of screen |
| `tap_element(using, value)` | Find and tap element, returns bool |
| `human_delay(min_s, max_s)` | Random sleep for human-like timing |

**Bundle IDs:**

| App | Bundle ID |
|-----|-----------|
| TikTok | `com.zhiliaoapp.musically` |
| Instagram | `com.burbn.instagram` |
| YouTube | `com.google.ios.youtube` |
| Reddit | `com.reddit.Reddit` |
| X/Twitter | `com.atebits.Tweetie2` |

## Warming (`src/sovi/device/warming.py`)

### Warming Phases

| Phase | Days | Behavior |
|-------|------|----------|
| PASSIVE (1) | 1-3 | Pure consumption, zero interactions |
| LIGHT (2) | 4-7 | Likes (5-10), follows (3-7) |
| MODERATE (3) | 8-14 | First posts + moderate engagement |
| ACTIVE (4) | 14+ | Full operation |

### Platform Warmers

Each warmer class has `passive_consumption()` and `light_engagement()` methods:

**TikTokWarmer:**
- Passive: Watch FYP videos (5-60s each), occasional 5-15s pauses, swipe to next
- Light: Like (double-tap, max 5-10), follow (max 3-7), with 30-90s gaps between actions
- Also: `search_niche_hashtags()` to train the algorithm

**InstagramWarmer:**
- Passive: 40% feed browsing, 60% Reels watching
- Light: Like (max 5-10), follow (max 3-5), find Follow buttons via predicate

**RedditWarmer:**
- Passive: Browse home feed, occasionally tap into posts to read comments
- Light: Upvote (max 5-15), find upvote button via predicate

**YouTubeWarmer:**
- Passive: 40% Home feed, 60% Shorts (swipe-up like TikTok)
- Light: Like Shorts (max 3-8)

**XTwitterWarmer:**
- Passive: Browse timeline, occasionally tap tweets to read replies
- Light: Like (max 5-12)

### Human-Like Behavior Patterns

All warmers incorporate:
- **Variable watch times**: 5-60s per video, 30% chance of watching to completion
- **Alert dismissal**: Every 5-8 videos, check for system alerts (lightweight, no heavy element search)
- **Random pauses**: 5-15% chance of 5-30s "zoning out" breaks
- **Rate-limited actions**: 30-90s minimum gap between likes, 30-60s between follows
- **Swipe variation**: Duration 0.3-0.8s to mimic natural scrolling

## App Lifecycle (`src/sovi/device/app_lifecycle.py`)

Manages the full delete → install → login cycle per warming session.

### delete_app(wda, platform)

1. Terminate app if running
2. Press Home
3. Try WDA `/wda/apps/uninstall` endpoint (fastest)
4. Fallback: Springboard method (long-press → Remove App → Delete → Confirm)
5. Emit event to `system_events`

### install_from_app_store(wda, platform)

1. Open App Store (`com.apple.AppStore`)
2. Tap Search tab
3. Find search field, type app name
4. Press Search on keyboard
5. Tap GET/Install/cloud download button
6. Poll `app_state()` until installed (up to 120s timeout)
7. Go Home

### login_tiktok(wda, email, password, totp_secret)

1. Launch TikTok
2. Find "Use phone / email / username" or "Log in"
3. Switch to email/username login
4. Enter email into text field
5. Enter password into secure text field
6. Tap "Log in"
7. Handle TOTP 2FA if prompted (enter code from `pyotp`)
8. Dismiss popups, verify on FYP

### login_instagram(wda, email, password)

1. Launch Instagram
2. Find "I already have an account" or "Log in"
3. Enter email/username
4. Enter password
5. Tap "Log in"
6. Dismiss save login info, notifications, etc.
7. Verify logged in (check for Home element)

### login_account(wda, account)

Dispatcher that decrypts credentials from the account dict and calls the appropriate platform login function.

## Account Creator (`src/sovi/device/account_creator.py`)

Full automated signup flow.

### Username Generation

Niche-aware prefixes:
- personal_finance → money, wealth, finance, cash, invest
- ai_storytelling → story, tales, narrative, fiction, epic
- tech_ai_tools → tech, ai, digital, code, smart
- motivation → grind, hustle, mindset, growth, win
- true_crime → crime, mystery, case, detective, unsolved

Format: `{prefix}{3-6 digits}` (e.g., `grind4829`)

### Signup Flow

1. Delete app (IDFV reset)
2. Install from App Store
3. Platform-specific signup:
   - Birthday picker (random adult DOB 1990-2002)
   - Email entry
   - CAPTCHA solving (CapSolver screenshot-based)
   - Email verification (IMAP polling)
   - SMS verification (TextVerified disposable number)
   - Password entry
   - Username setting
   - Skip interests/profile photo/contacts
4. Generate TOTP secret
5. Insert into DB with encrypted credentials

### auto_create_account()

Convenience function that auto-picks the niche with fewest accounts on the target platform, then calls `create_account()`.

## Device Registry (`src/sovi/device/device_registry.py`)

DB-driven device management replacing hardcoded device lists.

### Sync API (for scheduler threads)

| Function | Description |
|----------|-------------|
| `get_active_devices()` | All devices with status='active' |
| `get_device_by_id(id)` | Single device lookup |
| `get_device_by_name(name)` | Lookup by name |
| `to_wda_device(row)` | Convert DB row → WDADevice dataclass |
| `update_heartbeat(id)` | Touch `updated_at` + set status=active |
| `set_device_status(id, status)` | active/maintenance/failed/disconnected |
| `register_device(name, udid, ...)` | Upsert by UDID |

### Async API (for dashboard)

Mirrors sync API: `async_get_devices()`, `async_get_device()`, `async_register_device()`, `async_get_device_sessions()`.

### launchd Plist Generation

`generate_launchd_plists(device, output_dir)` creates two plists per device:
1. `com.sovi.iproxy-{name}.plist` — iproxy tunnel (KeepAlive)
2. `com.sovi.wda-{name}.plist` — WebDriverAgent xcodebuild test (KeepAlive)

## Scheduler (`src/sovi/device/scheduler.py`)

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| WARMING_DURATION_MIN | 30 | Warming time per session |
| OVERHEAD_MIN | 15 | Delete + install + login + cooldown |
| SESSION_TOTAL_MIN | 45 | Total per session |
| SESSIONS_PER_DAY | 32 | 24*60/45 = 32 sessions/device/day |
| WARMABLE_PLATFORMS | tiktok, instagram | Active platforms |

### DeviceScheduler

Singleton class (accessed via `get_scheduler()`).

**Lifecycle:**
1. `start()` — Queries active devices, spawns daemon threads
2. `_device_loop(device, dt)` — Infinite loop per device
3. `stop()` — Sets stop event, joins threads (30s timeout)

**Device Loop:**
```
while not stopped:
    heartbeat()
    if not wait_for_wda(): backoff 60s, continue
    task = get_next_task()
    if task is None: idle 30s, continue
    if task.type == "warm": execute_warming()
    if task.type == "create": execute_creation()
    sessions_today += 1
    cooldown 30s
```

**Task Priority:**
1. Warm existing account (not yet warmed today, earlier phases first)
   - Uses `FOR UPDATE SKIP LOCKED` to prevent thread conflicts
   - Orders by state priority: created > warming_p1 > p2 > p3 > active
2. Create new account (platform with fewest accounts)

**State Transitions:**
- Days 1-3 → `warming_p1`
- Days 4-7 → `warming_p2`
- Days 8-14 → `warming_p3`
- Days 14+ → `active`
