"""Node tab backend: the Gopher-model node client, wired to the Hub.

One identity: this module reads the SAME data/identity.dat the Hub
uses (32-byte secp256k1 secret, mode 0600). The Hub derives its HUB-
fingerprint and hub node id from the compressed pubkey; node.py
derives its NODE- fingerprint and node- id from the same pubkey.
Whichever side creates identity.dat first, the other side reads the
same 32 bytes — there is no second identity file and no conversion.

All list calls fail soft: a down or unreachable list server returns
{"error": ...}, never an exception, so the Node tab degrades instead
of 500ing. Policy and favorites never leave the node (local JSON in
the data dir, same as the standalone client).

Stdlib only.
"""

import json
import os
import re
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import node as node_mod  # noqa: E402
import tunnel as tunnel_mod  # noqa: E402

from .config import data_dir as hub_data_dir  # noqa: E402
from .identity import fingerprint as hub_fingerprint  # noqa: E402

DEFAULT_LIST_URL = os.environ.get(
    "NODE_LIST_URL", "https://node.darknet.ca")
NAME_FILE = "node_name.txt"


def list_url():
    return DEFAULT_LIST_URL


def _dd(data_dir=None):
    return data_dir or hub_data_dir()


def get_name(data_dir=None):
    try:
        with open(os.path.join(_dd(data_dir), NAME_FILE)) as f:
            return f.read().strip()
    except OSError:
        return ""


def set_name(name, data_dir=None):
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise ValueError("node name must be 1-64 characters")
    d = _dd(data_dir)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, NAME_FILE + ".tmp")
    with open(tmp, "w") as f:
        f.write(name)
    os.replace(tmp, os.path.join(d, NAME_FILE))
    return name


