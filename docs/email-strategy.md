# Email Strategy for Account Creation

## The Core Problem

500+ social media accounts each need a unique email address. The email domain itself is a correlation signal — 100 accounts all using `@mydomain.com` is an obvious farm. The ideal is emails that look like normal people: a mix of `@outlook.com`, `@gmail.com`, `@yahoo.com`, `@icloud.com` — the same distribution a real population would have.

Gmail and Yahoo require phone numbers at signup, and re-challenge with SMS on new device logins. Keeping 500 phone numbers active is prohibitively expensive. So the question becomes: **which major email providers let you create accounts without a persistent phone number, at scale, with IMAP access?**

## Technical Requirements from Codebase

- `email_verifier.py` polls IMAP for verification codes using `imapclient`
- `poll_for_code()` supports `target_email` param for filtering a shared inbox
- `account_creator.py` takes `email` + `imap_config` as parameters
- Scale: 500-1000 accounts across TikTok and Instagram
- Need to receive only (verification codes), rarely send

## Strategy: Create Real Email Accounts on Major Providers

The insight: **you already have 8 iPhones with mobile data and IP rotation**. This is exactly what you need to create email accounts that look legitimate. Mobile IPs are trusted by email providers and dramatically reduce phone verification challenges.

### Tier 1: Outlook/Hotmail (Primary — target 50-60% of emails)

Microsoft's Outlook.com is the best option at scale:

- **Phone verification is often skippable** from clean mobile IPs. Microsoft uses risk scoring — mobile IPs from major US carriers score low risk. When the IP is clean, they offer CAPTCHA-only signup.
- **IMAP access**: `imap-mail.outlook.com:993` — works with the existing `imapclient` code
- **Multiple domain choices**: `@outlook.com`, `@hotmail.com` — natural diversity within one provider
- **Can be created on-device**: Safari-based signup flow, automatable via WDA
- **High trust**: TikTok and Instagram never flag Outlook/Hotmail addresses

**Creation flow on the phones:**
```
1. Cellular-data reset (fresh mobile IP)
2. Open Safari via WDA
3. Navigate to signup.live.com
4. Fill signup form (name, username, password)
5. Solve CAPTCHA if presented (CapSolver integration exists)
6. If phone required: use TextVerified ($0.50, one-time)
7. If phone NOT required: account created, done
8. Store IMAP credentials in DB
```

The key advantage: when you create from a real iPhone on a real carrier IP, Microsoft's risk model sees a legitimate user. Phone verification skip rate from mobile IPs is reportedly 40-70%.

For the 30-60% that do require phone: TextVerified at $0.50-1.00 each. But unlike Gmail, Outlook doesn't re-challenge with the same phone later — it's a one-time verification. You don't need to retain the number.

**Cost: $0 for phone-skipped accounts, $0.50-1.00 for phone-required ones.**

### Tier 2: Mail.com / GMX (Secondary — target 20-30% of emails)

Mail.com (owned by 1&1/United Internet) is underappreciated for this use case:

- **20+ built-in domain choices**: `@mail.com`, `@email.com`, `@usa.com`, `@post.com`, `@consultant.com`, `@engineer.com`, `@dr.com`, `@europe.com`, `@asia.com`, `@myself.com`, `@writeme.com`, `@cheerful.com`, etc.
- **Signup often requires no phone** — CAPTCHA only
- **Free IMAP access**: `imap.mail.com:993`
- **Not flagged**: These are real, established email domains (mail.com has been around since 1995)
- **Natural diversity**: Using 10 different @domains from one provider looks like 10 different email providers to platforms

This gives instant domain diversity without buying any domains. An account using `@engineer.com` and another using `@usa.com` appear completely unrelated.

**Creation flow**: Same as Outlook — Safari on-device, automatable via WDA.

### Tier 3: Custom Catch-All Domains (Tertiary — 10-20% of emails)

A small number of custom domains (3-5) for remaining accounts. Since they're only 10-20% of the total, the per-domain concentration drops to ~20-30 accounts per domain — much less suspicious.

- Host all on Purelymail ($10/yr) or MXroute ($25/yr)
- Catch-all: any address works instantly, no provisioning
- Domain names should look like small media brands
- One domain per niche: `@wealthdailyco.com`, `@storycraft.io`, etc.

### Tier 4: iCloud Mail (Opportunistic — bonus)

Each iPhone already has an Apple ID for App Store access. iCloud provides:

- One `@icloud.com` address per Apple ID
- IMAP access: `imap.mail.me.com:993`
- Requires app-specific password (generated in Apple ID settings)
- 8 phones = 8 iCloud emails for free

These 8 addresses are high-trust and cost nothing. Use them for the most important accounts (highest-niche-potential ones in the ACTIVE phase).

## Email Distribution Target

