#!/usr/bin/env python3
"""Uncensored Hub autorun — one command, everything set up.

In WSL (recommended on Windows):
    cd uncensored-hub
    python3 autorun.py
Then open http://localhost:8900 in your Windows browser (WSL forwards
localhost automatically).

What it does, in order:
  1. Detects WSL / Linux / Windows and prints it.
  2. Makes sure huggingface_hub is available: uses it if installed,
     otherwise tries to install it into .venv (created if needed),
     and falls back to the stdlib downloader if that fails.
  3. Installs llama-server if missing (Vulkan first, then CUDA, then CPU).
  4. Downloads the chat model + EmbeddingGemma-2 via Hugging Face Hub
     (resumable, size + SHA256 verified).
  5. Starts chat (8080) + embedding (11435) servers — chat on VRAM if
     a GPU is present (RAM/CPU otherwise, with VRAM->RAM fallback),
     embedding on CPU/RAM by default (tiny model, saves VRAM for chat).
  6. Starts the ChatGPT-style web UI on http://127.0.0.1:8900.
  7. Waits for Ctrl-C, then stops the servers.

Flags:
  --ctx 16384|32768   chat context (default 32768)
  --ram               force RAM/CPU mode
  --web-port PORT     web UI port (default 8900)
  --setup-only        do setup + start servers + web, health-check, then stop
                      (used for automated testing)
  --no-web            setup + servers only, no web UI

Cloud keys are NOT asked for here (they would land in shell history).
Add an OpenRouter / Nvidia key in the web UI Settings page (stored in
your browser only), or set OPENROUTER_API_KEY / NVIDIA_API_KEY in WSL.
"""
import argparse
import os
import platform
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def is_wsl() -> bool:
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def ensure_hfhub():
    """Return True if huggingface_hub is importable after this call.

    Windows fix (v0.5): do NOT create a .venv on Windows — the venv
    python shim on WinGet/Store Python fails with a bogus
    '/usr/bin\\python.exe' error, and a broken .venv then repeats
    every run. On Windows, try pip --user with the running Python
    instead, and if that fails, fall back to the stdlib downloader
    (which already works, as the cached-model path proves). A broken
    .venv folder from an earlier run is deleted so it cannot recur.
    """
    try:
        import huggingface_hub  # noqa: F401
        print("huggingface_hub: already installed")
        return True
    except ImportError:
        pass
    if os.name == "nt":
        venv = os.path.join(ROOT, ".venv")
        if os.path.exists(venv):
            import shutil
            print("removing broken .venv from an earlier run ...")
            shutil.rmtree(venv, ignore_errors=True)
        print("huggingface_hub: not installed — trying pip install (user) ...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--user", "huggingface_hub"], check=True)
            import huggingface_hub  # noqa: F401
            print("huggingface_hub: installed (user)")
            return True
        except Exception as e:
            print(f"huggingface_hub: install failed ({e}) — continuing with the stdlib downloader")
            return False
    print("huggingface_hub: not installed — trying to install it into .venv ...")
    venv = os.path.join(ROOT, ".venv")
    venv_python = os.path.join(venv, "bin", "python")
    try:
        if not os.path.exists(venv_python):
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        subprocess.run([venv_python, "-m", "pip", "install", "--quiet", "huggingface_hub"], check=True)
        print("huggingface_hub: installed into .venv — re-execing autorun inside the venv ...")
        os.execv(venv_python, [venv_python, os.path.abspath(__file__)] + sys.argv[1:])
    except Exception as e:
        print(f"huggingface_hub: install failed ({e}) — continuing with the stdlib downloader")
        return False
    return True