def _sanitize_engine(value):
    """An image engine tag: 1-40 letters/digits/dot/underscore/dash.
    Returns the cleaned tag, or None when blank/invalid (blank means
    'the peer's default engine')."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > 40 or not re.match(r"^[A-Za-z0-9._-]+$", s):
        return None
    return s


def _engines(data_dir=None):
    """Operator-declared engines for announce tags (data/engines.json):
    {"image_engines": ["z-image"], "tts_engine": "pockettts",
     "tts_voice": "mya-03"}. Absent or invalid means no tags — the
    node still announces its base capabilities either way."""
    try:
        with open(os.path.join(_dd(data_dir), "engines.json"),
                  encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        out = {}
        imgs = [e for e in (raw.get("image_engines") or [])
                if _sanitize_engine(e)]
        if imgs:
            out["image_engines"] = imgs
        if _sanitize_engine(raw.get("tts_engine")):
            out["tts_engine"] = _sanitize_engine(raw.get("tts_engine"))
        if _sanitize_engine(raw.get("tts_voice")):
            out["tts_voice"] = _sanitize_engine(raw.get("tts_voice"))
        return out
    except Exception:
        return {}


def _client(data_dir=None, name=None, engines=None, endpoint=None):
    return node_mod.NodeClient(
        _dd(data_dir),
        name if name is not None else get_name(data_dir),
        list_url(), engines=engines,
        endpoint=endpoint if endpoint is not None else "")


def _is_hex(s):
    try:
        int(s, 16)
        return True
    except (TypeError, ValueError):
        return False


def _ok(body, status):
    if status == 200:
        return True, body
    err = body.get("error") if isinstance(body, dict) else None
    return False, {"error": err or ("list server error %s" % status)}


def status(data_dir=None):
    """Identity (both fingerprint formats, one secret) + node name."""
    d = _dd(data_dir)
    ident, _ = node_mod.load_or_create_identity(d)
    listed = None
    try:
        st, body = _client(d).nodes()
        if st == 200:
            listed = any(n.get("node_id") == ident.node_id
                         for n in body.get("nodes", []))
    except Exception:
        listed = None
    return {
        "node_id": ident.node_id,
        "fingerprint": ident.fingerprint,
        "hub_fingerprint": hub_fingerprint(ident.pubkey),
        "pubkey": ident.public_hex(),
        "name": get_name(d),
        "list_url": list_url(),
        "listed": listed,
    }


def peers(data_dir=None):
    try:
        st, body = _client(data_dir).nodes()
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    ok, out = _ok(body, st)
    return {"nodes": out.get("nodes", [])} if ok else out


def announce(data_dir=None):
    name = get_name(data_dir)
    if not name:
        return {"error": "set a node name first"}
    try:
        st, body = _client(data_dir, name, _engines(data_dir),
                           endpoint=get_serving(data_dir).get("public_endpoint", "")).announce()
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    if st == "skipped":
        return {"error": "announce disabled"}
    ok, out = _ok(body, st)
    return {"ok": ok, "status": st, "body": out}


def lobby(data_dir=None):
    """Recent public lobby messages, enriched with each sender's
    announced name + node_id from the /nodes list.

    The Worker stores only pubkey + name on a lobby post, and its
    name comes from the sender's announced record — not from the
    message itself — so a name cannot be spoofed by just typing it.
    The UI shows name with node_id small underneath, and node_id is
    derived from the pubkey, so two nodes claiming the same name are
    visibly different people. Enriching here (not in the Worker)
    means no Worker redeploy is needed for the new lobby UI."""
    try:
        st, body = _client(data_dir).lobby()
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    ok, out = _ok(body, st)
    if not ok:
        return out
    messages = out.get("messages", [])
    try:
        st2, body2 = _client(data_dir).nodes()
        by_pubkey = {}
        if st2 == 200:
            for n in body2.get("nodes", []):
                if n.get("pubkey"):
                    by_pubkey[n["pubkey"]] = n
        for m in messages:
            rec = by_pubkey.get(m.get("pubkey"), {})
            # Announced name wins over any name on the message itself.
            if rec.get("name"):
                m["name"] = rec["name"]
            if rec.get("node_id"):
                m["node_id"] = rec["node_id"]
    except Exception:
        pass
    return {"messages": messages}


def lobby_send(text, data_dir=None):
    text = (text or "").strip()
    if not text:
        return {"error": "empty message"}
    # Lobby gate: no name, no chat. An unnamed post would show up
    # with a blank name in the lobby, which is exactly the spoofing
    # hole the name + node_id display is meant to close. The UI
    # disables the input too; this is the server-side backstop.
    if not get_name(data_dir):
        return {"error": "set a name first"}
    if len(text) > 2000:
        return {"error": "lobby messages cap at 2000 characters"}
    try:
        st, body = _client(data_dir).lobby_send(text)
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    ok, out = _ok(body, st)
    return {"ok": ok, "status": st, "body": out}


def chat_poll(data_dir=None):
    try:
        st, body = _client(data_dir).chat_poll()
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    ok, out = _ok(body, st)
    return {"messages": out.get("messages", [])} if ok else out


def chat_send(to_pubkey, text, data_dir=None):
    to_pubkey = (to_pubkey or "").strip().lower()
    text = (text or "").strip()
    if not _is_hex(to_pubkey) or len(to_pubkey) != 66:
        return {"error": "recipient must be a 66-hex compressed pubkey"}
    if not text:
        return {"error": "empty message"}
    try:
        st, body = _client(data_dir).chat_send(to_pubkey, text)
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    ok, out = _ok(body, st)
    return {"ok": ok, "status": st, "body": out}


def uploads(data_dir=None):
    try:
        st, body = node_mod._get(list_url(), "/uploads")
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    ok, out = _ok(body, st)
    return {"uploads": out.get("uploads", [])} if ok else out


def publish_offer(filename, size, file_sha256, data_dir=None):
    """Publish a local shared file as a public offer on the list.

    The offer is signed with the node key (node.py upload); bytes move
    peer-to-peer afterwards. Returns (ok, body) like the other calls.
    """
    st, body = _client(data_dir).upload(filename, size, file_sha256)
    return _ok(body, st)


def policy(data_dir=None):
    return {"policy": node_mod.peers_load(_dd(data_dir))}


def set_policy(pubkey, action, data_dir=None):
    pubkey = (pubkey or "").strip().lower()
    if not _is_hex(pubkey) or len(pubkey) != 66:
        return {"error": "peer must be a 66-hex compressed pubkey"}
    if action not in ("allow", "deny", "block", "clear"):
        return {"error": "action must be allow, deny, block, or clear"}
    d = _dd(data_dir)
    peers = node_mod.peers_load(d)
    if action == "clear":
        peers.pop(pubkey, None)
    else:
        peers[pubkey] = action
    node_mod.peers_save(d, peers)
    return {"ok": True, "policy": peers}


def favorites(data_dir=None):
    return {"favorites": node_mod.favorites_load(_dd(data_dir))}


def add_favorite(pubkey, name, data_dir=None):
    pubkey = (pubkey or "").strip().lower()
    if not _is_hex(pubkey) or len(pubkey) != 66:
        return {"error": "peer must be a 66-hex compressed pubkey"}
    d = _dd(data_dir)
    favs = node_mod.favorites_load(d)
    favs[pubkey] = {"name": (name or "").strip()[:64],
                    "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    node_mod.favorites_save(d, favs)
    return {"ok": True, "favorites": favs}


def remove_favorite(pubkey, data_dir=None):
    pubkey = (pubkey or "").strip().lower()
    d = _dd(data_dir)
    favs = node_mod.favorites_load(d)
    favs.pop(pubkey, None)
    node_mod.favorites_save(d, favs)
    return {"ok": True, "favorites": favs}


def send_job(to_pubkey, capability, payload, data_dir=None):
    """Queue a signed job in a peer's mailbox (text/tts/image jobs from
    the UI). The peer's own policy decides whether it runs; replies
    arrive via mailbox_poll, not via chat. Payloads are validated and
    sanitized here so a bad form cannot queue a malformed job; the
    serving node's own handlers validate again on receipt."""
    to_pubkey = (to_pubkey or "").strip().lower()
    if not _is_hex(to_pubkey) or len(to_pubkey) != 66:
        return {"error": "peer must be a 66-hex compressed pubkey"}
    if capability not in ("text", "tts", "image"):
        return {"error": "capability must be text, tts, or image"}
    if not isinstance(payload, dict):
        return {"error": "job needs a payload"}
    if capability in ("text", "tts"):
        if not (payload.get("text") or "").strip():
            return {"error": "job needs text"}
        clean = {"text": payload["text"].strip()}
        if capability == "tts" and (payload.get("voice") or "").strip():
            clean["voice"] = payload["voice"].strip()
    else:  # image
        if not (payload.get("prompt") or "").strip():
            return {"error": "image job needs a prompt"}
        clean = {"prompt": payload["prompt"].strip()}
        for key in ("width", "height"):
            if payload.get(key) is not None:
                try:
                    clean[key] = max(16, min(2048, int(payload[key])))
                except (TypeError, ValueError):
                    return {"error": "%s must be a number" % key}
        if payload.get("seed") is not None:
            try:
                clean["seed"] = int(payload["seed"])
            except (TypeError, ValueError):
                return {"error": "seed must be a number"}
        if (payload.get("name") or "").strip():
            clean["name"] = payload["name"].strip()
        eng = _sanitize_engine(payload.get("engine"))
        if eng is None and (payload.get("engine") or ""):
            return {"error": "engine must be 1-40 letters, digits, . _ -"}
        if eng:
            clean["engine"] = eng
    try:
        ident, _ = node_mod.load_or_create_identity(_dd(data_dir))
        st, body = tunnel_mod.send_job(
            list_url(), ident, to_pubkey, capability, clean)
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    ok, out = _ok(body, st)
    if ok:
        record_transfer(out.get("id"), to_pubkey, capability, clean)
        return {"ok": True, "job_id": out.get("id"), "status": out.get("status")}
    return out


