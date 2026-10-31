"""regula_core file ledger: the local file system based on the ledger.

Rules, from the regula_core record:
- One key per node: the node's identity.dat secret signs everything.
- Nothing counts until it is signed and the signature verifies.
- Append-only ledger: each event carries prev_hash chaining it to the
  previous event; the event hash is the double-SHA256 of the canonical
  event including its signature. The genesis event has an all-zero
  prev_hash, as Chamber Zero did.
- Shared files travel as signed, chained, Merkle-rooted bundles: a
  manifest with a sha256 per file, a Merkle root over the sorted
  hashes, the prev_hash version-ledger pointer, and an ECDSA signature
  over the canonical manifest. Peers verify signature, hashes, chain,
  and Merkle root before anything merges.
- Rejected items never enter signed history: a failed publish records
  no ledger event.

Chunked block store (50MB chunks, for node-to-node transfer):
- Files meant to travel are split into 50MB chunks, one chunk in
  memory at a time, and stored content-addressed under data/blocks/
  (<chunk-sha>.chunk). The same chunk bytes are never stored twice.
- A file's manifest (data/blocks/<file-sha>.manifest.json) lists its
  chunks in order, the whole-file sha256, and the Merkle root over
  the chunk hashes. The ledger records one block event per chunk
  plus one manifest event closing the set — that chain of block
  events is the block ledger.
- Files that only need a record (sessions, stories, code, samples)
  use record_file instead: one manifest event with the file's hash,
  no byte copying. The ledger is the record; the file stays where
  it lives.

The signature scheme is exactly the network's own: ECDSA on secp256k1
over double-SHA256, DER-encoded, RFC 6979 (node.py) — so ledger
signatures verify against the same public key the node announces with.
The 2006 Chamber Zero header (RSA-1024, LCG nonce, port 666 BBS) is
history, not the scheme used here.
"""
import hashlib
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import node as node_mod  # noqa: E402

ZERO_HASH = "0" * 64
CHUNK_SIZE = 50 * 1024 * 1024  # 50MB chunks: the node-to-node transfer unit


def sha256d_hex(data: bytes) -> str:
    return hashlib.sha256(hashlib.sha256(data).digest()).hexdigest()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def ledger_path(data_dir: str) -> str:
    return os.path.join(data_dir, "ledger.json")


def _load(data_dir: str):
    try:
        with open(ledger_path(data_dir), encoding="utf-8") as f:
            events = json.load(f)
        return events if isinstance(events, list) else []
    except (OSError, ValueError):
        return []


def _save(data_dir: str, events):
    os.makedirs(data_dir, exist_ok=True)
    tmp = ledger_path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, sort_keys=True)
    os.replace(tmp, ledger_path(data_dir))


def _identity(data_dir: str):
    ident, _existed = node_mod.load_or_create_identity(data_dir)
    return ident


def node_id(data_dir: str) -> str:
    """This node's network id (node-<hash12>) for signed manifests."""
    return _identity(data_dir).node_id


def identity_info(data_dir: str):
    """This node's public identity for status views: network id,
    NODE fingerprint, and compressed public key. No secret, ever."""
    ident = _identity(data_dir)
    return {"node_id": ident.node_id, "fingerprint": ident.fingerprint,
            "pubkey": ident.public_hex()}


def signing_probe(data_dir: str) -> bool:
    """Real signing proof for status views: sign a probe string with
    the node key and verify it against the node pubkey. True only
    when this node's key actually signs and verifies."""
    try:
        ident = _identity(data_dir)
        sig = node_mod.sign_payload(ident.secret, canonical("regula-status-probe"))
        return node_mod.verify_signature(ident.public_hex(),
                                         canonical("regula-status-probe"), sig)
    except Exception:
        return False


def _sign_event(ident, event):
    sig = node_mod.sign_payload(ident.secret, canonical(event))
    return sig


def ensure_genesis(data_dir: str):
    """Create the genesis event if the ledger is empty. Returns the events."""
    events = _load(data_dir)
    if events:
        return events
    ident = _identity(data_dir)
    event = {"index": 0, "prev_hash": ZERO_HASH, "kind": "genesis",
             "path": "", "sha256": "", "size": 0,
             "created_at": time.time(), "node_id": ident.node_id}
    event["signature"] = _sign_event(ident, event)
    event["event_hash"] = sha256d_hex(canonical(event))
    _save(data_dir, [event])
    return [event]


