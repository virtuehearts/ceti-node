@echo off
REM Mya TTS (PocketTTS bridge) — local text-to-speech for the CETI Hub.
REM Plain Windows Python (no WSL, no Docker).
REM
REM Start it:
REM   start_tts.bat
REM   set TTS_PORT=8001 & start_tts.bat
REM
REM First run creates a venv (tts\.venv) and installs the dependencies
REM (torch CPU + pocket-tts + FastAPI, ~1 GB — takes a few minutes),
REM then downloads the kyutai/pocket-tts model from Hugging Face
REM (~215 MB) into tts\data\hf-cache. Later runs reuse both.
REM
REM Idempotent: safe to run twice — a fresh venv is created only when
REM missing, and pip install runs only once (marker .venv\.tts-installed).
REM Auth is OFF (localhost only, like the Hub itself).
cd /d "%~dp0"

if not defined TTS_PORT set TTS_PORT=8000
if not defined DEFAULT_VOICE set DEFAULT_VOICE=mya-03

if not exist ".venv\Scripts\python.exe" (
  echo tts: creating venv (.venv) ...
  python -m venv .venv
  if errorlevel 1 (
    echo tts: ERROR: venv creation failed — is Python 3.11+ on PATH? Not starting.
    exit /b 1
  )
)

if not exist ".venv\.tts-installed" (
  echo tts: installing dependencies (first run, a few minutes) ...
  REM Fail LOUDLY: the old script ran on past a failed pip install
  REM and died in uvicorn, while the Hub UI kept saying DOWN /
  REM "starting…". Never write the marker on a failed install, so a
  REM retry re-runs pip instead of launching a half-built venv.
  .venv\Scripts\pip install --quiet --upgrade pip
  if errorlevel 1 (
    echo tts: ERROR: pip upgrade failed — check network and Python version. Not starting.
    exit /b 1
  )
  .venv\Scripts\pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo tts: ERROR: dependency install failed — check network and Python version. Not starting.
    exit /b 1
  )
  type nul > ".venv\.tts-installed"
  echo tts: dependencies installed
)

REM Smoke check before serving: a venv whose install half-failed
REM must not reach uvicorn and die silently — stop here with a line
REM the Hub UI now shows.
.venv\Scripts\python -c "import uvicorn, pocket_tts, torch, fastapi"
if errorlevel 1 (
  echo tts: ERROR: dependencies incomplete after install (uvicorn/pocket_tts/torch/fastapi import failed) — delete tts\.venv and retry. Not starting.
  exit /b 1
)

set ENABLE_AUTH=false
set PORT=%TTS_PORT%
set HF_HOME=%CD%\data\hf-cache
set HUGGINGFACE_HUB_CACHE=%CD%\data\hf-cache

echo tts: starting on http://127.0.0.1:%TTS_PORT% (voice %DEFAULT_VOICE%)
.venv\Scripts\python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port %TTS_PORT%