def mailbox_poll(data_dir=None):
    """Fetch + clear this node's mailbox (job replies land here).

    Only replies are returned: any job message met here is re-queued
    verbatim for the serving poller. Without that, fetching replies
    would eat incoming jobs (fetch-and-clear) and the poller would
    never serve them — the mirror image of the poller eating replies,
    which tunnel.py already bounces.
    """
    try:
        ident, _ = node_mod.load_or_create_identity(_dd(data_dir))
        st, body = tunnel_mod.poll_mailbox(list_url(), ident)
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    ok, out = _ok(body, st)
    if not ok:
        return out
    replies = []
    for m in out.get("messages", []):
        if m.get("type") == "reply":
            replies.append(m)
            update_transfer(m.get("in_reply_to"), m.get("result") or {})
        else:
            tunnel_mod.requeue_message(list_url(), m)
    return {"messages": replies}


# ---- Transfers: every sent job tracked to its reply (uTorrent-style) ----

TRANSFERS_FILE = "transfers.json"
TRANSFERS_CAP = 200


def _transfers_path(data_dir=None):
    return os.path.join(_dd(data_dir), TRANSFERS_FILE)


def _transfers_load(data_dir=None):
    try:
        with open(_transfers_path(data_dir)) as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _transfers_save(rows, data_dir=None):
    rows = rows[-TRANSFERS_CAP:]
    tmp = _transfers_path(data_dir) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f)
    os.replace(tmp, _transfers_path(data_dir))
    return rows


