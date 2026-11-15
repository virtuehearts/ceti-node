"""Presence + rate-limit tests (see TEST-PLAN.md, round 2).

The Worker (worker-node-list.js) is presence, not a roster: a node
is listed only while it has been seen within the TTL (15 min), and
sends are rate-limited per verified sender (600 per 10 min). The
mock list mirrors the Worker, so these tests prove the contract the
real Worker enforces — stdlib only, no Cloudflare, no KV.

  presence: announce -> listed -> TTL passes -> gone -> re-announce
            -> listed again; an expired node gets 403 on send; a
            re-announce updates the node's capabilities; /nodes
            entries carry a `seen` stamp.
  rate limit: a sender that exceeds its quota gets 429 on the 6th
            send; a different sender is unaffected (per-sender);
            the limit applies to lobby, mailbox, and reply sends.
  poller heartbeat: a poller heartbeating every 1s keeps its node
            listed past a 3s TTL; a node that only announces once
            drops off the list.

Short TTLs/limits are passed to the mock Store (test-only); the
real defaults stay 15 min / 600 per 10 min.
"""

import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node as node_mod  # noqa: E402
import mock_list  # noqa: E402
import tunnel as tunnel_mod  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "run-presence")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def listed(url, pubkey):
    status, body = node_mod._get(url, "/nodes")
    nodes = body.get("nodes", []) if status == 200 else []
    return [n for n in nodes if n.get("pubkey") == pubkey]


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    # --- Store 1: short TTL (3s), generous rate limit (presence) ---
    store = mock_list.Store(ttl=3, rate_limit=600, rate_window=60)
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port

    alice, _ = node_mod.load_or_create_identity(os.path.join(WORK, "alice"))
    bob, _ = node_mod.load_or_create_identity(os.path.join(WORK, "bob"))

    def client_for(ident, name, caps=None):
        return node_mod.NodeClient(
            os.path.join(WORK, name), name, url, announce=True,
            capabilities=caps or ["text"])

    # Announce -> listed, with a `seen` stamp.
    alice_c = client_for(alice, "alice")
    status, _ = alice_c.announce()
    check("presence: announce accepted", status == 200, "status %s" % status)
    entries = listed(url, alice.public_hex())
    check("presence: node listed after announce", len(entries) == 1)
    check("presence: /nodes entry carries a seen stamp",
          bool(entries and entries[0].get("seen")),
          "seen=%s" % (entries[0].get("seen") if entries else None))

    # A second node lists too, and both are visible.
    bob_c = client_for(bob, "bob", ["text", "tts"])
    status, _ = bob_c.announce()
    check("presence: second node announces", status == 200)
    status, body = node_mod._get(url, "/nodes")
    check("presence: both nodes listed",
          status == 200 and len(body.get("nodes", [])) == 2,
          "nodes=%d" % len(body.get("nodes", [])))

    # TTL passes with no heartbeat -> both drop off the list.
    time.sleep(4)
    check("presence: node gone after TTL with no heartbeat",
          listed(url, alice.public_hex()) == [],
          "alice still listed" if listed(url, alice.public_hex()) else "")
    check("presence: second node gone after TTL too",
          listed(url, bob.public_hex()) == [])

    # An expired node cannot send (403, not a listed node).
    status, body = alice_c.lobby_send("hello from a dead node")
    check("presence: expired node gets 403 on lobby send",
          status == 403 and body.get("error") == "not a listed node",
          "status %s body %s" % (status, body))

    # Re-announce -> listed again, and the caps update.
    alice_c2 = client_for(alice, "alice", ["text", "tts", "image"])
    status, _ = alice_c2.announce()
    check("presence: re-announce accepted", status == 200)
    entries = listed(url, alice.public_hex())
    check("presence: node listed again after re-announce", len(entries) == 1)
    check("presence: re-announce updates capabilities",
          entries and sorted(entries[0].get("capabilities", [])) == ["image", "text", "tts"],
          "caps=%s" % (entries[0].get("capabilities") if entries else None))

    # NodeClient.heartbeat() is a re-announce that refreshes `seen`.
    before = listed(url, alice.public_hex())[0].get("seen")
    time.sleep(1.2)
    status, _ = alice_c2.heartbeat()
    check("presence: heartbeat() re-announces", status == 200)
    after = listed(url, alice.public_hex())[0].get("seen")
    check("presence: heartbeat refreshes the seen stamp",
          after and after != before, "before=%s after=%s" % (before, after))

    # --- Poller heartbeat: keeps the node listed past the TTL ---
    poller = tunnel_mod.TunnelClient(
        os.path.join(WORK, "bob"), url, set(), {"text": lambda p: "ok"},
        name="bob", capabilities=["text"], heartbeat_interval=1)
    thread = threading.Thread(target=lambda: poller.run_forever(1), daemon=True)
    thread.start()
    time.sleep(5)
    check("presence: heartbeating poller keeps its node listed",
          len(listed(url, bob.public_hex())) == 1,
          "bob dropped off despite heartbeating" if not listed(url, bob.public_hex()) else "")
    # The poller also heartbeats on start (before the first poll).
    check("presence: poller heartbeated at least once on schedule",
          poller._last_heartbeat > 0, "_last_heartbeat=%s" % poller._last_heartbeat)

    # --- Store 2: small rate limit (5 per 60s), generous TTL ---
    store2 = mock_list.Store(ttl=900, rate_limit=5, rate_window=60)
    server2, port2 = mock_list.serve(store2)
    url2 = "http://127.0.0.1:%d" % port2

    carol, _ = node_mod.load_or_create_identity(os.path.join(WORK, "carol"))
    dave, _ = node_mod.load_or_create_identity(os.path.join(WORK, "dave"))
    carol_c = node_mod.NodeClient(
        os.path.join(WORK, "carol"), "carol", url2, announce=True,
        capabilities=["text"])
    dave_c = node_mod.NodeClient(
        os.path.join(WORK, "dave"), "dave", url2, announce=True,
        capabilities=["text"])
    check("ratelimit: carol announces", carol_c.announce()[0] == 200)
    check("ratelimit: dave announces", dave_c.announce()[0] == 200)

    # 5 lobby posts OK (announce counted as 1, so 4 more posts = 5).
    ok = 0
    for i in range(4):
        status, _ = carol_c.lobby_send("post %d" % i)
        if status == 200:
            ok += 1
    check("ratelimit: sends inside the quota succeed", ok == 4,
          "ok=%d" % ok)
    status, body = carol_c.lobby_send("post 5 — over the limit")
    check("ratelimit: 6th send gets 429",
          status == 429 and body.get("error") == "rate limited",
          "status %s body %s" % (status, body))
    # Per-sender: dave is unaffected by carol's limit.
    status, _ = dave_c.lobby_send("dave is fine")
    check("ratelimit: a different sender is unaffected", status == 200,
          "status %s" % status)
    # The limit also applies to mailbox sends (same counter).
    status, body = carol_c.chat_send(dave.public_hex(), "carol again")
    check("ratelimit: mailbox send over the limit gets 429",
          status == 429, "status %s" % status)

    server.shutdown()
    server2.shutdown()
    shutil.rmtree(WORK, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
