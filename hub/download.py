"""Download a file from Hugging Face with resume, size and SHA256 checks. Stdlib only."""
import hashlib
import os
import urllib.request


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_hf(repo: str, filename: str, dest: str, size=None, sha256=None, token=None):
    """Download https://huggingface.co/<repo>/resolve/main/<filename> to dest.
    Resumes a partial file with Range. Raises on size/SHA mismatch.
    token, when given, is sent as a Bearer header for this request only."""
    url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    existing = os.path.getsize(dest) if os.path.exists(dest) else 0
    if size is not None and existing == size:
        print(f"already present: {dest} ({size} bytes)")
    else:
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if existing > 0:
            req.add_header("Range", f"bytes={existing}-")
            print(f"resuming {filename} from {existing} bytes ...")
        else:
            print(f"downloading {filename} from {repo} ...")
        with urllib.request.build_opener().open(req, timeout=120) as r, open(dest, "ab") as f:
            downloaded = existing
            last_mark = existing
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if downloaded - last_mark >= 100 * 1024 * 1024:
                    print(f"  ... {downloaded / 1048576:.0f} MB")
                    last_mark = downloaded
    final_size = os.path.getsize(dest)
    if size is not None and final_size != size:
        raise ValueError(f"size mismatch for {filename}: got {final_size}, expected {size}")
    print(f"size OK: {final_size} bytes")
    with open(dest, "rb") as f:
        magic = f.read(4)
    if magic != b"GGUF":
        raise ValueError(f"{filename} is not a GGUF file (magic {magic!r})")
    print("GGUF magic OK")
    if sha256:
        got = _sha256(dest)
        if got != sha256:
            raise ValueError(f"SHA256 mismatch for {filename}: got {got}, expected {sha256}")
        print(f"SHA256 OK: {got}")
    return dest
