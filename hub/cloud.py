"""Cloud chat via OrcaRouter, OpenRouter or Nvidia, OpenAI-compatible,
stdlib only.

Keys are NEVER written to disk by this module. The caller supplies
the key per request (from the browser's localStorage, or from the
ORCAROUTER_API_KEY / OPENROUTER_API_KEY / NVIDIA_API_KEY environment
variables on the server). Local chat remains the default and sends
nothing anywhere.

OrcaRouter: https://api.orcarouter.ai/v1/chat/completions
  Keys look like sk-orca-...; orcarouter/auto is seeded on signup
  and picks the cheapest live chat model. Models listed via
  /v1/models with the key.
OpenRouter: https://openrouter.ai/api/v1/chat/completions
  Free models are listed dynamically via /api/v1/models (pricing
  prompt == "0" or 0) — no hardcoded free-model list to go stale.
Nvidia: https://integrate.api.nvidia.com/v1/chat/completions
  Models listed via /v1/models with the key.
"""
import json
import os
import urllib.request
import urllib.error

PROVIDERS = {
    "orcarouter": {
        "base": "https://api.orcarouter.ai/v1",
        "env": "ORCAROUTER_API_KEY",
        "suggest": ["orcarouter/auto",
                    "openai/gpt-4o-mini",
                    "anthropic/claude-sonnet-4.6"],
    },
    "openrouter": {
        "base": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
        "suggest": ["recursive/flint-v1.0:free",
                    "liquid/lfm-2.5-2.6b:free",
                    "google/gemma-4-26b-a4b-it:free"],
    },
    "nvidia": {
        "base": "https://integrate.api.nvidia.com/v1",
        "env": "NVIDIA_API_KEY",
        "suggest": ["nvidia/llama-3.1-nemotron-70b-instruct",
                    "meta/llama-3.1-8b-instruct",
                    "mistralai/mixtral-8x7b-instruct-v0.1"],
        # NOTE 2026-10-22: Nvidia chat for these IDs returned 404/410
        # with the key tested (list works, chat does not). The web UI
        # lists models live and surfaces the error per model.
    },
}


def key_for(provider: str, supplied: str = "") -> str:
    if supplied:
        return supplied
    return os.environ.get(PROVIDERS[provider]["env"], "")


def _headers(api_key: str, provider: str):
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    if provider == "openrouter":
        h["HTTP-Referer"] = "http://localhost:8900"
        h["X-Title"] = "Uncensored Hub"
    return h


def list_models(provider: str, api_key: str = ""):
    """Return a list of model ids. For OpenRouter, free models first.
    Returns [] on any error (caller shows suggestions instead)."""
    base = PROVIDERS[provider]["base"]
    url = f"{base}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    try:
        with urllib.request.build_opener().open(req, timeout=30) as r:
            data = json.load(r)
    except Exception:
        return []
    ids = []
    for m in data.get("data", []):
        mid = m.get("id")
        if mid:
            ids.append(mid)
    if provider == "openrouter":
        free = []
        for m in data.get("data", []):
            pricing = m.get("pricing", {}) or {}
            try:
                if float(pricing.get("prompt", "1")) == 0:
                    free.append(m["id"])
            except Exception:
                continue
        # Free first, then the rest, deduplicated.
        seen = set()
        ordered = []
        for mid in free + ids:
            if mid not in seen:
                seen.add(mid)
                ordered.append(mid)
        return ordered
    return ids


def chat_cloud(provider: str, api_key: str, model: str, messages,
               max_tokens=2048, temperature=0.7, stream=False):
    """Non-streaming cloud chat: returns content string.
    For streaming, use stream_cloud below."""
    base = PROVIDERS[provider]["base"]
    payload = json.dumps({"model": model, "messages": messages,
                          "max_tokens": max_tokens, "temperature": temperature,
                          "stream": False}).encode("utf-8")
    req = urllib.request.Request(f"{base}/chat/completions", data=payload,
                                 headers=_headers(api_key, provider))
    with urllib.request.build_opener().open(req, timeout=600) as r:
        data = json.load(r)
    msg = data["choices"][0]["message"]
    return msg.get("content") or ""


def stream_cloud(provider: str, api_key: str, model: str, messages,
                 max_tokens=2048, temperature=0.7):
    """Streaming cloud chat: yields content chunks (SSE, OpenAI-compatible)."""
    base = PROVIDERS[provider]["base"]
    payload = json.dumps({"model": model, "messages": messages,
                          "max_tokens": max_tokens, "temperature": temperature,
                          "stream": True}).encode("utf-8")
    req = urllib.request.Request(f"{base}/chat/completions", data=payload,
                                 headers=_headers(api_key, provider))
    resp = urllib.request.build_opener().open(req, timeout=600)
    buf = b""
    try:
        while True:
            chunk = resp.read(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                try:
                    obj = json.loads(data.decode("utf-8", errors="ignore"))
                except Exception:
                    continue
                try:
                    delta = obj["choices"][0]["delta"]
                except Exception:
                    continue
                text = delta.get("content") or ""
                if text:
                    yield text
    finally:
        try:
            resp.close()
        except Exception:
            pass