def _transfer_snippet(capability, payload):
    if capability in ("text", "tts"):
        return (payload.get("text") or "")[:120]
    return (payload.get("prompt") or "")[:120]


def record_transfer(job_id, to_pubkey, capability, payload, data_dir=None):
    """Append a sent job to the local transfer log. Local only, and
    the payload is capped so a long text cannot bloat the file."""
    if not job_id:
        return None
    payload = dict(payload or {})
    if isinstance(payload.get("text"), str):
        payload["text"] = payload["text"][:8000]
    if isinstance(payload.get("prompt"), str):
        payload["prompt"] = payload["prompt"][:8000]
    rows = _transfers_load(data_dir)
    rows.append({
        "job_id": job_id, "to_pubkey": to_pubkey, "capability": capability,
        "snippet": _transfer_snippet(capability, payload),
        "payload": payload,
        "voice": payload.get("voice", "") if capability == "tts" else "",
        "status": "sent", "sent_at": time.time(), "updated_at": time.time(),
    })
    _transfers_save(rows, data_dir)
    return rows[-1]


def _reply_kind(result):
    if result.get("error"):
        return "error"
    if result.get("audio_b64"):
        return "audio"
    if result.get("image_b64"):
        return "image"
    if result.get("text"):
        return "text"
    return "done"


def update_transfer(job_id, result, data_dir=None):
    """Mark a transfer done/failed when its reply arrives. Unknown
    job ids (replies to jobs sent before tracking, or from another
    UI) are recorded too, so the list still shows them."""
    if not job_id:
        return None
    result = result or {}
    rows = _transfers_load(data_dir)
    row = next((r for r in rows if r["job_id"] == job_id), None)
    if row is None:
        row = {
            "job_id": job_id, "to_pubkey": "", "capability": "",
            "snippet": "", "payload": {}, "voice": "",
            "sent_at": time.time(),
        }
        rows.append(row)
    row["status"] = "failed" if result.get("error") else "done"
    row["error"] = result.get("error", "") if result.get("error") else ""
    if isinstance(result.get("text"), str):
        row["reply_text"] = result["text"][:300]
    row["reply_kind"] = _reply_kind(result)
    row["updated_at"] = time.time()
    _transfers_save(rows, data_dir)
    return row