| Provider | Domain(s) | Count | % | Notes |
|----------|-----------|-------|---|-------|
| Outlook | `@outlook.com`, `@hotmail.com` | 250-300 | 50-60% | Highest trust, phone sometimes skippable |
| Mail.com | `@mail.com`, `@email.com`, `@usa.com`, +7 more | 100-150 | 20-30% | Natural diversity, no phone needed |
| Custom domains | 3-5 niche-specific domains | 50-100 | 10-20% | Catch-all, cheapest per-address |
| iCloud | `@icloud.com` | 8 | ~1% | Free, already exists on phones |
| **Total** | **15-18 unique domains** | **500** | **100%** | |

## Cost Projection (500 accounts)

| Item | Cost |
|------|------|
| Outlook phone verification (~40% of 300) | ~$60-120 one-time |
| Mail.com accounts | $0 (free) |
| Custom domains (5 x $12/yr) | $60/yr |
| Purelymail hosting | $10/yr |
| iCloud | $0 (already have Apple IDs) |
| **Total Year 1** | **$130-190** |
| **Total Year 2+** | **$70/yr** |

Compare: 500 Gmail accounts with retained phone numbers = $2,000+/yr.

## Implementation Architecture

### Email Account Registry

Add a table (or extend the existing schema) to track email accounts separately from social accounts:

```sql
CREATE TABLE email_accounts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider        TEXT NOT NULL,           -- 'outlook', 'mailcom', 'custom', 'icloud'
    email           TEXT NOT NULL UNIQUE,     -- ENCRYPTED
    password        TEXT NOT NULL,            -- ENCRYPTED
    imap_host       TEXT NOT NULL,
    imap_port       INT NOT NULL DEFAULT 993,
    domain          TEXT NOT NULL,            -- 'outlook.com', 'mail.com', etc.
    status          TEXT NOT NULL DEFAULT 'available',  -- available, assigned, disabled
    assigned_to     UUID REFERENCES accounts(id) ON DELETE SET NULL,
    phone_used      BOOLEAN DEFAULT false,   -- whether phone verification was needed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_email_accounts_status ON email_accounts (status) WHERE status = 'available';
CREATE INDEX idx_email_accounts_domain ON email_accounts (domain);
```

### Email Creation Pipeline

Separate from social account creation. Run as a batch job:

```
Phase 1: Email harvesting (run before account creation campaigns)
  For each needed email:
    1. Pick provider (weighted: 55% outlook, 25% mailcom, 20% custom)
    2. Pick domain within provider (round-robin mailcom domains, random custom)
    3. Cellular-data reset (fresh IP)
    4. Create email account on-device via Safari/WDA
    5. Store credentials encrypted in email_accounts table
    6. Mark as 'available'

Phase 2: Social account creation (existing flow)
  When creating a social account:
    1. Claim an available email_account (FOR UPDATE SKIP LOCKED)
    2. Pass its IMAP config to create_account()
    3. Mark email as 'assigned', link to social account
```

### IMAP Configuration per Provider

```python
IMAP_CONFIGS = {
    "outlook": {"host": "imap-mail.outlook.com", "port": 993},
    "hotmail": {"host": "imap-mail.outlook.com", "port": 993},
    "mailcom": {"host": "imap.mail.com", "port": 993},
    "icloud":  {"host": "imap.mail.me.com", "port": 993},
    "custom":  {"host": "imap.purelymail.com", "port": 993},  # or mxroute
}
```

The existing `poll_for_code()` already accepts `ImapConfig` per call — no changes needed to the verification polling logic.

## Email Creation Automation (WDA-based)

### Outlook Signup via Safari

```
1. wda.launch_app("com.apple.mobilesafari")
2. Navigate to https://signup.live.com
3. Enter first name, last name
4. Select "Get a new email address"
5. Type username, select @outlook.com or @hotmail.com
6. Enter password
7. Solve CAPTCHA (screenshot → CapSolver)
8. IF phone verification shown:
     - Get number from TextVerified
     - Enter number, get code, enter code
   ELSE:
     - Account created directly
9. Go to Settings → enable IMAP (already on by default for Outlook)
10. Store credentials
```

### Mail.com Signup via Safari

```
1. Navigate to https://www.mail.com/int/
2. Tap "Free sign up"
3. Choose domain from dropdown (@mail.com, @email.com, @usa.com, etc.)
4. Enter username
5. Enter password
6. Fill basic profile (name, DOB, country)
7. Solve CAPTCHA
8. Usually no phone required
9. Store credentials
```

## Domain Diversity Analysis

After implementation, the email distribution across 500 accounts looks like:

```
outlook.com     ~150  (30%)
hotmail.com     ~100  (20%)
mail.com        ~30   (6%)
email.com       ~25   (5%)
usa.com         ~20   (4%)
post.com        ~15   (3%)
consultant.com  ~15   (3%)
engineer.com    ~15   (3%)
dr.com          ~10   (2%)
myself.com      ~10   (2%)
writeme.com     ~10   (2%)
cheerful.com    ~10   (2%)
domain1.com     ~30   (6%)
domain2.com     ~25   (5%)
domain3.com     ~20   (4%)
domain4.com     ~15   (3%)
domain5.com     ~10   (2%)
icloud.com      ~8    (1.5%)
```

