#!/bin/bash
# Mya TTS (PocketTTS bridge) — local text-to-speech for the CETI Hub.
#
# Start it:
#   ./start_tts.sh            # :8000, voice mya-03 by default
#   TTS_PORT=8001 ./start_tts.sh
#
# First run creates a venv (tts/.venv) and installs the dependencies
# (torch CPU + pocket-tts + FastAPI, ~1 GB — takes a few minutes),
# then downloads the kyutai/pocket-tts model from Hugging Face
# (~215 MB) into tts/data/hf-cache. Later runs reuse both.
#
# Idempotent: safe to run twice — a fresh venv is created only when
# missing, and pip install runs only once (marker .venv/.tts-installed).
# No Docker needed. Auth is OFF (localhost only, like the Hub itself).
set -e
cd "$(dirname "$0")"

PORT="${TTS_PORT:-8000}"
VENV=".venv"
MARKER="$VENV/.tts-installed"

if [ ! -x "$VENV/bin/python" ]; then
  echo "tts: creating venv ($VENV) ..."
  python3 -m venv "$VENV"
fi

if [ ! -f "$MARKER" ]; then
  echo "tts: installing dependencies (first run, a few minutes) ..."
  # Fail LOUDLY: a quiet pip failure used to exit here with the
  # error buried in the log while the Hub UI kept saying DOWN /
  # "starting…". Never write the marker on a failed install, so a
  # retry re-runs pip instead of launching a half-built venv.
  if ! "$VENV/bin/pip" install --quiet --upgrade pip; then
    echo "tts: ERROR: pip upgrade failed — check network and Python version; not starting"
    exit 1
  fi
  if ! "$VENV/bin/pip" install --quiet -r requirements.txt; then
    echo "tts: ERROR: dependency install failed — check network and Python version; not starting"
    exit 1
  fi
  touch "$MARKER"
  echo "tts: dependencies installed"
fi

# Smoke check before serving: a venv whose install half-failed
# (or was built against a different Python) must not reach uvicorn
# and die silently — stop here with a line the Hub UI now shows.
if ! "$VENV/bin/python" -c "import uvicorn, pocket_tts, torch, fastapi"; then
  echo "tts: ERROR: dependencies incomplete after install (uvicorn/pocket_tts/torch/fastapi import failed) — delete tts/.venv and retry; not starting"
  exit 1
fi

export ENABLE_AUTH=false
export DEFAULT_VOICE="${DEFAULT_VOICE:-mya-03}"
export PORT="$PORT"
export HF_HOME="$PWD/data/hf-cache"
export HUGGINGFACE_HUB_CACHE="$PWD/data/hf-cache"

echo "tts: starting on http://127.0.0.1:$PORT (voice $DEFAULT_VOICE)"
exec "$VENV/bin/python" -m uvicorn app.main:create_app --factory \
  --host 127.0.0.1 --port "$PORT"