def list_transfers(data_dir=None):
    """Newest first. This log tracks status, not reply bytes — the
    bytes arrive with the mailbox fetch and render there."""
    return {"transfers": list(reversed(_transfers_load(data_dir)))}


def resend_transfer(job_id, data_dir=None):
    """Re-send a tracked job's payload as a new job (fresh id). The
    old row is marked resent; the new send is tracked in turn."""
    rows = _transfers_load(data_dir)
    row = next((r for r in rows if r["job_id"] == job_id), None)
    if not row:
        return {"error": "unknown transfer"}
    if row["capability"] not in ("text", "tts", "image"):
        return {"error": "transfer has no resendable payload"}
    if not row.get("to_pubkey"):
        return {"error": "transfer has no peer"}
    out = send_job(row["to_pubkey"], row["capability"],
                   row.get("payload") or {}, data_dir=data_dir)
    if out.get("error"):
        return out
    # send_job recorded the new row, so re-read before marking the
    # old one resent — saving the stale list would clobber it.
    rows = _transfers_load(data_dir)
    row = next((r for r in rows if r["job_id"] == job_id), None)
    if row is None:
        return {"error": "original transfer vanished"}
    row["status"] = "resent"
    row["updated_at"] = time.time()
    _transfers_save(rows, data_dir)
    return {"ok": True, "old_job_id": job_id, "job_id": out.get("job_id")}


# ---- Serving: operator-set defaults for what this node serves ----

SERVING_FILE = "serving.json"
SERVING_BACKENDS = ("llama", "ollama", "openrouter", "nvidia")


def _serving_path(data_dir=None):
    return os.path.join(_dd(data_dir), SERVING_FILE)


def get_serving(data_dir=None):
    """Serving config with the key redacted (never returned)."""
    try:
        with open(_serving_path(data_dir)) as f:
            cfg = json.load(f)
        cfg = cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        cfg = {}
    return {
        "text_backend": cfg.get("text_backend", ""),
        "text_model": cfg.get("text_model", ""),
        "tts_voice": cfg.get("tts_voice", ""),
        "public_endpoint": cfg.get("public_endpoint", ""),
        "has_cloud_key": bool(cfg.get("cloud_key")),
    }


def set_serving(body, data_dir=None):
    """Validate + store serving config. The cloud key is written to
    disk (0600) ONLY when the operator sets a cloud backend and
    supplies a key — serving a cloud model headless needs the key
    on disk, and that tradeoff is the operator's explicit opt-in.
    Switching to a local backend drops the stored key."""
    body = body or {}
    backend = (body.get("text_backend") or "").strip()
    if backend and backend not in SERVING_BACKENDS:
        return {"error": "text_backend must be llama, ollama, openrouter, or nvidia"}
    model = (body.get("text_model") or "").strip()
    if backend in ("openrouter", "nvidia") and not model:
        return {"error": "a cloud backend needs a model"}
    endpoint = (body.get("public_endpoint") or "").strip()
    if endpoint and not endpoint.startswith("https://"):
        return {"error": "public_endpoint must be https:// or empty"}
    if len(endpoint) > 200:
        return {"error": "public_endpoint too long"}
    voice = (body.get("tts_voice") or "").strip()
    try:
        with open(_serving_path(data_dir)) as f:
            cfg = json.load(f)
        cfg = cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        cfg = {}
    if backend:
        cfg["text_backend"] = backend
    else:
        cfg.pop("text_backend", None)
    cfg["text_model"] = model
    cfg["tts_voice"] = voice
    cfg["public_endpoint"] = endpoint
    key = (body.get("cloud_key") or "").strip()
    if backend in ("openrouter", "nvidia") and key:
        cfg["cloud_key"] = key
    else:
        cfg.pop("cloud_key", None)
    tmp = _serving_path(data_dir) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, _serving_path(data_dir))
    try:
        os.chmod(_serving_path(data_dir), 0o600)
    except OSError:
        pass
    return {"ok": True, "serving": get_serving(data_dir)}
