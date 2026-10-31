"""Tunnel client: how a firewalled node serves the network.

This box (and any node behind NAT) cannot accept inbound connections, so
it never listens. Instead it OPENS an outbound connection to the list
server (node.darknet.ca) and polls its mailbox — the same mailbox +
poller pattern the sovereign-chat relay used. Outbound HTTPS passes
through firewalls; nothing inbound is ever required.

Flow for capabilities served here (tts: the PocketTTS bridge; text:
a local chat model via Ollama — see ollama_text, the swap point if
a different backend is chosen later):
  1. Phone/laptop sends a signed job to the node's mailbox on the list
     server. The server only checks the signature + that the sender
     is listed — it is a dumb pipe, not a gate.
  2. This poller (running next to the bridge) polls its own mailbox,
     verifies each job's signature AND its own allow/deny/block
     policy, runs the job against the local service, and posts a
     signed reply to the requester's mailbox. A peer set to "allow"
     is a whitelist: persistent, no per-job ask.
  3. The requester polls its mailbox and gets the result.

The bridge itself stays on localhost (127.0.0.1:8000). It is never
exposed directly; the only path in is a signed job through the
mailbox, decided by this node's own policy. Known prototype limits:
polling is at-most-once (a job polled but not yet replied is lost if
the poller dies mid-job), and mailbox values cap at KV's 25MB — long
audio goes through TC containers instead of the mailbox.

Image capability: an "image" job appends one line to the node's
Z-Image queue (seed|name|WxH|prompt) and the poller sweeps finished
renders (test_output/<name>.png) each cycle, replying with the PNG
base64 when it fits the mailbox (<= 20MB) and with a signed TC
manifest otherwise. Every capability — tts, image, text — is gated
by THIS node's own allow/deny/mute policy (peers.json); there is no
central grant and no sysop to ask.

Runs anywhere node.py runs (Windows, Linux, Termux). Stdlib only.
"""

import base64
import json
import os
import re
import secrets
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node as node_mod  # noqa: E402

BRIDGE_URL = "http://127.0.0.1:8000"