18 unique domains, max concentration 30% on outlook.com (which is normal — Outlook is one of the world's largest email providers). This distribution mirrors what a natural population looks like.

## Operational Notes

### Account Aging

Email accounts created today and used for social signup tomorrow look suspicious. Build a buffer:
- Create email accounts in batches of 20-30
- Wait 3-7 days before using them for social signup
- During the waiting period, the email account just sits (no need to "warm" emails)
- Older email accounts correlate with higher social account survival rates

### Phone Verification Optimization

When creating Outlook accounts:
- Always try without phone first (CAPTCHA-only path)
- If phone is required, use TextVerified
- Track the phone-required rate per carrier IP range
- If a specific carrier consistently triggers phone verification, switch to a different carrier's IP for email creation

### Batch Scheduling

Don't create 50 email accounts in one day from one phone. Spread across devices and days:
- Max 5-8 email accounts per device per day
- 8 devices × 6 accounts/day = 48/day
- 500 accounts ÷ 48/day = ~10 days to build full inventory
- Start email creation 2 weeks before you need accounts

### Recovery Email

When creating Outlook accounts, they ask for a recovery email. Use one of the catch-all custom domain addresses for this. This way, if Microsoft challenges the account later, you can recover via the catch-all inbox.

## Research Findings (Forum/Community Intelligence)

### GetMX — Unlimited Email Under Existing Domains

GetMX is a service that creates unlimited email accounts under domains you don't own (or under their own domains). Specifically promoted in TikTok account creation communities.

**How it works:**
- Pick from available domains (shared pool)
- Create unlimited email accounts instantly — no phone verification
- IMAP access included
- Pricing: appears to be subscription-based

**Assessment: NOT RECOMMENDED for primary use.**
- The domains GetMX uses are **shared by every customer**. Any domain popular enough on GetMX is likely already flagged by TikTok/Instagram's risk systems.
- If GetMX gets popular in the farming community (it already is), its domains become poison.
- Useful as a quick test or throwaway, but not for accounts you want to last months.
- If you do use it: treat as a Tier 4 supplement, never more than 5% of email inventory.

### Buying Pre-Made Email Accounts (BlackHatWorld Marketplace)

Active market for bulk email accounts. Vendors on BlackHatWorld and sites like accountsvendor.com sell:
- Gmail accounts: $0.03-0.15 each (phone-verified, aged options available)
- Outlook/Hotmail: $0.02-0.10 each
- Yahoo: $0.02-0.08 each
- Auto-delivery, bulk discounts

**Assessment: VIABLE AS A SUPPLEMENT, with caveats.**

Pros:
- Instant inventory — buy 500 Outlook accounts for ~$15-50
- Saves 10 days of on-device creation time
- Domain diversity already built in (gmail.com, outlook.com, hotmail.com, yahoo.com)
- Aged accounts available (3-12 months old) — much better for social signup trust

Cons:
- **Quality varies wildly** — many accounts get locked within days/weeks
- Accounts may have been created from flagged IPs or VPNs
- Shared risk: if the vendor used the same IP range for 10,000 accounts, all their accounts may be correlated
- No control over creation quality (password strength, recovery email, etc.)
- You need to verify IMAP access works before using each account

**Recommendation:** Buy a small batch (50-100) of aged Outlook accounts as a supplement to self-created accounts. Test IMAP access and social signup success rate before scaling. If survival rate > 70% at 30 days, buy more. Budget: $5-15 for a test batch.

### Key Community Insights

From Reddit and BlackHatWorld threads:

1. **Mobile IP is king** — Unanimous consensus that real mobile IPs (not mobile proxies, which are shared) dramatically reduce verification challenges on both email providers and social platforms.

2. **Outlook is the sweet spot** — Gmail requires phone + is aggressive about re-verification. Yahoo requires phone. Outlook is the most permissive major provider for bulk creation from mobile IPs.

3. **Account aging matters more than email provider** — A 30-day-old Outlook account has better social signup survival than a brand-new Gmail. Most detection focuses on account age, not provider prestige.

4. **Rate limiting is IP-based** — Microsoft tracks how many accounts are created from a given IP. Airplane mode rotation solves this. Create max 2-3 per IP (rotate between each creation).

5. **CAPTCHA type indicates risk score** — If Outlook shows a simple image CAPTCHA, the IP is trusted. If it shows FunCaptcha (complex multi-step), the IP is semi-trusted. If it asks for phone, the IP is untrusted. Track this per carrier.

## Final Recommendation

**Primary strategy: Self-create Outlook + Mail.com accounts on your phones.** This leverages your existing infrastructure (phones, mobile IPs, WDA automation, CapSolver) and produces the highest-quality email accounts.

**Supplement with:** A small batch of purchased aged Outlook accounts to bootstrap inventory faster.

**Avoid:** GetMX and similar shared-domain services for any accounts you care about keeping.

The cost structure ($130-190 year 1) is a fraction of Gmail-based approaches and produces accounts that blend naturally into the email provider distribution of real users.
