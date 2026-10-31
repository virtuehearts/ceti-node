"""Find or install llama-server, and check the GPU driver. Stdlib only.

Install picks a current llama.cpp release via the GitHub API at runtime
(no hardcoded asset name, because names change between builds). It
prefers Vulkan (single package, works on NVIDIA, no separate CUDA DLL
zip), then CUDA, then CPU. A CUDA build on Windows also needs the
matching cudart DLL zip in the same folder; Vulkan avoids that.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

from .config import bin_dir


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def gpu_info() -> str:
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if out:
        return f"NVIDIA: {out}"
    if platform.system() == "Windows":
        return "no nvidia-smi found (CPU or non-NVIDIA GPU; Vulkan/CPU fallback)"
    return "no nvidia-smi found (CPU or non-NVIDIA GPU; Vulkan/CPU fallback)"


MIN_EMBED_BUILD = 11630
"""Minimum llama.cpp build for the embedding model. EmbeddingGemma-2
uses the `gemma-embedding2` architecture, which older binaries reject
with "unknown model architecture: 'gemma-embedding2'" after loading
every tensor — the model file is fine, the binary is too old. This
bit us on this box (b11200 + the Ollama-bundled server) and again on
the laptop running 0.0.1, where a stale binary was silently reused
because install only ran when no binary was found at all."""


def server_build(server: str):
    """Return the llama.cpp build number of a server binary, or None
    if it cannot be determined. `llama-server --version` and
    `llama --version` both print e.g. 'version: 0.7.0-dev (build 11941, ...)'."""
    import re
    out = _run([server, "--version"])
    if not out:
        return None
    m = re.search(r"build\s+(\d+)", out)
    return int(m.group(1)) if m else None


def _candidates():
    """Server candidates in priority order. bin/ (Hub-managed) beats
    PATH on purpose: a fresh Hub install must win over a stale
    Ollama-bundled binary that happens to be on PATH."""
    env = os.environ.get("LLAMA_BIN")
    if env and os.path.isfile(env):
        yield env
    for name in ("llama-server", "llama-server.exe", "llama", "llama.exe"):
        cand = os.path.join(bin_dir(), name)
        if os.path.isfile(cand):
            yield cand
    # llama-app install location (~/.llama-app/llama, subcommand "serve")
    # — the working binary on this box, missed by the bin//PATH checks.
    for cand in (os.path.expanduser("~/.llama-app/llama"),
                 os.path.expanduser("~/.llama-app/llama-server"),
                 os.path.expanduser("~/.llama-app/llama.exe")):
        if os.path.isfile(cand):
            yield cand
    found = shutil.which("llama-server") or shutil.which("llama")
    if found:
        yield found


def find_server(min_build=MIN_EMBED_BUILD):
    """Return a llama-server path, or None. Skips binaries too old
    for EmbeddingGemma-2 (build < 11630) instead of letting the
    embedding server die on them later; callers that install when
    this returns None then fetch a current build. Pass
    min_build=None to accept any binary (chat-only use).
    Fail-closed (2026-10-30, 0.0.1e): a binary whose build cannot be
    determined is ALSO skipped. The old code accepted unknown builds
    on trust, so a stale binary with an unparseable --version sailed
    through the gate on the laptop and the embed server died on it
    with "unknown model architecture: 'gemma-embedding2'" — the exact
    failure the gate was built to prevent. An unknown build is never
    better than a fresh download."""
    for cand in _candidates():
        if min_build is not None:
            build = server_build(cand)
            if build is None or build < min_build:
                continue
        return cand
    return None


def server_args(server: str):
    """Return the argv prefix for a server binary: llama-app uses 'serve'."""
    base = os.path.basename(server).lower()
    if base in ("llama", "llama.exe"):
        return [server, "serve"]
    return [server]


def _github_json(url):
    """GET a GitHub API URL as JSON (User-Agent required)."""
    req = urllib.request.Request(url, headers={"User-Agent": "uncensored-hub"})
    with urllib.request.build_opener().open(req, timeout=60) as r:
        return json.load(r)


def _pick_asset(assets, is_win, arch=None):
    """Pick a llama-server zip from a release's assets, or None.
    Preference: Vulkan, then CUDA, then CPU; x64 on x64 machines,
    arm64 on ARM machines. Only packages named llama-* count — the
    cudart-*.zip DLL bundles match a naive 'cuda'+'win' test but
    contain no server binary, so they must never win."""
    if arch is None:
        machine = platform.machine().lower()
        arch = "arm64" if "arm" in machine or "aarch" in machine else "x64"
    if is_win:
        want = [("vulkan", "win"), ("cuda", "win"), ("cpu", "win")]
    else:
        want = [("vulkan", "ubuntu"), ("vulkan", "linux"), ("cpu", "ubuntu"), ("cpu", "linux")]
    for kind, plat in want:
        for a in assets or []:
            name = (a.get("name") or "").lower()
            if not name.startswith("llama-") or not name.endswith(".zip"):
                continue
            if kind not in name or plat not in name or arch not in name:
                continue
            return a
    return None


def install_server():
    """Download a current llama.cpp build into bin/. Returns the server path.
    Release model (2026-10-30, 0.0.1g): the 'latest' release is now a
    versioned stable (v0.8.0) whose only asset is nightly-tag.txt —
    the binaries live in the nightly bXXXX releases. So try the
    latest release's assets first, then scan recent releases newest
    first for the first one with a matching server zip."""
    os.makedirs(bin_dir(), exist_ok=True)
    is_win = platform.system() == "Windows"
    chosen = None
    try:
        latest = _github_json("https://api.github.com/repos/ggml-org/llama.cpp/releases/latest")
        chosen = _pick_asset(latest.get("assets", []), is_win)
    except Exception:
        chosen = None
    if not chosen:
        try:
            for rel in _github_json("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30"):
                chosen = _pick_asset(rel.get("assets", []), is_win)
                if chosen:
                    break
        except Exception:
            chosen = None
    if not chosen:
        raise RuntimeError("could not discover a llama.cpp release asset; download one manually from https://github.com/ggml-org/llama.cpp/releases")
    print(f"downloading {chosen['name']} ...")
    dest = os.path.join(bin_dir(), "llama.zip")
    urllib.request.urlretrieve(chosen["browser_download_url"], dest)
    with zipfile.ZipFile(dest) as z:
        z.extractall(bin_dir())
    os.remove(dest)
    # Return the bin/ binary explicitly, not find_server(): a stale
    # binary on PATH must not shadow the fresh download we just made.
    server = None
    for name in ("llama-server", "llama-server.exe", "llama", "llama.exe"):
        cand = os.path.join(bin_dir(), name)
        if os.path.isfile(cand):
            server = cand
            break
    if not server:
        server = find_server(min_build=None)
    if not server:
        raise RuntimeError("download succeeded but no server binary found in bin/")
    if not is_win:
        try:
            os.chmod(server, 0o755)
        except OSError:
            pass
    return server


def doctor(install=False):
    """Print a health report. With install=True, fetch llama-server if missing."""
    print(f"python: {sys.version.split()[0]} on {platform.system()} {platform.machine()}")
    print(f"gpu: {gpu_info()}")
    try:
        import shutil as _sh
        total, used, free = _sh.disk_usage(os.path.dirname(os.path.abspath(__file__)))
        print(f"disk free: {free // 1073741824} GB")
    except Exception:
        pass
    server = find_server()
    if server:
        build = server_build(server)
        print(f"llama-server: {server} (build {build if build is not None else 'unknown'})")
    elif install:
        # Nothing usable found — either missing, or only a too-old
        # binary exists (gated out by find_server). Same remedy.
        print(f"llama-server: missing or too old for EmbeddingGemma-2 "
              f"(needs build >= {MIN_EMBED_BUILD}) — installing a current build ...")
        server = install_server()
        print(f"llama-server installed: {server} (build {server_build(server)})")
    else:
        print(f"llama-server: NOT FOUND or too old for EmbeddingGemma-2 "
              f"(needs build >= {MIN_EMBED_BUILD}); run: python -m hub doctor --install")
    return server