def append(data_dir: str, kind: str, path: str, sha256: str, size: int, detail: str = ""):
    """Append one signed event to the ledger. Returns the event."""
    events = ensure_genesis(data_dir)
    ident = _identity(data_dir)
    event = {"index": len(events), "prev_hash": events[-1]["event_hash"],
             "kind": kind, "path": path, "sha256": sha256, "size": size,
             "detail": detail,
             "created_at": time.time(), "node_id": ident.node_id}
    event["signature"] = _sign_event(ident, event)
    event["event_hash"] = sha256d_hex(canonical(event))
    events.append(event)
    _save(data_dir, events)
    return event


def tip_hash(data_dir: str) -> str:
    events = _load(data_dir)
    return events[-1]["event_hash"] if events else ZERO_HASH


def verify(data_dir: str):
    """Re-check the whole ledger: hashes, prev-hash pointers, signatures.

    Returns (ok, height, error). An empty ledger verifies as height 0:
    no history yet, nothing broken.
    """
    events = _load(data_dir)
    if not events:
        return True, 0, ""
    ident = _identity(data_dir)
    pubkey_hex = ident.public_hex()
    prev = ZERO_HASH
    for event in events:
        if event.get("prev_hash") != prev:
            return False, len(events), f"event {event.get('index')} prev_hash mismatch"
        sig = event.get("signature", "")
        body = {k: v for k, v in event.items() if k not in ("signature", "event_hash")}
        if not node_mod.verify_signature(pubkey_hex, canonical(body), sig):
            return False, len(events), f"event {event.get('index')} bad signature"
        if sha256d_hex(canonical({**body, "signature": sig})) != event.get("event_hash"):
            return False, len(events), f"event {event.get('index')} event_hash mismatch"
        prev = event["event_hash"]
    return True, len(events), ""


def merkle_root(sha256_hex_list):
    """Merkle root over the sorted file hashes (double-SHA256 pairs)."""
    level = sorted(sha256_hex_list)
    if not level:
        return ZERO_HASH
    if len(level) == 1:
        return level[0]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            nxt.append(sha256d_hex(bytes.fromhex(left) + bytes.fromhex(right)))
        level = nxt
    return level[0]


def bundle_manifest(data_dir: str, files):
    """Build a signed regula bundle manifest for the given files.

    files: [{name, sha256, size}]. The manifest carries the sha256 per
    file, the Merkle root over the sorted hashes, the ledger tip as
    the prev_hash version pointer, and the node's signature over the
    canonical manifest.
    """
    ident = _identity(data_dir)
    ordered = sorted(files, key=lambda f: f["name"])
    manifest = {"version": 1, "files": ordered,
                "merkle_root": merkle_root([f["sha256"] for f in ordered]),
                "prev_hash": tip_hash(data_dir),
                "node_id": ident.node_id, "created_at": time.time()}
    return sign_manifest(data_dir, manifest)


def sign_manifest(data_dir: str, manifest):
    """Sign a manifest with the node key: signature + pubkey attached."""
    ident = _identity(data_dir)
    manifest["signature"] = node_mod.sign_payload(ident.secret, canonical(manifest))
    manifest["pubkey"] = ident.public_hex()
    return manifest


def verify_manifest(manifest, pubkey_hex: str) -> bool:
    """Verify a bundle manifest: Merkle root + signature over it.

    The Merkle root covers the chunk hashes when the manifest lists
    chunks (chunked store), else the per-file hashes (single-file
    bundles from bundle_manifest).
    """
    try:
        files = manifest["files"]
        hashes = []
        for f in files:
            if f.get("chunks"):
                hashes.extend(c["sha256"] for c in f["chunks"])
            else:
                hashes.append(f["sha256"])
        if manifest["merkle_root"] != merkle_root(hashes):
            return False
        body = {k: v for k, v in manifest.items() if k not in ("signature", "pubkey")}
        return node_mod.verify_signature(pubkey_hex, canonical(body), manifest["signature"])
    except Exception:
        return False


# ---- chunked block store --------------------------------------------

def blocks_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "blocks")
    os.makedirs(d, exist_ok=True)
    return d


def chunk_path(data_dir: str, sha: str) -> str:
    return os.path.join(blocks_dir(data_dir), sha + ".chunk")


def manifest_path(data_dir: str, file_sha: str) -> str:
    return os.path.join(blocks_dir(data_dir), file_sha + ".manifest.json")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _ledger_path_for(data_dir: str, path: str) -> str:
    """Ledger path for a file: relative to data_dir when inside it,
    else the bare filename. Hashes, not absolute paths, are the record."""
    try:
        rel = os.path.relpath(path, data_dir)
        if not rel.startswith(".."):
            return rel
    except ValueError:
        pass
    return os.path.basename(path)


