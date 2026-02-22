# Warming Strategy

## Goals

- **95% device utilization** — 24/7 operation with minimal idle time
- **32 sessions per device per day** — 30 min warming + 15 min overhead = 45 min/session
- **10+ device capacity** — 320+ accounts across all niches
- **IDFV isolation** — Each session appears as a fresh device installation
- **Human-like behavior** — Variable timing, natural engagement patterns, rate-limited actions

## Session Timing

| Component | Duration | Notes |
|-----------|----------|-------|
| App delete | ~10s | WDA uninstall or springboard method |
| App install | 30-90s | App Store search + download |
| Login | 15-30s | Email/password + TOTP if needed |
| Warming | 30 min | Platform-specific browsing + engagement |
| Cooldown | 30s | Brief pause between sessions |
| **Total** | **~45 min** | **32 sessions/device/day** |

## Phase Progression

### Phase 1: PASSIVE (Days 1-3)

**Goal:** Establish a consumption pattern without any interactions.

| Platform | Behavior |
|----------|----------|
| TikTok | Watch FYP videos. 30% chance of full watch (20-60s), 70% partial (5-25s). No likes, no follows, no comments. |
| Instagram | 40% feed scrolling, 60% Reels watching. Similar timing to TikTok. |

**Rationale:** New accounts that immediately start engaging are flagged. Pure consumption trains the algorithm and establishes a baseline behavioral fingerprint.

### Phase 2: LIGHT (Days 4-7)

**Goal:** Begin light engagement to build reputation signals.

| Platform | Behavior |
|----------|----------|
| TikTok | 5-10 likes (double-tap), 3-7 follows. 30-90s gap between likes, 30-60s between follows. |
| Instagram | 5-10 likes, 3-5 follows via Follow button. Same rate limiting. |

**Rate limits:**
- Max likes per session: 5-10 (randomized)
- Max follows per session: 3-7 (randomized)
- Min gap between actions: 30s
- Like probability per video: 12-15%
- Follow probability per video: 6%

### Phase 3: MODERATE (Days 8-14)

**Goal:** First content posts + increased engagement.

Account is ready for light content distribution. Warming continues but engagement limits increase.

### Phase 4: ACTIVE (Day 14+)

**Goal:** Full operation — regular posting + continued warming.

Account transitions to `active` state and becomes eligible for content distribution. Daily warming sessions continue to maintain the account's behavioral profile.

## IDFV Isolation Strategy

**Problem:** Platforms track IDFV (Identifier for Vendor) to fingerprint devices. Multiple accounts from the same IDFV get linked and flagged.

**Solution:** Delete and reinstall the app before every warming session.

```
Session N:   [delete TikTok] → [install TikTok] → [login account_A] → [warm 30m]
Session N+1: [delete TikTok] → [install TikTok] → [login account_B] → [warm 30m]
```

Each installation generates a new IDFV, so platforms see each session as a different device. The delete→install cycle takes ~60-90s, which is included in the 15-min overhead budget.

**Important:** The App Store must be signed in on the device for reinstallation to work. Apps download from purchase history ("cloud download") which is faster than a fresh install.

## Human Emulation Techniques

### Timing Variation

Every delay uses `random.uniform(min, max)` to avoid predictable patterns:

```python
# Video watch times
watch_time = random.uniform(5, 25)      # Partial watch
watch_time = random.uniform(20, 60)     # Full watch (30% chance)

# Swipe duration
duration = random.uniform(0.3, 0.6)     # Natural scroll speed

# Post-swipe delay
time.sleep(random.uniform(0.5, 1.5))    # Before next video loads

# Random "zoning out" (8% chance)
time.sleep(random.uniform(5, 15))
```

### Alert Handling

System alerts (notifications, tracking) are handled with lightweight checks every 5 videos. We avoid heavy element searches (which trigger WDA to traverse the full UI tree) and instead just check for alert text.

```python
# Lightweight — only checks alert bar
alert_text = self.wda.get_alert_text()

# Heavy — avoid this during warming loops
# el = self.wda.find_element("accessibility id", "Allow")
```

### Engagement Rate Limiting

Actions are gated by both probability and hard caps:

```python
max_likes = random.randint(5, 10)       # Randomized cap per session
if likes < max_likes and random.random() < 0.15:  # 15% per-video chance
    self.auto.like_current()
    time.sleep(random.uniform(30, 90))  # Long gap after action
```

### Niche Hashtag Training

TikTok warmers can search niche-specific hashtags to train the For You Page algorithm toward the account's content vertical:

```python
warmer.search_niche_hashtags(["personalfinance", "investing", "moneytips"])
```

This makes the account's FYP show content similar to what it will eventually post, creating a more natural profile.

## Scheduler Task Priority

The scheduler always prefers warming existing accounts over creating new ones:

```sql
-- Priority 1: Warm existing (not yet warmed today)
SELECT * FROM accounts
WHERE current_state IN ('created', 'warming_p1', 'warming_p2', 'warming_p3', 'active')
  AND platform IN ('tiktok', 'instagram')
  AND deleted_at IS NULL
  AND (last_warmed_at IS NULL OR last_warmed_at < CURRENT_DATE)
ORDER BY
  CASE current_state
    WHEN 'created' THEN 0      -- Newest accounts first
    WHEN 'warming_p1' THEN 1
    WHEN 'warming_p2' THEN 2
    WHEN 'warming_p3' THEN 3
    WHEN 'active' THEN 4       -- Active accounts last
  END,
  last_warmed_at ASC NULLS FIRST
LIMIT 1
FOR UPDATE SKIP LOCKED;         -- Prevent thread conflicts

-- Priority 2: Create new account (only when nothing to warm)
-- Picks platform with fewer accounts to maintain balance
```

## Account Fleet Scaling

| Devices | Accounts/Day Created | Total at 30 Days | Sessions/Day |
|---------|---------------------|-------------------|--------------|
| 2 | 2-4 | 60-120 | 64 |
| 5 | 5-10 | 150-300 | 160 |
| 10 | 10-20 | 300-600 | 320 |

The system self-scales: when all existing accounts have been warmed for the day, the scheduler automatically creates new accounts on the platform/niche with the fewest accounts.
