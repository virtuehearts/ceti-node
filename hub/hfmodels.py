"""Hugging Face model browsing + downloading for the Hub's Models view.

Server-side only: the browser never talks to huggingface.co directly.
An HF token, when supplied, is passed per-request in memory only — it is
never written to disk, never logged, and never returned in any response.

GPU-memory figures are ESTIMATES, not measurements. Formula (shown in
the UI next to every estimate):

    est. GPU memory = file size x 1.2
    (weights + ~20% overhead allowance for KV cache / context)

A file's quantization is parsed from its filename (e.g. Q4_K_M); when
no known quant token is found the UI shows a dash, never a guess.
"""
import json
import os
import re
import threading
import urllib.error
import urllib.request

from .config import data_dir, models_dir

HF_API = "https://huggingface.co/api"
OVERHEAD_FACTOR = 1.2

_QUANT_RE = re.compile(
    r"(Q\d+_K_[MSL]|Q\d+_K|Q\d+_0|Q\d+_1|IQ\d+_[A-Z]+|F16|F32|BF16)"
)


def estimate_gpu_gb(size_bytes):
    """Estimated GPU memory in GiB for a GGUF file of size_bytes.

    Weights plus a 20% overhead allowance for KV cache / context.
    This is an estimate, never a measured figure.
    """
    if size_bytes is None:
        return None
    return round(size_bytes * OVERHEAD_FACTOR / (1024 ** 3), 2)


def parse_quant(filename):
    """Quantization token parsed from a GGUF filename, or None."""
    m = _QUANT_RE.search(filename or "")
    return m.group(1) if m else None


def fmt_bytes(n):
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def hf_get_json(url, token=None):
    """GET a Hugging Face API URL, return parsed JSON. Token in-memory only."""
    req = urllib.request.Request(url, headers={"User-Agent": "uncensored-hub/0.0.1"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.build_opener().open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ValueError(f"HF API {e.code} for {url}") from e


def list_collection(slug, token=None):
    """Models in a public HF collection slug like 'chwoo/uncensored-models'.

    Returns [{id}] for items of type model. Raises ValueError on API error.
    """
    data = hf_get_json(f"{HF_API}/collections/{slug}", token=token)
    items = data.get("items", []) if isinstance(data, dict) else []
    return [{"id": it.get("id")} for it in items if it.get("type") == "model" and it.get("id")]


def model_detail(repo, token=None):
    """One repo's GGUF files + free HF metadata. No numbers are invented:
    missing fields come back as None and the UI shows a dash.

    File sizes come from the repo's siblings list when present; when the
    API omits them there (common for newer Xet-backed repos), they are
    filled from the repo's file tree (/tree/main), which carries the
    same sizes + LFS hashes. A tree failure leaves dashes, never guesses.
    """
    data = hf_get_json(f"{HF_API}/models/{repo}", token=token)
    tree_sizes = {}
    try:
        tree = hf_get_json(f"{HF_API}/models/{repo}/tree/main", token=token)
        for entry in tree if isinstance(tree, list) else []:
            if entry.get("type") == "file" and entry.get("path"):
                tree_sizes[entry["path"]] = (
                    entry.get("size"),
                    (entry.get("lfs") or {}).get("oid"),
                )
    except Exception:
        tree_sizes = {}
    files = []
    for sib in data.get("siblings", []) or []:
        name = sib.get("rfilename", "")
        if not name.lower().endswith(".gguf"):
            continue
        size = sib.get("size")
        sha256 = (sib.get("lfs") or {}).get("oid")
        if (size is None or sha256 is None) and name in tree_sizes:
            tsize, tsha = tree_sizes[name]
            size = size if size is not None else tsize
            sha256 = sha256 if sha256 is not None else tsha
        files.append({
            "name": name,
            "size": size,
            "sha256": sha256,
            "quant": parse_quant(name),
            "gpu_gb_est": estimate_gpu_gb(size),
        })
    return {
        "repo": data.get("id", repo),
        "downloads": data.get("downloads"),
        "last_modified": data.get("lastModified"),
        "files": files,
    }


def local_models():
    """GGUF files already in models/ (name + size). No network, no token."""
    out = []
    try:
        names = sorted(os.listdir(models_dir()))
    except OSError:
        return out
    for name in names:
        if not name.lower().endswith(".gguf"):
            continue
        try:
            size = os.path.getsize(os.path.join(models_dir(), name))
        except OSError:
            continue
        out.append({"name": name, "size": size, "quant": parse_quant(name)})
    return out


# ---- downloads (background jobs; job records hold no token) -----------
_jobs = {}
_jobs_lock = threading.Lock()
_job_seq = [0]


def _job_download(job_id, repo, filename, dest, size, sha256, token):
    from .hfdownload import download_model
    try:
        path, method = download_model(repo, filename, dest,
                                      size=size, sha256=sha256, token=token)
        ledger = None
        try:
            from . import regula
            _man, ev = regula.store_chunked(data_dir(), "model", path,
                                            os.path.basename(filename), repo)
            ledger = {"ok": True, "event_hash": ev["event_hash"]}
        except Exception as e:
            ledger = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with _jobs_lock:
            _jobs[job_id].update({"state": "done", "path": path, "method": method,
                                  "downloaded": os.path.getsize(path),
                                  "ledger": ledger})
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id].update({"state": "error", "error": str(e)})


def start_download(repo, filename, size=None, sha256=None, token=None):
    """Start a background download into models/. Returns the job id.

    The token is passed to the worker thread's memory only; the stored
    job record contains repo/filename/path/state — never the token.
    """
    dest = os.path.join(models_dir(), os.path.basename(filename))
    with _jobs_lock:
        _job_seq[0] += 1
        job_id = f"hfdl-{_job_seq[0]}"
        _jobs[job_id] = {"id": job_id, "repo": repo, "filename": filename,
                         "size": size, "state": "running", "downloaded": 0}
    t = threading.Thread(target=_job_download,
                         args=(job_id, repo, filename, dest, size, sha256, token),
                         daemon=True)
    t.start()
    return job_id


def job_status(job_id):
    """Job status with live byte progress (dest file size while running)."""
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        return None
    if job["state"] == "running":
        dest = os.path.join(models_dir(), os.path.basename(job["filename"]))
        try:
            job["downloaded"] = os.path.getsize(dest)
        except OSError:
            pass
    return job
