# CLI Reference

## Entry Point

```bash
python -m sovi <command> [options]
# or (if installed as package):
sovi <command> [options]
```

## Commands

### health

System health check — WDA status, apps, database, services, API keys, FFmpeg, disk.

```bash
sovi health
```

Checks:
- WDA connectivity on all registered devices
- Installed app states (TikTok, Instagram, YouTube, Reddit, X)
- PostgreSQL connection + table counts
- Dashboard availability (http://localhost:8888)
- Scheduler status
- Account fleet summary by platform/state
- launchd service status
- API key configuration (.env)
- FFmpeg availability + features
- Disk usage

### server

Start the FastAPI dashboard.

```bash
sovi server [--port 8888] [--host 0.0.0.0]
```

### scheduler

Manage the continuous device scheduler.

```bash
sovi scheduler start     # Start threads, block until Ctrl+C
sovi scheduler status    # Show thread states + session counts
sovi scheduler stop      # Stop all threads
```

### accounts

Manage social media accounts.

```bash
sovi accounts list [--platform tiktok] [--niche motivation] [--status warming_p1]
sovi accounts create --platform tiktok --email user@example.com [--niche motivation]
```

Output columns: Platform, Username, State, Day, Followers, Niche, Last Warmed

### devices

Manage the device fleet.

```bash
sovi devices list
sovi devices add --name iPhone-C --udid 00008140-... [--model iPhone] [--wda-port 8102]
sovi devices setup --name iPhone-C    # Generate launchd plists
```

### warm (Legacy)

Run a single warming session on all devices.

```bash
sovi warm [--duration 30] [--phase passive|light]
```

### produce

Produce a video from a topic.

```bash
sovi produce --topic "3 AI tools that save money" \
    [--niche personal_finance] \
    [--platform tiktok] \
    [--format faceless] \
    [--duration 45] \
    [--elevenlabs]     # Use ElevenLabs instead of OpenAI TTS
```

### dry-run

Validate the production pipeline without generating assets.

```bash
sovi dry-run --topic "..." [--niche personal_finance] [--platform tiktok] [--duration 30]
```

### research

Run the trend research scanner.

```bash
sovi research [--reddit-only] [--tiktok-only] [--stories]
```

### db

Show database summary — table counts, recent content, warming sessions, trending topics.

```bash
sovi db
```

## Configuration

All settings via environment variables or `.env` file:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| DATABASE_URL | No | postgresql://sovi:sovi@localhost:5432/sovi | PostgreSQL connection |
| ANTHROPIC_API_KEY | For scripts | — | Claude API |
| FAL_KEY | For assets | — | fal.ai image/video gen |
| OPENAI_API_KEY | For voice | — | OpenAI TTS |
| ELEVENLABS_API_KEY | Optional | — | Premium voiceover |
| DEEPGRAM_API_KEY | For captions | — | Word-level transcription |
| CAPSOLVER_API_KEY | For signup | — | CAPTCHA solving |
| TEXTVERIFIED_API_KEY | For signup | — | Disposable SMS |
| SOVI_MASTER_KEY | For auth | — | AES-256 encryption key |
| LATE_API_KEY | For posting | — | Multi-platform distribution |
| REDDIT_CLIENT_ID | For research | — | Reddit API |
| REDDIT_CLIENT_SECRET | For research | — | Reddit API |

Generate encryption key:
```bash
python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```