def main():
    p = argparse.ArgumentParser(description="Uncensored Hub autorun")
    p.add_argument("--ctx", type=int, default=32768, choices=[16384, 32768])
    p.add_argument("--ram", action="store_true")
    p.add_argument("--embed-gpu", action="store_true", help="put embedding model on GPU too (default: CPU/RAM)")
    p.add_argument("--web-port", type=int, default=8900)
    p.add_argument("--setup-only", action="store_true")
    p.add_argument("--no-web", action="store_true")
    args = p.parse_args()

    print("=" * 60)
    print("Uncensored Hub — autorun")
    print(f"platform: {platform.system()} {platform.machine()} | WSL: {is_wsl()} | python: {sys.version.split()[0]}")
    print("=" * 60)

    from hub.config import data_dir, models_dir, load_catalog, chat_port, embed_port
    from hub.identity import ensure_identity
    from hub.llama import find_server, install_server, doctor, server_build, MIN_EMBED_BUILD
    from hub.hfdownload import download_model
    from hub.servers import ensure_servers, stop_all, _health

    ident, existed = ensure_identity(data_dir())
    print(f"identity: {ident.fingerprint} ({'loaded' if existed else 'generated, mode 0600'})")

    ensure_hfhub()
    doctor(install=False)
    if not find_server():
        print("llama-server missing — installing ...")
        try:
            install_server()
            print(f"llama-server: {find_server()}")
        except Exception as e:
            # Install failure must not kill the whole run (0.0.1f
            # died here with a traceback and nothing started).
            # Chat + web UI still start; embedding stays DOWN until
            # a server binary is installed, and the cause is printed.
            print(f"llama-server install failed: {e}")
            print("Continuing without it: chat + web UI will start, embedding stays DOWN.")
            print("Download a build manually from https://github.com/ggml-org/llama.cpp/releases")
            print("into bin/ (llama-server or llama-server.exe), then restart.")

    cat = load_catalog()["models"]
    for m in cat:
        dest = os.path.join(models_dir(), m["file"])
        _path, method = download_model(m["hf_repo"], m["file"], dest,
                                       size=m.get("size"), sha256=m.get("sha256"))
        print(f"{m['id']}: ready via {method}")

    gpu = not args.ram
    chat_m = next(m for m in cat if m["role"] == "chat")
    emb_m = next(m for m in cat if m["role"] == "embed")
    print(f"starting servers (ctx {args.ctx}, {'RAM' if args.ram else 'VRAM'} first, RAM fallback if VRAM fails) ...")
    chat_ok, emb_ok = ensure_servers(
        os.path.join(models_dir(), chat_m["file"]),
        os.path.join(models_dir(), emb_m["file"]),
        gpu=gpu, ctx=args.ctx, embed_gpu=args.embed_gpu)
    print(f"chat server: http://127.0.0.1:{chat_port()} ({'up' if chat_ok else 'DOWN'})")
    print(f"embedding server: http://127.0.0.1:{embed_port()} ({'up' if emb_ok else 'DOWN'})")
    if not emb_ok:
        srv = find_server()
        print(f"embedding server DOWN (server: {srv} build "
              f"{server_build(srv) if srv else 'none'} — needs build >= {MIN_EMBED_BUILD}; "
              f"run: python -m hub doctor --install)")
    if not chat_ok:
        print("WARNING: chat server did not come up — check data/server-*.log")
        print("Continuing to the web UI anyway: Node tab, Files, and Settings still work.")

    web_thread = None
    if not args.no_web:
        os.environ["HUB_WEB_PORT"] = str(args.web_port)
        from hub.web import run_web
        import threading
        web_thread = threading.Thread(target=run_web, kwargs={"port": args.web_port}, daemon=True)
        web_thread.start()
        time.sleep(1)
        print(f"web UI: http://127.0.0.1:{args.web_port}")
        print("In WSL, open that URL in your Windows browser.")
        print("Add an OpenRouter / Nvidia key in Settings (browser only, never written to disk),")
        print("or set OPENROUTER_API_KEY / NVIDIA_API_KEY in WSL for cloud models.")

    if args.setup_only:
        print("setup-only: health check, then stopping ...")
        time.sleep(2)
        print(f"health: chat={_health(chat_port())} embed={_health(embed_port())}")
        stop_all()
        print("servers stopped (setup-only mode)")
        return 0

    print("\nRunning. Press Ctrl-C to stop the servers and exit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping servers ...")
        stop_all()
        print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
