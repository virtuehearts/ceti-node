"""Portable node client: runs on Windows, Linux, and Termux (Android).

Stdlib only — no pip packages, no compiled extensions, no Hub imports.
A single file the user can copy to a laptop or a Pixel 7 Pro and run with
any Python 3.8+.

Identity: data_dir/identity.dat holds a fresh 32-byte secp256k1 secret
(mode 0600 where the OS supports it), created on first run. Only the
compressed public key ever leaves the machine. Signing is the regula_core
scheme (ECDSA secp256k1 over double-SHA256, DER, RFC 6979 nonces, low-S),
byte-compatible with the Hub's hub/package.py — a signature made here
verifies there and vice versa.

Flow:
  1. Run the client with a name: it loads (or creates) its identity.
  2. With --announce yes (the default), it announces itself to the list
     at node.darknet.ca: name + pubkey + capabilities + endpoint, signed.
     With --announce no it stays silent and never contacts the list.
  3. To use another node's resources, just send it a job: the mailbox
     is a dumb pipe (signature + listed check only). Whether the job
     RUNS is that node's own decision — its allow/deny/mute policy.
     A whitelist is a peer set to "allow": persistent, no per-job ask.
     To ask an operator to whitelist you, message them in the lobby
     or by sysop chat.
  4. Capabilities, in order: text (now); music, tts (PocketTTS bridge),
     image, music-gen, files (later). Files are offered to the public
     list; each receiver decides its own downloads (fetch allow-list).

Usage:
  python node.py --data-dir ./data --name "pixel-7" --announce yes nodes
  python node.py --data-dir ./data --name "pixel-7" announce
  python node.py --data-dir ./data upload --file model.gguf --size 2146495872
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.request
import urllib.error
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# --- secp256k1 (stdlib only) ------------------------------------------------
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

DEFAULT_LIST_URL = "https://node.darknet.ca"
CAPABILITIES_NOW = ["text"]
CAPABILITIES_LATER = ["music", "tts", "image", "music-gen", "files"]


def _announce_caps(capabilities, engines):
    """Base capabilities plus engine tags, deduped in order.
    Tags (image-engine:<e>, tts-engine:<e>, tts-voice:<v>) ride in
    capabilities because the Worker passes that array through
    verbatim; a dedicated engines field would need a redeploy."""
    out = []
    for c in list(capabilities or []):
        if c not in out:
            out.append(c)
    for e in (engines or {}).get("image_engines", []):
        tag = "image-engine:" + str(e)
        if tag not in out:
            out.append(tag)
    if (engines or {}).get("tts_engine"):
        tag = "tts-engine:" + str(engines["tts_engine"])
        if tag not in out:
            out.append(tag)
    if (engines or {}).get("tts_voice"):
        tag = "tts-voice:" + str(engines["tts_voice"])
        if tag not in out:
            out.append(tag)
    return out
CHUNK_SIZE = 50 * 1024 * 1024  # 50 MiB containers (same format as tc.py)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and y1 != y2:
        return None
    if p == q:
        lam = (3 * x1 * x1) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def _mul(k, point=(Gx, Gy)):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def compressed_pubkey(secret):
    k = int.from_bytes(secret, "big") % N
    if k == 0:
        k = 1
    x, y = _mul(k)
    return (b"\x03" if (y & 1) else b"\x02") + x.to_bytes(32, "big")


def _decompress_pubkey(comp):
    if len(comp) != 33 or comp[0] not in (2, 3):
        raise ValueError("not a compressed secp256k1 public key")
    x = int.from_bytes(comp[1:], "big")
    if x >= P:
        raise ValueError("public key x out of range")
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if (y * y - y_sq) % P != 0:
        raise ValueError("public key point not on curve")
    if (y & 1) != (comp[0] & 1):
        y = P - y
    return (x, y)


def sha256d(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _der_int(x):
    b = x.to_bytes((x.bit_length() + 7) // 8 or 1, "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return b"\x02" + bytes([len(b)]) + b


def _der_encode(r, s):
    body = _der_int(r) + _der_int(s)
    return b"\x30" + bytes([len(body)]) + body


def _der_decode(data):
    if len(data) < 2 or data[0] != 0x30:
        raise ValueError("not a DER sequence")
    total = data[1]
    if total & 0x80 or len(data) != 2 + total:
        raise ValueError("bad DER length")
    body = data[2:]
    if not body or body[0] != 0x02:
        raise ValueError("bad DER integer tag")
    l1 = body[1]
    r = int.from_bytes(body[2:2 + l1], "big")
    rest = body[2 + l1:]
    if not rest or rest[0] != 0x02 or len(rest) != 2 + rest[1]:
        raise ValueError("bad DER integer tag")
    s = int.from_bytes(rest[2:], "big")
    return r, s


def _rfc6979_k(secret_int, digest):
    z1 = int.from_bytes(digest, "big") % N
    bx = secret_int.to_bytes(32, "big") + z1.to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + bx, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + bx, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_payload(secret, payload):
    digest = sha256d(payload)
    secret_int = int.from_bytes(secret, "big") % N
    if secret_int == 0:
        raise ValueError("invalid secret")
    k = _rfc6979_k(secret_int, digest)
    r = _mul(k)[0] % N
    if r == 0:
        raise ValueError("degenerate nonce")
    z = int.from_bytes(digest, "big")
    s = (pow(k, -1, N) * (z + r * secret_int)) % N
    if s == 0:
        raise ValueError("degenerate signature")
    if s > N // 2:
        s = N - s
    return _der_encode(r, s).hex()


def verify_signature(pubkey_hex, payload, signature_hex):
    try:
        point = _decompress_pubkey(bytes.fromhex(pubkey_hex))
        r, s = _der_decode(bytes.fromhex(signature_hex))
        if not (1 <= r < N and 1 <= s < N):
            return False
        z = int.from_bytes(sha256d(payload), "big")
        w = pow(s, -1, N)
        point_sum = _add(_mul((z * w) % N), _mul((r * w) % N, point))
        if point_sum is None:
            return False
        return point_sum[0] % N == r
    except Exception:
        return False


# --- identity ----------------------------------------------------------------
class Identity:
    def __init__(self, secret):
        self.secret = secret
        self.pubkey = compressed_pubkey(secret)
        h = hashlib.sha256(self.pubkey).hexdigest()
        self.hash = h
        self.fingerprint = "NODE-" + "-".join(
            h[i:i + 4] for i in range(0, 16, 4)).upper()
        self.node_id = "node-" + h[:12]

    def public_hex(self):
        return self.pubkey.hex()


def load_or_create_identity(data_dir):
    """Load data_dir/identity.dat, or create it. Returns (Identity, existed)."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "identity.dat")
    if os.path.exists(path):
        with open(path, "rb") as f:
            secret = f.read()
        if len(secret) != 32:
            raise ValueError("identity.dat is not a 32-byte secret; "
                             "back it up and delete it to regenerate")
        return Identity(secret), True
    secret = secrets.token_bytes(32)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(secret)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows has no POSIX modes; the file is still private by path
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return Identity(secret), False


