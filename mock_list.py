"""In-memory mock of the node.darknet.ca list server, for tests only.

Gopher model: the list is open, the gates are per-node.
  GET  /nodes            -> listed nodes with their ANNOUNCED
                            capabilities (what each node offers)
  POST /gossip           -> echo (compat with the current discovery worker)
  POST /announce         -> signed announce -> listed immediately
                            with the capabilities it claims to offer
  POST /mailbox/send     -> signed job to a node's mailbox. The server
                            checks the signature + that the sender is
                            listed — nothing more. Whether the job RUNS
                            is the receiving node's own decision
                            (its poller allow/deny/block policy).
  POST /mailbox/poll     -> fetch + clear own mailbox
  POST /mailbox/reply    -> signed reply to a requester's mailbox
  POST /upload           -> signed file offer -> listed offers
  GET  /uploads          -> offered files (each receiver decides its
                            own downloads; no central accept step)
  POST /lobby/send       -> signed public lobby post (listed nodes only)
  GET  /lobby            -> recent lobby messages (last 100)
  POST /chatbox/send     -> signed private sysop-chat message
                            (signature + listed check only; the
                            recipient decides who it answers)
  POST /chatbox/poll     -> fetch + clear own private chat messages

There is NO central sysop and NO grant system in this model:
allow/deny/mute live on each node (its poller peer policy), and a
whitelist is just a peer set to "allow" — persistent, no per-job ask.
Signatures are verified with node.py's verifier (same regula_core scheme).
"""

import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import node as node_mod  # noqa: E402


class Store:
    def __init__(self):
        self.pending_announces = {}   # pubkey -> announce payload
        self.nodes = {}               # pubkey -> public list entry
        self.uploads = []             # offered files (no accept step)
        self.mailbox = {}             # pubkey -> queued job/reply messages
        self.lobby = []               # public lobby messages (last 100)
        self.chatbox = {}             # pubkey -> private chat messages


def list_entry(ann):
    """Public list entry for an announced node. Capabilities are what
    the node CLAIMS to offer (its announce payload) — whether anyone
    may use them is each receiving node's own decision, not a
    server-side grant. There is no verified flag in this model:
    recognition is per-node (allow-list / favorites), not global."""
    return {
        "node_id": ann["node_id"],
        "fingerprint": ann["fingerprint"],
        "name": ann["name"],
        "pubkey": ann["pubkey"],
        "capabilities": list(ann.get("capabilities", [])),
        "endpoint": ann.get("endpoint", ""),
    }


def verify_signed(payload):
    """A signed payload carries pubkey + signature over the canonical
    payload-without-those-two-fields. Returns True/False."""
    sig = payload.get("signature", "")
    pubkey = payload.get("pubkey", "")
    unsigned = {k: v for k, v in payload.items()
                if k not in ("signature", "pubkey")}
    # The signer signed the fields PLUS its pubkey (see signed_payload):
    # canonical over fields-with-pubkey, without signature.
    signed_form = dict(unsigned)
    signed_form["pubkey"] = pubkey
    return node_mod.verify_signature(pubkey, node_mod.canonical(signed_form), sig)


def make_handler(store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                return None

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/nodes":
                self._send(200, {"format": "tc-node-list", "version": 1,
                                 "nodes": list(store.nodes.values())})
            elif parsed.path == "/uploads":
                self._send(200, {"uploads": store.uploads})
            elif parsed.path == "/lobby":
                self._send(200, {"messages": store.lobby})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            payload = self._read_json()
            if payload is None:
                self._send(400, {"error": "bad json"})
                return
            if self.path == "/gossip":
                self._send(200, {"echo": payload})
                return
            if self.path == "/mailbox/send":
                if not verify_signed(payload):
                    self._send(401, {"error": "bad signature"})
                    return
                # Gopher model: the server checks signature + listed
                # ONLY. Whether the job runs is the receiving node's
                # own decision (its poller allow/deny/block policy).
                if payload.get("pubkey", "") not in store.nodes:
                    self._send(403, {"error": "not a listed node"})
                    return
                store.mailbox.setdefault(payload["to_pubkey"], []).append(payload)
                self._send(200, {"status": "queued", "id": payload.get("id")})
                return
            if self.path == "/mailbox/poll":
                if not verify_signed(payload):
                    self._send(401, {"error": "bad signature"})
                    return
                msgs = store.mailbox.pop(payload.get("pubkey", ""), [])
                self._send(200, {"messages": msgs})
                return
            if self.path == "/mailbox/reply":
                if not verify_signed(payload):
                    self._send(401, {"error": "bad signature"})
                    return
                store.mailbox.setdefault(payload["to_pubkey"], []).append(payload)
                self._send(200, {"status": "queued"})
                return
            if self.path == "/lobby/send":
                if not verify_signed(payload):
                    self._send(401, {"error": "bad signature"})
                    return
                sender = payload.get("pubkey", "")
                if sender not in store.nodes:
                    self._send(403, {"error": "not a listed node"})
                    return
                text = str(payload.get("text", "")).strip()
                if not text:
                    self._send(400, {"error": "empty message"})
                    return
                entry = {"pubkey": sender,
                         "name": store.nodes[sender].get("name", ""),
                         "text": text[:2000],
                         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                store.lobby.append(entry)
                store.lobby = store.lobby[-100:]
                self._send(200, {"status": "posted"})
                return
            if self.path == "/chatbox/send":
                if not verify_signed(payload):
                    self._send(401, {"error": "bad signature"})
                    return
                sender = payload.get("pubkey", "")
                if sender not in store.nodes:
                    self._send(403, {"error": "not a listed node"})
                    return
                text = str(payload.get("text", "")).strip()
                if not text:
                    self._send(400, {"error": "empty message"})
                    return
                store.chatbox.setdefault(payload["to_pubkey"], []).append({
                    "from_pubkey": sender,
                    "from_name": store.nodes.get(sender, {}).get("name", ""),
                    "text": text[:2000],
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                self._send(200, {"status": "queued"})
                return
            if self.path == "/chatbox/poll":
                if not verify_signed(payload):
                    self._send(401, {"error": "bad signature"})
                    return
                msgs = store.chatbox.pop(payload.get("pubkey", ""), [])
                self._send(200, {"messages": msgs})
                return
            if self.path == "/announce":
                if not verify_signed(payload):
                    self._send(401, {"error": "bad signature"})
                    return
                store.pending_announces[payload["pubkey"]] = payload
                # Gopher model: a valid announce lists the node
                # immediately, with the capabilities it claims to
                # offer. No approval, no verified flag — recognition
                # is per-node, not global.
                store.nodes[payload["pubkey"]] = list_entry(payload)
                self._send(200, {"status": "listed",
                                 "note": "listed; each node decides its own allow/deny/mute"})
                return
            if self.path == "/upload":
                if not verify_signed(payload):
                    self._send(401, {"error": "bad signature"})
                    return
                store.uploads.append(payload)
                self._send(200, {"status": "offered",
                                 "note": "offer listed; each receiver decides its own downloads"})
                return
            self._send(404, {"error": "not found"})

    return H


def serve(store, port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]