def record_file(data_dir: str, kind: str, path: str, detail: str = ""):
    """Record one file in the ledger: hash it, append a manifest event.

    No bytes are copied — the file stays where it lives. For sessions,
    stories, ezines, code, and samples: the ledger is the record.
    Returns the event. Raises on unreadable files (callers that must
    not fail on ledger errors catch this themselves).
    """
    sha = sha256_file(path)
    size = os.path.getsize(path)
    return append(data_dir, kind, _ledger_path_for(data_dir, path), sha, size, detail)


def chunk_file(data_dir: str, src_path: str):
    """Split a file into 50MB content-addressed chunks (no ledger events).

    Streams the source one chunk at a time (one chunk in memory).
    Each chunk is written to data/blocks/<chunk-sha>.chunk unless
    already present (dedup). Returns (file_sha256, size, chunks,
    merkle_root) where chunks = [{index, sha256, size}]. Chunk files
    are content cache; ledger history comes from store_chunked or
    from the caller's own manifest event.
    """
    chunks = []
    file_hash = hashlib.sha256()
    with open(src_path, "rb") as f:
        index = 0
        while True:
            buf = f.read(CHUNK_SIZE)
            if not buf:
                break
            sha = hashlib.sha256(buf).hexdigest()
            file_hash.update(buf)
            pth = chunk_path(data_dir, sha)
            if not os.path.exists(pth):
                tmp = pth + ".tmp"
                with open(tmp, "wb") as out:
                    out.write(buf)
                os.replace(tmp, pth)
            chunks.append({"index": index, "sha256": sha, "size": len(buf)})
            index += 1
    return file_hash.hexdigest(), os.path.getsize(src_path), chunks, \
        merkle_root([c["sha256"] for c in chunks])


def store_chunked(data_dir: str, kind: str, src_path: str, name: str, detail: str = ""):
    """Store a file as 50MB content-addressed chunks + a manifest.

    Chunks go to data/blocks/ (deduped), one block event per chunk
    lands in the ledger, and one manifest event closes the set. The
    manifest (data/blocks/<file-sha>.manifest.json) lists the chunks
    in order, the whole-file sha256, and the Merkle root over the
    chunk hashes. Returns (manifest, manifest_event).
    """
    file_sha, size, chunks, root = chunk_file(data_dir, src_path)
    for c in chunks:
        append(data_dir, "block", c["sha256"] + ".chunk", c["sha256"], c["size"], name)
    manifest = {"version": 1,
                "files": [{"name": name, "sha256": file_sha, "size": size,
                           "chunks": chunks}],
                "merkle_root": root,
                "prev_hash": tip_hash(data_dir),
                "node_id": _identity(data_dir).node_id, "created_at": time.time()}
    sign_manifest(data_dir, manifest)
    with open(manifest_path(data_dir, file_sha), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    event = append(data_dir, kind, name, file_sha, size, detail)
    return manifest, event


def get_chunk(data_dir: str, sha: str):
    """Return one chunk's bytes by its sha256, or None."""
    if len(sha) != 64:
        return None
    try:
        int(sha, 16)
    except ValueError:
        return None
    pth = chunk_path(data_dir, sha)
    if not os.path.isfile(pth):
        return None
    with open(pth, "rb") as f:
        return f.read()


def verify_blocks(data_dir: str):
    """Check every block event's chunk file exists and hashes right.

    Returns (ok, checked, error). Manifest/record events reference
    files that live elsewhere and are not checked here.
    """
    events = _load(data_dir)
    checked = 0
    for event in events:
        if event.get("kind") != "block":
            continue
        sha = event.get("sha256", "")
        pth = chunk_path(data_dir, sha)
        if not os.path.isfile(pth):
            return False, checked, f"missing chunk {sha[:16]}"
        if sha256_file(pth) != sha:
            return False, checked, f"chunk hash mismatch {sha[:16]}"
        checked += 1
    return True, checked, ""


def list_events(data_dir: str, limit=50):
    """Ledger events, newest first, for inspection views."""
    events = _load(data_dir)
    return list(reversed(events))[:max(1, min(200, limit))]


def blocks_count(data_dir: str) -> int:
    d = os.path.join(data_dir, "blocks")
    if not os.path.isdir(d):
        return 0
    return sum(1 for n in os.listdir(d) if n.endswith(".chunk"))
