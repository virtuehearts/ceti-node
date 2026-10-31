"""Download GGUFs via Hugging Face Hub, with a stdlib fallback.

Preferred path: huggingface_hub.hf_hub_download (resumable, HF's
own client, works with public repos unauthenticated). If that
package is not installed, fall back to the stdlib urllib downloader
in hub.download, which does the same size + SHA256 checks.

Autorun installs huggingface_hub into .venv if it can; this module
never fails just because the package is missing — it falls back.
"""
import os

from .download import download_hf


def download_via_hfhub(repo: str, filename: str, dest: str, size=None, sha256=None, token=None):
    """Try hf_hub_download, then verify. Returns (path, method).
    token, when given, is passed to hf_hub_download for this call only."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as e:
        raise RuntimeError("huggingface_hub not installed") from e
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    path = hf_hub_download(repo_id=repo, filename=filename,
                           local_dir=os.path.dirname(dest),
                           local_dir_use_symlinks=False,
                           token=token)
    # hf_hub_download may return a cached path with a different name
    # layout; if it is not dest, copy/link it into place.
    if os.path.abspath(path) != os.path.abspath(dest):
        import shutil
        shutil.copyfile(path, dest)
    if size is not None and os.path.getsize(dest) != size:
        raise ValueError(f"size mismatch for {filename}: got {os.path.getsize(dest)}, expected {size}")
    if sha256:
        import hashlib
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != sha256:
            raise ValueError(f"SHA256 mismatch for {filename}")
    with open(dest, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError(f"{filename} is not a GGUF file")
    return dest, "huggingface_hub"


def download_model(repo: str, filename: str, dest: str, size=None, sha256=None, token=None):
    """Download a model, HF Hub first, stdlib urllib as fallback.
    Returns (path, method_used). token is per-call only, never stored."""
    if size is not None and os.path.exists(dest) and os.path.getsize(dest) == size:
        print(f"already present: {dest} ({size} bytes)")
        return dest, "cached"
    try:
        return download_via_hfhub(repo, filename, dest, size=size, sha256=sha256, token=token)
    except Exception as e:
        print(f"HF Hub download unavailable ({e}); falling back to stdlib download ...")
        download_hf(repo, filename, dest, size=size, sha256=sha256, token=token)
        return dest, "urllib-fallback"