def _post(list_url, path, payload):
    req = urllib.request.Request(list_url + path, method="POST",
                                 data=json.dumps(payload).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = {}
        return e.code, body


def send_job(list_url, sender_identity, to_pubkey, capability, payload):
    """Queue a signed job in the target node's mailbox. Returns (status, body).

    Retried with the SAME signed payload (same job id): a dropped
    send response must not leave the sender unsure whether the job
    queued, and a duplicate post of the same id is deduped by the
    serving poller's _seen_jobs, so retrying is safe."""
    import secrets
    job = {
        "type": "job",
        "id": secrets.token_hex(8),
        "to_pubkey": to_pubkey,
        "capability": capability,
        "payload": payload,
    }
    return _post_retry(list_url, "/mailbox/send",
                       node_mod.signed_payload(sender_identity, job))


def poll_mailbox(list_url, identity):
    """Fetch + clear this node's mailbox. Returns (status, body)."""
    payload = node_mod.signed_payload(identity, {"type": "poll"})
    return _post(list_url, "/mailbox/poll", payload)


def _post_retry(list_url, path, payload, attempts=5):
    """POST with retries and backoff (2/4/8/16s, ~30s window).
    5 requests (RemoteDisconnected); a dropped reply or re-queue is
    a LOST message, so retry. The failure mode this accepts is a
    duplicate (first attempt stored, its response dropped): the
    awaiter takes the first copy and the spare bounces out — better
    than a job processed with no reply ever reaching its sender."""
    last = (0, {})
    for i in range(attempts):
        try:
            st, body = _post(list_url, path, payload)
        except Exception as e:
            st, body = 0, {"error": "%s: %s" % (type(e).__name__, e)}
        if st == 200:
            return st, body
        last = (st, body)
        if i < attempts - 1:
            time.sleep(2 * (2 ** i))
    return last


def reply_to(list_url, identity, to_pubkey, in_reply_to, result):
    payload = node_mod.signed_payload(identity, {
        "type": "reply",
        "to_pubkey": to_pubkey,
        "in_reply_to": in_reply_to,
        "result": result,
    })
    return _post_retry(list_url, "/mailbox/reply", payload)


def requeue_message(list_url, message):
    """Post a fetched message back to its mailbox, verbatim.

    Used by every mailbox consumer for messages that are not theirs:
    the serving poller bounces replies (see MAX_REQUEUES), and reply
    awaiters bounce jobs. The server re-verifies the ORIGINAL
    signature on post, so a forged or tampered message cannot bounce
    (its post is rejected) — and a genuine message keeps its original
    signature, sender, and addressing, so it lands back in exactly the
    mailbox it came from. Without this, fetch-and-clear would let any
    consumer silently destroy another consumer's messages. Bounces
    retry only 3 times: a failed bounce goes to the caller's pending
    list and is retried next cycle anyway, and every retry that lands
    after a dropped response is a duplicate KV write against the
    shared 1000/day free-tier budget.
    """
    return _post_retry(list_url, "/mailbox/send", message, attempts=3)


def ollama_text(text, model=None, url=None):
    """Chat with a local Ollama model. Returns {"text": ...}, the exact
    shape the Android app's ChatClient expects, or raises on any
    Ollama error — never an echo, never a canned reply. The caller
    (TunnelClient) turns a raise into {"error": ...} in the reply, so
    a down Ollama server fails loud instead of faking a chat answer.

    Backend swap point: replace this one function (or pass a
    different callable as the "text" handler) to serve text from
    another backend; the mailbox shape and the node's own policy
    gates do not change. Defaults: model from TEXT_MODEL, else
    qwen2.5-coder:1.5b (this box's existing local model); URL from
    OLLAMA_URL, else http://127.0.0.1:11434.
    """
    model = model or os.environ.get("TEXT_MODEL", "qwen2.5-coder:1.5b")
    url = (url or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
    req = urllib.request.Request(
        url + "/api/chat", method="POST",
        data=json.dumps({"model": model, "stream": False,
                         "messages": [{"role": "user", "content": text}]}).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read().decode("utf-8", "replace"))
    content = body.get("message", {}).get("content", "")
    if not content:
        raise ValueError("ollama returned no message content: %r" % (body,)[:200])
    return {"text": content}


def llama_text(text, url=None):
    """Chat with a local llama-server model (the LFM3 path). Returns
    {"text": ...}, the exact shape the Android app's ChatClient
    expects, or raises on any error — never an echo, never a canned
    reply. Same fail-loud contract as ollama_text.

    Why a second backend: Ollama's bundled llama.cpp predates LFM3
    support (0.34.4 fails with "unknown model architecture: 'lfm3'"),
    and the box's llama-server only gained it at build 11941
    (support postdates b11744). The server is model-agnostic — it
    serves whatever GGUF it was started with — so this takes no model
    argument: point it at a server running the model you want. URL
    from LLAMA_URL, else http://127.0.0.1:11436. Prompt format mirrors
    the Android app's local chat: plain "User: ...\\nAssistant:" with
    "User:" stops, 512 tokens max per turn.
    """
    url = (url or os.environ.get("LLAMA_URL", "http://127.0.0.1:11436")).rstrip("/")
    req = urllib.request.Request(
        url + "/completion", method="POST",
        data=json.dumps({"prompt": "User: %s\nAssistant:" % text,
                         "n_predict": 512, "temperature": 0.7,
                         "top_k": 40, "top_p": 0.9,
                         "stop": ["User:", "\nUser:"]}).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read().decode("utf-8", "replace"))
    content = (body.get("content") or "").strip()
    if not content:
        raise ValueError("llama-server returned no content: %r" % (body,)[:200])
    return {"text": content}


def text_handler(payload):
    """Tunnel "text" handler: payload {"text": ...} -> {"text": ...}."""
    return ollama_text(payload.get("text", ""))


def bridge_tts(text, voice="mya-03"):
    """Call the local PocketTTS bridge (localhost only). Requests MP3 —
    the format the Pixel app plays — and returns a dict with base64
    audio, or raises on bridge errors."""
    req = urllib.request.Request(
        BRIDGE_URL + "/v1/audio/speech", method="POST",
        data=json.dumps({"input": text, "voice": voice,
                         "response_format": "mp3"}).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        audio = r.read()
        ctype = r.headers.get("Content-Type", "audio/wav")
    return {"audio_b64": base64.b64encode(audio).decode("ascii"),
            "content_type": ctype, "bytes": len(audio)}


IMAGE_MAILBOX_MAX = 20_000_000  # PNGs bigger than this go via TC, not mailbox

# Replies re-queued, not dropped, when the serving poll meets them.
# A node both serves jobs (poller) and awaits replies (its Jobs tab,
# a self-test): mailbox polling is fetch-and-clear, so without this
# the poller would eat replies meant for the awaiting side. Each
# re-queue is one KV write, and every poll is one too (the Worker
# saves the mailbox on every poll, even an empty one), so the bounce
# is bounded: after MAX_REQUEUES cycles an unfetched reply is
# dropped. The old bound of 3 gave a manual fetcher only ~3 minutes
# at a 60s poller — the voice-job failure of 2026-10-29, where a user
# who fetched a little late found nothing and no error. 10 cycles
# (~10 minutes at 60s) still costs at most 10 writes per unfetched
# reply, trivial on the Paid plan, and the web UI now auto-fetches
# voice replies every 15s anyway, so the bounce is a safety net,
# not the only path to the reply.
MAX_REQUEUES = 10
MAX_REPLY_TRIES = 30  # cycles a failed job reply is retried (~30 min at 60s)


def engine_offered(requested, configured):
    """An image job's engine request against this node's engine.
    Blank means 'the node's default' — always offered. A named
    engine must match exactly, or the job fails loud with an error
    reply instead of rendering on the wrong engine."""
    if not requested:
        return True
    return str(requested) == str(configured)


def queue_image_job(payload, queue_path):
    """Append one Z-Image queue line (seed|name|WxH|prompt) for an
    "image" job. Payload: {prompt, name?, width?, height?, seed?}.
    The queue format splits on "|", so prompts are sanitized (no pipes
    or newlines); names are sanitized to safe filename characters.
    Returns {"job": name, "status": "queued", "seed": seed, "size": WxH}."""
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("image job needs a prompt")
    prompt = prompt.replace("|", "/").replace("\n", " ").replace("\r", " ")

    def _dim(value, default):
        try:
            v = int(value)
        except (TypeError, ValueError):
            return default
        return max(16, min(2048, v))

    width = _dim(payload.get("width"), 512)
    height = _dim(payload.get("height"), 512)
    try:
        seed = int(payload.get("seed"))
    except (TypeError, ValueError):
        seed = secrets.randbelow(1000000)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(payload.get("name", "")))[:64]
    if not name:
        name = "node-%d" % seed
    line = "%d|%s|%dx%d|%s\n" % (seed, name, width, height, prompt)
    with open(queue_path, "a") as f:
        f.write(line)
    return {"job": name, "status": "queued", "seed": seed,
            "size": "%dx%d" % (width, height)}


def serving_config(data_dir):
    """Operator serving config (data/serving.json), best-effort.

    {} when unset: the poller's CLI flags stay in charge. Keys:
    text_backend (llama|ollama|openrouter|nvidia), text_model,
    cloud_key (stored on disk ONLY when the operator opts into
    serving a cloud model — see nodeapi.set_serving), tts_voice,
    public_endpoint (announced for direct connections).
    """
    try:
        with open(os.path.join(data_dir, "serving.json")) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def serving_voice(data_dir):
    """Default TTS voice for jobs that name none: serving.json first,
    then engines.json, then mya-03."""
    cfg = serving_config(data_dir)
    if (cfg.get("tts_voice") or "").strip():
        return cfg["tts_voice"].strip()
    try:
        with open(os.path.join(data_dir, "engines.json")) as f:
            eng = json.load(f)
        if (eng.get("tts_voice") or "").strip():
            return eng["tts_voice"].strip()
    except (OSError, ValueError):
        pass
    return "mya-03"


def text_dispatch(data_dir, fallback, llama_url, payload):
    """Text handler that honors serving.json when the operator set it.

    No serving.json (or no text_backend in it) -> the poller's CLI
    fallback (llama/ollama). A cloud backend needs a stored key
    (operator opt-in) and a model; without them it fails loud
    instead of silently serving nothing. The cloud import is lazy
    so this module stays importable where hub/ is absent.
    """
    text = (payload or {}).get("text", "")
    cfg = serving_config(data_dir)
    backend = (cfg.get("text_backend") or fallback or "ollama").strip()
    if backend == "llama":
        return llama_text(text, url=llama_url)
    if backend in ("openrouter", "nvidia"):
        key = (cfg.get("cloud_key") or "").strip()
        model = (cfg.get("text_model") or "").strip()
        if not key:
            return {"error": "cloud serving key not set — set it on the Serving page"}
        if not model:
            return {"error": "cloud serving model not set — set it on the Serving page"}
        try:
            from hub import cloud as cloud_mod
        except Exception as e:
            return {"error": "cloud backend unavailable here: %s: %s" % (type(e).__name__, e)}
        try:
            out = cloud_mod.chat_cloud(backend, key, model,
                                       [{"role": "user", "content": text}])
        except Exception as e:
            return {"error": "%s: %s" % (type(e).__name__, e)}
        return {"text": out}
    return ollama_text(text)


class TunnelClient:
    """Polls our mailbox and serves local capabilities to peers this
    node allows (its own allow/deny/block policy)."""

    def __init__(self, data_dir, list_url, allow, handlers,
                 image_queue=None, image_outdir=None, peers_file=None,
                 image_engine="z-image"):
        self.identity, _ = node_mod.load_or_create_identity(data_dir)
        self.data_dir = data_dir
        self.list_url = list_url.rstrip("/")
        self.allow = set(allow)
        self.handlers = handlers  # capability -> callable(payload) -> result
        self.image_queue = image_queue  # path to zimage queue.txt, or None
        self.image_outdir = image_outdir  # zimage test_output dir, or None
        self.image_engine = image_engine  # this node's image engine
        self.peers_file = peers_file or os.path.join(data_dir, "peers.json")
        self.ledger_path = os.path.join(data_dir, "image_jobs.json")
        self.last_deliveries = []  # [(job_id, "mailbox"|"tc")] from last sweep
        self._requeue_counts = {}  # in_reply_to -> re-queues so far
        self._pending_requeues = []  # replies whose bounce failed (burst)
        self._pending_replies = []  # job replies whose post failed (burst)
        self._seen_jobs = set()  # job ids already answered (dedup below)

    def _peers(self):
        """This node's own per-peer policy: {pubkey: allow|deny|block}.
        A peer entry wins over the allow set; unlisted peers fall back
        to the allow set, else silent skip."""
        try:
            with open(self.peers_file) as f:
                peers = json.load(f)
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in peers.items()
                if v in ("allow", "deny", "block")}

    def _ledger_load(self):
        try:
            with open(self.ledger_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _ledger_save(self, entries):
        tmp = self.ledger_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(entries, f, indent=2, sort_keys=True)
        os.replace(tmp, self.ledger_path)

    def deliver_finished_images(self):
        """Sweep ledger entries whose render finished. Small PNGs go back
        as a mailbox reply (base64); large ones are TC-packaged into
        data_dir/outbox/<transfer_id>/ and the reply carries the signed
        manifest (requester fetches chunks from the serving node's
        serve_outbox address). Returns [(job_id, kind)]."""
        if not self.image_outdir:
            return []
        entries = self._ledger_load()
        delivered, remaining = [], []
        for entry in entries:
            png = os.path.join(self.image_outdir, entry["name"] + ".png")
            if not os.path.isfile(png):
                remaining.append(entry)
                continue
            with open(png, "rb") as f:
                data = f.read()
            if len(data) <= IMAGE_MAILBOX_MAX:
                result = {"image_b64": base64.b64encode(data).decode("ascii"),
                          "content_type": "image/png", "name": entry["name"]}
                kind = "mailbox"
            else:
                transfer_id = "img-" + entry["job_id"]
                outbox = os.path.join(self.data_dir, "outbox", transfer_id)
                manifest = node_mod.split_file(self.identity, png, outbox,
                                               transfer_id)
                result = {"tc_transfer": transfer_id, "tc_manifest": manifest,
                          "name": entry["name"],
                          "tc_note": "fetch chunks with node.py fetch_transfer "
                                     "from this node's serve_outbox address"}
                kind = "tc"
            status, _ = reply_to(self.list_url, self.identity,
                                 entry["requester"], entry["job_id"], result)
            if status == 200:
                delivered.append((entry["job_id"], kind))
            else:
                remaining.append(entry)  # retry next sweep
        self._ledger_save(remaining)
        return delivered

    def _flush_pending(self):
        """Retry reply re-queues that failed during a proxy burst.

        A failed bounce must not mean a lost reply: the message is
        held here and retried on every cycle until it posts (or hits
        the MAX_REQUEUES bound and is dropped as stale). The bounce
        count increments only on a successful post.
        """
        still = []
        for job in self._pending_requeues:
            rid = job.get("in_reply_to", "")
            if self._requeue_counts.get(rid, 0) >= MAX_REQUEUES:
                print("tunnel: dropping stale pending reply to %s" % rid)
                self._requeue_counts.pop(rid, None)
                continue
            st, _ = requeue_message(self.list_url, job)
            if st == 200:
                self._requeue_counts[rid] = self._requeue_counts.get(rid, 0) + 1
            else:
                still.append(job)
        self._pending_requeues = still
        # Failed job replies are held the same way: a reply post that
        # failed (proxy burst) is retried every cycle until it posts.
        # After MAX_REPLY_TRIES cycles it is dropped LOUDLY — the
        # sender's re-send is then the only recovery, and the log
        # says so instead of staying silent about a lost reply.
        still = []
        for item in self._pending_replies:
            st, _ = reply_to(self.list_url, self.identity,
                             item["to"], item["jid"], item["result"])
            if st == 200:
                print("tunnel: held reply for job %s delivered on retry" % item["jid"])
                self._seen_jobs.add(item["jid"])
                if len(self._seen_jobs) > 1000:
                    self._seen_jobs.clear()
                    self._seen_jobs.add(item["jid"])
            elif item["tries"] + 1 >= MAX_REPLY_TRIES:
                print("tunnel: dropping undelivered reply for job %s after %d tries — sender must re-send"
                      % (item["jid"], MAX_REPLY_TRIES))
            else:
                item["tries"] += 1
                still.append(item)
        self._pending_replies = still

    def _reply_or_hold(self, to_pubkey, jid, result):
        """Post a job reply; on failure HOLD it for retry next cycle.

        A failed post used to vanish silently (run_once printed
        "processed" either way, and the reply was gone with the
        consumed job). Now the failure is logged loudly and the
        reply is retried every cycle until it posts or hits
        MAX_REPLY_TRIES. Returns the reply_to status.
        """
        rst, _ = reply_to(self.list_url, self.identity, to_pubkey, jid, result)
        if rst == 200:
            self._seen_jobs.add(jid)
            if len(self._seen_jobs) > 1000:
                self._seen_jobs.clear()
                self._seen_jobs.add(jid)
        else:
            print("tunnel: reply for job %s FAILED (%s) — held for retry" % (jid, rst))
            if not any(p["jid"] == jid for p in self._pending_replies):
                self._pending_replies.append(
                    {"to": to_pubkey, "jid": jid, "result": result, "tries": 0})
                if len(self._pending_replies) > 1000:
                    self._pending_replies.pop(0)
        return rst

    def run_once(self):
        """One poll cycle. Returns (processed_ids, skipped_ids)."""
        self._flush_pending()
        status, body = poll_mailbox(self.list_url, self.identity)
        if status != 200:
            return [], []
        processed, skipped = [], []
        for job in body.get("messages", []):
            # Replies are not jobs. This node may both serve (this
            # poller) and await replies (its Jobs tab, a self-test):
            # fetch-and-clear would otherwise let the poller eat
            # replies meant for the awaiting side. Re-queue them
            # verbatim — the server re-verifies the original
            # signature on post, so a forged reply cannot bounce.
            # Bounded (see MAX_REQUEUES): an unfetched reply is
            # dropped after a minute of bouncing. A bounce that
            # fails (proxy burst) is held in _pending_requeues and
            # retried next cycle, not lost.
            if job.get("type") == "reply":
                rid = job.get("in_reply_to", "")
                if self._requeue_counts.get(rid, 0) >= MAX_REQUEUES:
                    print("tunnel: dropping stale reply to %s after %d re-queues"
                          % (rid, MAX_REQUEUES))
                    self._requeue_counts.pop(rid, None)
                else:
                    st, _ = requeue_message(self.list_url, job)
                    if st == 200:
                        self._requeue_counts[rid] = self._requeue_counts.get(rid, 0) + 1
                        if len(self._requeue_counts) > 1000:
                            self._requeue_counts.clear()
                            self._requeue_counts[rid] = 1
                    else:
                        print("tunnel: reply re-queue failed, held for retry (%s)" % st)
                        self._pending_requeues.append(job)
                        if len(self._pending_requeues) > 1000:
                            self._pending_requeues.pop(0)
                # Bounced, not skipped: replies are neither processed
                # jobs nor skipped jobs, so they appear in neither list.
                continue
            sender = job.get("pubkey", "")
            cap = job.get("capability", "")
            # Dedup: retries after ambiguous posts (first attempt
            # stored, its response dropped) can put identical copies
            # of one job in the mailbox. Answering twice wastes a
            # full bridge/model run, so a job answered with a
            # delivered (200) reply is never answered again. A job
            # whose reply post failed is NOT re-run: the reply itself
            # is held in _pending_replies and retried every cycle
            # until it posts (at-least-once for the reply,
            # at-most-once for the work).
            jid = job.get("id")
            if jid in self._seen_jobs:
                print("tunnel: duplicate job %s skipped" % jid)
                continue
            # Verify the job signature first; then this node's own peer
            # policy (block = silent skip, deny = explicit error reply);
            # then the offered capability.
            unsigned = {k: v for k, v in job.items()
                        if k not in ("signature", "pubkey")}
            signed_form = dict(unsigned)
            signed_form["pubkey"] = sender
            ok_sig = node_mod.verify_signature(
                sender, node_mod.canonical(signed_form), job.get("signature", ""))
            if not ok_sig:
                skipped.append(job.get("id"))
                continue
            policy = self._peers().get(sender)
            if policy == "block":
                skipped.append(job.get("id"))
                continue
            if policy == "deny":
                self._reply_or_hold(sender, job.get("id"),
                                    {"error": "peer denied by this node"})
                processed.append(job.get("id"))
                continue
            is_image = cap == "image" and self.image_queue is not None
            if policy != "allow" and sender not in self.allow:
                skipped.append(job.get("id"))
                continue
            if not (cap in self.handlers or is_image):
                # Allowed peer, but this node does not offer that
                # capability (e.g. a tts job sent to a text-only
                # poller). Silently dropping it used to make voice
                # jobs "never show" with no error anywhere — fail
                # loud with an error reply instead, so the sender's
                # UI can show why there is no audio.
                rst, _ = reply_to(self.list_url, self.identity, sender, job.get("id"),
                         {"error": "capability '%s' not offered by this node" % cap})
                if rst == 200:
                    self._seen_jobs.add(jid)
                processed.append(job.get("id"))
                continue
            if is_image and not engine_offered(
                    (job.get("payload", {}) or {}).get("engine"),
                    self.image_engine):
                # Wrong engine for this node (e.g. stable-diffusion
                # sent to a z-image node): fail loud, never render
                # on the wrong engine and pretend it was the request.
                self._reply_or_hold(sender, job.get("id"),
                                    {"error": "engine '%s' not offered here (this node runs %s)"
                                     % (job["payload"].get("engine"), self.image_engine)})
                processed.append(job.get("id"))
                continue
            try:
                if is_image:
                    result = queue_image_job(job.get("payload", {}),
                                             self.image_queue)
                    entries = self._ledger_load()
                    entries.append({"job_id": job.get("id"),
                                    "requester": sender,
                                    "name": result["job"],
                                    "seed": result["seed"],
                                    "size": result["size"]})
                    self._ledger_save(entries)
                else:
                    result = self.handlers[cap](job.get("payload", {}))
            except Exception as e:
                result = {"error": "%s: %s" % (type(e).__name__, e)}
            self._reply_or_hold(sender, job.get("id"), result)
            processed.append(job.get("id"))
        try:
            self.last_deliveries = self.deliver_finished_images()
        except Exception as e:
            print("tunnel: image sweep error: %s" % e)
            self.last_deliveries = []
        return processed, skipped

    def run_forever(self, interval=5):
        while True:
            try:
                processed, skipped = self.run_once()
                if processed:
                    print("tunnel: processed %s" % processed)
                if self.last_deliveries:
                    print("tunnel: delivered %s" % (self.last_deliveries,))
            except Exception as e:
                print("tunnel: poll error: %s" % e)
            time.sleep(interval)