# --- list API ----------------------------------------------------------------
def _post(list_url, path, payload):
    req = urllib.request.Request(list_url + path, method="POST",
                                 data=json.dumps(payload).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = {}
        return e.code, body


def _get(list_url, path):
    try:
        with urllib.request.urlopen(list_url + path, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = {}
        return e.code, body


def signed_payload(identity, fields):
    """Attach the sender's pubkey + signature over the canonical fields."""
    payload = dict(fields)
    payload["pubkey"] = identity.public_hex()
    payload["signature"] = sign_payload(identity.secret, canonical(payload))
    return payload


class NodeClient:
    def __init__(self, data_dir, name, list_url=DEFAULT_LIST_URL,
                 announce=True, capabilities=None, endpoint="",
                 engines=None):
        self.identity, self.existed = load_or_create_identity(data_dir)
        self.name = name
        self.list_url = list_url.rstrip("/")
        self.announce_enabled = announce
        self.capabilities = capabilities if capabilities is not None \
            else list(CAPABILITIES_NOW)
        self.endpoint = endpoint
        # Operator-declared engines (data/engines.json via the Hub):
        # announced as capability tags, because the Worker's list
        # entry passes capabilities through verbatim while dropping
        # unknown fields — a clean engines field needs a Worker
        # redeploy, tags work today with no redeploy.
        self.engines = engines or {}

    def announce(self):
        """Announce this node to the list. Returns (status, body); when
        disabled, status is "skipped" and nothing is sent."""
        if not self.announce_enabled:
            return "skipped", {"skipped": True, "reason": "announce node: no"}
        payload = signed_payload(self.identity, {
            "type": "announce",
            "node_id": self.identity.node_id,
            "fingerprint": self.identity.fingerprint,
            "name": self.name,
            "capabilities": _announce_caps(self.capabilities, self.engines),
            "endpoint": self.endpoint,
        })
        return _post(self.list_url, "/announce", payload)

    def nodes(self):
        return _get(self.list_url, "/nodes")

    def chat_send(self, to_pubkey, text):
        """Private sysop-chat message (separate chatbox queue, so the
        job poller can never eat chat). Anyone listed can send; the
        recipient decides who it answers."""
        payload = signed_payload(self.identity, {
            "type": "chat", "to_pubkey": to_pubkey, "text": text})
        return _post(self.list_url, "/chatbox/send", payload)

    def chat_poll(self):
        """Fetch + clear own private chat messages."""
        return _post(self.list_url, "/chatbox/poll",
                     signed_payload(self.identity, {"type": "poll"}))

    def lobby(self):
        """Recent public lobby messages."""
        return _get(self.list_url, "/lobby")

    def lobby_send(self, text):
        """Post a signed public lobby message (listed nodes only)."""
        payload = signed_payload(self.identity, {
            "type": "lobby", "text": text})
        return _post(self.list_url, "/lobby/send", payload)

    def upload(self, filename, size, file_sha256):
        """Offer a file: it lands in the public offers list. Each
        receiver decides its own downloads (its fetch allow-list);
        there is no central accept step. Bytes themselves move
        peer-to-peer (TC) afterwards."""
        payload = signed_payload(self.identity, {
            "type": "upload",
            "node_id": self.identity.node_id,
            "name": self.name,
            "filename": filename,
            "size": size,
            "file_sha256": file_sha256,
        })
        return _post(self.list_url, "/upload", payload)


# --- local lists: favorites + per-peer policy (never leave the node) -----
def _json_load(data_dir, name):
    try:
        with open(os.path.join(data_dir, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _json_save(data_dir, name, obj):
    os.makedirs(data_dir, exist_ok=True)
    tmp = os.path.join(data_dir, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, os.path.join(data_dir, name))


def favorites_load(data_dir):
    """{pubkey: {"name": str, "added": iso}} — nodes this node favorites."""
    return _json_load(data_dir, "favorites.json")


def favorites_save(data_dir, favs):
    _json_save(data_dir, "favorites.json", favs)


def peers_load(data_dir):
    """{pubkey: "allow"|"deny"|"block"} — this node's own peer policy.
    Block = silently drop everything from that peer; deny = explicit
    error reply; allow = normal. Unlisted peers fall back to the
    poller's allow set, else silent skip."""
    peers = _json_load(data_dir, "peers.json")
    return {k: v for k, v in peers.items()
            if v in ("allow", "deny", "block")}


def peers_save(data_dir, peers):
    _json_save(data_dir, "peers.json", peers)


# --- file transfer (TC containers, same format as tc-prototype/tc.py) ------
# Container file: one JSON header line, then raw payload bytes.
# Header: transfer_id, index, total_chunks, payload_sha256, sender_pubkey,
# signature over the canonical header-without-signature. The manifest is
# signed the same way. The receiver keeps an append-only ledger: one entry
# per chunk, accepted or rejected. Nothing reassembles unless every chunk
# verifies AND the sender is on the receiver's allow-list AND the file's
# upload offer is listed (checked by fetch_transfer).

def make_transfer_manifest(identity, transfer_id, filename, total_size,
                           file_sha256, total_chunks):
    manifest = {
        "format": "tc-transfer",
        "version": 1,
        "transfer_id": transfer_id,
        "filename": filename,
        "total_size": total_size,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": total_chunks,
        "file_sha256": file_sha256,
        "sender_pubkey": identity.public_hex(),
        "sender_fingerprint": identity.fingerprint,
    }
    manifest["signature"] = sign_payload(identity.secret, canonical(manifest))
    return manifest


def verify_transfer_manifest(manifest):
    sig = manifest.get("signature", "")
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    return verify_signature(manifest["sender_pubkey"], canonical(unsigned), sig)


def split_file(identity, src_path, out_dir, transfer_id):
    """Split src into 50MB signed containers + a signed manifest.
    Two streaming passes over the source: never holds more than one
    chunk in memory, so multi-gigabyte models work on small machines."""
    os.makedirs(out_dir, exist_ok=True)
    file_hash = hashlib.sha256()
    total_size = 0
    total = 0
    with open(src_path, "rb") as f:
        while True:
            payload = f.read(CHUNK_SIZE)
            if not payload:
                break
            file_hash.update(payload)
            total_size += len(payload)
            total += 1
    with open(src_path, "rb") as f:
        for i in range(total):
            payload = f.read(CHUNK_SIZE)
            header = {
                "transfer_id": transfer_id,
                "index": i,
                "total_chunks": total,
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "sender_pubkey": identity.public_hex(),
            }
            header["signature"] = sign_payload(identity.secret, canonical(header))
            with open(os.path.join(out_dir, "chunk-%06d.tc" % i), "wb") as out:
                out.write(json.dumps(header, sort_keys=True).encode("utf-8"))
                out.write(b"\n")
                out.write(payload)
    manifest = make_transfer_manifest(
        identity, transfer_id, os.path.basename(src_path),
        total_size, file_hash.hexdigest(), total)
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest


def _ledger_append(ledger_path, entry):
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(ledger_path, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def receive_containers(manifest, chunk_paths, allow, ledger_path, out_path):
    """Verify manifest + chunks, ledger every chunk, reassemble if all pass.
    Returns (ok, detail)."""
    sender = manifest.get("sender_pubkey", "")
    if sender not in allow:
        _ledger_append(ledger_path, {"transfer_id": manifest.get("transfer_id"),
                                     "decision": "denied", "reason": "sender not allowed"})
        return False, "sender not allowed"
    if not verify_transfer_manifest(manifest):
        _ledger_append(ledger_path, {"transfer_id": manifest.get("transfer_id"),
                                     "decision": "denied", "reason": "bad manifest signature"})
        return False, "bad manifest signature"
    total = manifest["total_chunks"]
    payloads = {}
    for path in sorted(chunk_paths):
        with open(path, "rb") as f:
            header_line = f.readline()
            payload = f.read()
        try:
            header = json.loads(header_line.decode("utf-8"))
        except ValueError:
            _ledger_append(ledger_path, {"transfer_id": manifest["transfer_id"],
                                         "decision": "rejected", "reason": "unreadable header"})
            return False, "unreadable chunk header"
        sig = header.get("signature", "")
        unsigned = {k: v for k, v in header.items() if k != "signature"}
        ok_sig = verify_signature(header.get("sender_pubkey", ""),
                                  canonical(unsigned), sig)
        ok_hash = hashlib.sha256(payload).hexdigest() == header.get("payload_sha256")
        ok_meta = (header.get("transfer_id") == manifest["transfer_id"]
                   and header.get("total_chunks") == total
                   and header.get("sender_pubkey") == sender)
        if not (ok_sig and ok_hash and ok_meta):
            reason = ("bad signature" if not ok_sig else
                      "payload hash mismatch" if not ok_hash else "manifest mismatch")
            _ledger_append(ledger_path, {"transfer_id": manifest["transfer_id"],
                                         "index": header.get("index"),
                                         "payload_sha256": header.get("payload_sha256"),
                                         "decision": "rejected", "reason": reason})
            return False, "chunk %s rejected: %s" % (header.get("index"), reason)
        _ledger_append(ledger_path, {"transfer_id": manifest["transfer_id"],
                                     "index": header["index"],
                                     "payload_sha256": header["payload_sha256"],
                                     "decision": "accepted"})
        payloads[header["index"]] = payload
    if len(payloads) != total or sorted(payloads) != list(range(total)):
        return False, "incomplete chunk set"
    with open(out_path, "wb") as f:
        for i in range(total):
            f.write(payloads[i])
    with open(out_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    if digest != manifest["file_sha256"]:
        return False, "reassembled file hash mismatch"
    return True, "reassembled %d chunks, file sha256 matches manifest" % total


def _get_bytes(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def fetch_transfer(list_url, base_url, allow, out_dir):
    """Fetch a transfer from the sender's outbox URL, enforcing the gates:
    the file's upload offer must be listed, the sender must be
    allow-listed BY THE RECEIVER (its own choice — no central accept),
    and every chunk must verify. Returns (ok, detail, manifest)."""
    os.makedirs(out_dir, exist_ok=True)
    manifest = json.loads(_get_bytes(base_url.rstrip("/") + "/manifest.json").decode("utf-8"))
    status, offers = _get(list_url.rstrip("/"), "/uploads")
    if status != 200 or not any(
            u.get("file_sha256") == manifest.get("file_sha256")
            for u in offers.get("uploads", [])):
        return False, "upload offer not listed", manifest
    tmp = os.path.join(out_dir, ".tc-download")
    os.makedirs(tmp, exist_ok=True)
    chunk_paths = []
    try:
        for i in range(manifest["total_chunks"]):
            data = _get_bytes("%s/chunk-%06d.tc" % (base_url.rstrip("/"), i))
            path = os.path.join(tmp, "chunk-%06d.tc" % i)
            with open(path, "wb") as f:
                f.write(data)
            chunk_paths.append(path)
    except Exception as e:
        return False, "download failed: %s" % e, manifest
    out_path = os.path.join(out_dir, manifest["filename"])
    ledger_path = os.path.join(out_dir, "transfer-ledger.jsonl")
    ok, detail = receive_containers(manifest, chunk_paths, allow, ledger_path, out_path)
    return ok, detail, manifest


def serve_outbox(out_dir, port):
    """Serve an outbox directory over HTTP (for the fetching node)."""
    handler = partial(SimpleHTTPRequestHandler, directory=out_dir)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print("serving %s at http://0.0.0.0:%d/ (Ctrl-C to stop)" % (out_dir, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return server


def _yes_no(value):
    v = value.strip().lower()
    if v in ("yes", "y", "true", "1"):
        return True
    if v in ("no", "n", "false", "0"):
        return False
    raise argparse.ArgumentTypeError("expected yes or no")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Portable node client")
    ap.add_argument("--data-dir", default=os.path.expanduser("~/.node"))
    ap.add_argument("--name", required=True, help="node name, set when run")
    ap.add_argument("--list-url", default=DEFAULT_LIST_URL)
    ap.add_argument("--announce", type=_yes_no, default=True,
                    help="announce node: yes or no (default yes)")
    ap.add_argument("--capabilities", default=",".join(CAPABILITIES_NOW))
    ap.add_argument("--endpoint", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("identity", help="print this node's pubkey/fingerprint "
                   "(never the secret) — paste the pubkey as SYSOP_PUBKEY")
    sub.add_parser("announce")
    sub.add_parser("nodes")
    up = sub.add_parser("upload")
    up.add_argument("--file", required=True)
    up.add_argument("--size", type=int, required=True)
    up.add_argument("--sha256", default="")
    sd = sub.add_parser("send")
    sd.add_argument("--file", required=True)
    sd.add_argument("--outbox", required=True)
    sd.add_argument("--transfer-id", default="")
    ft = sub.add_parser("fetch")
    ft.add_argument("--base-url", required=True, help="sender's outbox URL")
    ft.add_argument("--out", required=True)
    ft.add_argument("--allow-pubkey", action="append", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--dir", required=True)
    sv.add_argument("--port", type=int, default=8000)
    fv = sub.add_parser("favorite")
    fv.add_argument("action", choices=["add", "remove", "list"])
    fv.add_argument("pubkey", nargs="?", default="")
    fv.add_argument("--name", default="")
    pr = sub.add_parser("peer")
    pr.add_argument("action", choices=["allow", "deny", "block", "list"])
    pr.add_argument("pubkey", nargs="?", default="")
    cs = sub.add_parser("chat-send")
    cs.add_argument("target")
    cs.add_argument("text", nargs="+")
    sub.add_parser("chat-poll")
    sub.add_parser("lobby")
    ls = sub.add_parser("lobby-send")
    ls.add_argument("text", nargs="+")
    args = ap.parse_args(argv)

    client = NodeClient(args.data_dir, args.name, args.list_url,
                        args.announce, args.capabilities.split(",") or [],
                        args.endpoint)
    if args.cmd == "identity":
        # Local only: no network, no secret in output. The pubkey printed
        # here is the value to paste as SYSOP_PUBKEY in wrangler.toml.
        status, body = "local", {
            "pubkey": client.identity.public_hex(),
            "created_now": not client.existed,
        }
    elif args.cmd == "announce":
        status, body = client.announce()
    elif args.cmd == "nodes":
        status, body = client.nodes()
    elif args.cmd == "upload":
        status, body = client.upload(args.file, args.size, args.sha256)
    elif args.cmd == "send":
        file_hash = hashlib.sha256()
        with open(args.file, "rb") as f:
            while True:
                block = f.read(1024 * 1024)
                if not block:
                    break
                file_hash.update(block)
        file_sha = file_hash.hexdigest()
        tid = args.transfer_id or ("tc-" + file_sha[:12])
        manifest = split_file(client.identity, args.file, args.outbox, tid)
        status, body = "local", {"transfer_id": tid,
                                 "chunks": manifest["total_chunks"],
                                 "outbox": args.outbox,
                                 "file_sha256": file_sha}
    elif args.cmd == "fetch":
        ok, detail, manifest = fetch_transfer(
            args.list_url, args.base_url, set(args.allow_pubkey), args.out)
        status, body = ("ok" if ok else "refused",
                        {"detail": detail, "transfer_id": manifest.get("transfer_id"),
                         "filename": manifest.get("filename")})
    elif args.cmd == "serve":
        serve_outbox(args.dir, args.port)
        return 0
    elif args.cmd == "favorite":
        favs = favorites_load(args.data_dir)
        if args.action == "add" and args.pubkey:
            favs[args.pubkey] = {
                "name": args.name,
                "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            favorites_save(args.data_dir, favs)
        elif args.action == "remove" and args.pubkey:
            favs.pop(args.pubkey, None)
            favorites_save(args.data_dir, favs)
        status, body = "local", {"favorites": favs}
    elif args.cmd == "peer":
        peers = peers_load(args.data_dir)
        if args.action in ("allow", "deny", "block") and args.pubkey:
            peers[args.pubkey] = args.action
            peers_save(args.data_dir, peers)
        status, body = "local", {"peers": peers}
    elif args.cmd == "chat-send":
        status, body = client.chat_send(args.target, " ".join(args.text))
    elif args.cmd == "chat-poll":
        status, body = client.chat_poll()
    elif args.cmd == "lobby":
        status, body = client.lobby()
    elif args.cmd == "lobby-send":
        status, body = client.lobby_send(" ".join(args.text))
    print(json.dumps({"node_id": client.identity.node_id,
                      "fingerprint": client.identity.fingerprint,
                      "http_status": status, "result": body}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
