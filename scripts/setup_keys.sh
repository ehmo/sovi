#!/bin/bash
# SOVI API Key Setup Script
# Run this to configure all required API keys in the .env file.
#
# Usage: bash scripts/setup_keys.sh

ENV_FILE="${SOVI_DIR:-/Users/noh/Work/ai/sovi}/.env"

echo "=== SOVI API Key Setup ==="
echo "This will configure your .env file at: $ENV_FILE"
echo ""

# Read existing .env
if [ -f "$ENV_FILE" ]; then
    echo "Existing .env found. Will update/add keys."
else
    echo "Creating new .env file."
    echo "# SOVI Configuration" > "$ENV_FILE"
    echo "DATABASE_URL=postgresql://sovi:sovi@localhost:5432/sovi" >> "$ENV_FILE"
    echo "REDIS_URL=redis://localhost:6379/0" >> "$ENV_FILE"
fi

add_key() {
    local key_name="$1"
    local description="$2"
    local required="$3"

    # Check if already set
    existing=$(grep "^${key_name}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    if [ -n "$existing" ] && [ "$existing" != '""' ] && [ "$existing" != "''" ]; then
        echo "  $key_name: already set (${existing:0:8}...)"
        return
    fi

    if [ "$required" = "required" ]; then
        echo -n "  $key_name ($description) [REQUIRED]: "
    else
        echo -n "  $key_name ($description) [optional, press Enter to skip]: "
    fi
    read -r value

    if [ -n "$value" ]; then
        # Remove existing line if present
        grep -v "^${key_name}=" "$ENV_FILE" > "${ENV_FILE}.tmp" 2>/dev/null || true
        mv "${ENV_FILE}.tmp" "$ENV_FILE"
        echo "${key_name}=${value}" >> "$ENV_FILE"
        echo "    -> Set!"
    else
        echo "    -> Skipped"
    fi
}

echo ""
echo "--- Required for script generation ---"
add_key "ANTHROPIC_API_KEY" "Claude API for scripts" "required"

echo ""
echo "--- Required for voice generation ---"
add_key "OPENAI_API_KEY" "OpenAI TTS for bulk voiceover" "required"
add_key "ELEVENLABS_API_KEY" "ElevenLabs for premium voiceover" "optional"

echo ""
echo "--- Required for image/video generation ---"
add_key "FAL_KEY" "fal.ai for FLUX images + Kling/Hailuo video" "required"

echo ""
echo "--- Required for transcription/captions ---"
add_key "DEEPGRAM_API_KEY" "Deepgram Nova-3 for word-level timestamps" "required"

echo ""
echo "--- Distribution (needed later) ---"
add_key "LATE_API_KEY" "Late.dev for multi-platform posting" "optional"

echo ""
echo "=== Setup Complete ==="
echo "Keys saved to: $ENV_FILE"
echo ""
echo "To test the pipeline:"
echo "  cd /Users/noh/Work/ai/sovi"
echo "  .venv/bin/python -m sovi produce \\"
echo "    --topic '3 AI tools that save you money' --niche personal_finance"
