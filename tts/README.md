# Mya TTS — local text-to-speech for the CETI Hub

A self-contained copy of the PocketTTS bridge (FastAPI + kyutai/pocket-tts,
OpenAI-compatible `/v1/audio/speech`), so the laptop release ships local
TTS with no cloud account and no Docker. The Hub's autorun starts it
alongside the Hub; the Hub's voice jobs call it at `http://127.0.0.1:8000`.

## Voices

Shipped, ready to use (no cloning, no token):

| voice | what it is |
|---|---|
| `mya-03` | Mya's working voice (default). |
| `mya-02` | Mya's earlier voice. |

The voice list also shows the 8 voices built into the PocketTTS model
(alba, marius, javert, jean, fantine, cosette, eponine, azelma). The
`data/voices.db` in this folder references **only** mya-02 and mya-03
(no other voices, no admin credentials).

## Start it

Linux / WSL / macOS:

```sh
./start_tts.sh            # :8000
TTS_PORT=8001 ./start_tts.sh
```

Windows (plain Python, no WSL, no Docker):

```bat
start_tts.bat
```

First run creates a venv (`tts/.venv`), installs the dependencies
(torch CPU + pocket-tts + FastAPI, ~1 GB — a few minutes), and downloads
the kyutai/pocket-tts model from Hugging Face (~215 MB) into
`tts/data/hf-cache`. Later runs reuse both. Safe to run twice.

## Endpoint

```sh
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"input": "Paris is the capital of France.", "voice": "mya-03", "response_format": "mp3"}' \
  --output voice.mp3
```

Auth is off (localhost only, like the Hub). The web UI is at
`http://127.0.0.1:8000/` — with auth off it opens straight in.

## If TTS stays DOWN

The Hub's System view shows STARTING while the first-run install
is actually running, and the last lines of the bridge log under the
voice panel when it is not. Common causes:

- **First run still installing** — torch is ~1 GB and the model
  ~215 MB; give it several minutes. The log tail shows the pip
  progress.
- **Install failed** — the log tail says so (network, or a Python
  older than 3.11). Fix the cause, delete `tts/.venv`, and Start
  again; a failed install never writes the installed marker, so a
  retry re-runs pip.
- **START did nothing at all (fixed in 0.0.1-beta)** — the Hub used
  to launch `start_tts.bat` directly, which Windows refuses
  (a .bat is not an executable); it now goes through `cmd /c`, the
  same as autorun. Double-clicking the .bat always worked.

Dependencies are pinned in `requirements.txt` to the versions
verified with this bridge — a fresh install no longer floats to
whatever torch / pocket-tts is newest that day.

## What downloads on first run (not shipped)

- The kyutai/pocket-tts model from Hugging Face (~215 MB) into
  `tts/data/hf-cache`. Same rule as the Hub's models: they download on
  the laptop via autorun, never in the zip.
- No HF token is needed for the shipped voices. A token is only needed
  if you want to clone a **new** voice (set `HF_TOKEN` and accept the
  terms at https://huggingface.co/kyutai/pocket-tts first).

## Docker (alternative, not required)

A `Dockerfile` is included for Docker users:

```sh
docker build -t mya-tts .
docker run -p 8000:8000 mya-tts
```

The venv route above is the primary path — Docker Desktop is not
assumed on the laptop.

## Layout

```
tts/
  app/            FastAPI bridge (api, services, templates, static)
  src/            pockettts_bridge CLI helper
  data/
    voices/       mya-02.wav, mya-03.wav (voice samples)
    embeddings/   mya-02.safetensors, mya-03.safetensors (voice embeddings)
    voices.db     voice registry — mya-02 + mya-03 only
    hf-cache/     model cache (empty in the zip; filled on first run)
    output/       generated audio (empty in the zip)
  requirements.txt  torch CPU + pocket-tts + FastAPI
  start_tts.sh    Linux/WSL/macOS start (venv, idempotent)
  start_tts.bat   Windows start (venv, idempotent)
  Dockerfile      Docker alternative
```

Source: https://github.com/virtuehearts/pockettts-agent-bridge
(upstream repo; this folder is a self-contained snapshot of it).
