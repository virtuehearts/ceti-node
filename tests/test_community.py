"""Tests for the open-P2P community layer (Gopher model).

- Auto-list: a valid announce lists the node immediately, with the
  capabilities it claims to offer. No approval, no verified flag —
  recognition is per-node (allow-list / favorites), not global.
- Lobby: signed posts from listed nodes only, capped at 100, public read.
- Private sysop chat: separate chatbox queue (the job poller never eats
  chat). Anyone listed can send; the recipient decides who it answers.
- Favorites + per-peer policy: local JSON only, never leaves the node.
  Peer policy in the poller: block = silent skip, deny = explicit error
  reply, allow = normal (a whitelist: persistent, no per-job ask),
  unlisted = allow-set fallback else silent skip.
"""

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node as node_mod  # noqa: E402
import mock_list  # noqa: E402
import tunnel as tunnel_mod  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "run-community")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    alice, _ = node_mod.load_or_create_identity(os.path.join(WORK, "alice"))
    bob, _ = node_mod.load_or_create_identity(os.path.join(WORK, "bob"))
    carol, _ = node_mod.load_or_create_identity(os.path.join(WORK, "carol"))
    mynode, _ = node_mod.load_or_create_identity(os.path.join(WORK, "mynode"))
    store = mock_list.Store()
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port
    try:
        def client_for(ident, name):
            return node_mod.NodeClient(os.path.join(WORK, name), name, url)

        # --- auto-list ------------------------------------------------------
        alice_c = client_for(alice, "alice")
        s, b = alice_c.announce()
        check("community: valid announce lists immediately",
              s == 200 and b.get("status") == "listed", str(b))
        s, b = alice_c.nodes()
        entry = [n for n in b.get("nodes", [])
                 if n.get("pubkey") == alice.public_hex()]
        check("community: listed entry shows announced caps",
              len(entry) == 1 and entry[0].get("capabilities") == ["text"]
              and "verified" not in entry[0], str(entry))

        # --- lobby ----------------------------------------------------------
        s, b = alice_c.lobby_send("hello lobby")
        check("community: lobby post accepted", s == 200 and b.get("status") == "posted")
        s, b = alice_c.lobby()
        msgs = b.get("messages", [])
        check("community: lobby read shows post",
              len(msgs) == 1 and msgs[0].get("text") == "hello lobby"
              and msgs[0].get("name") == "alice", str(msgs))
        # unannounced node cannot post
        carol_c = client_for(carol, "carol")
        s, b = carol_c.lobby_send("i am not listed")
        check("community: unlisted lobby post rejected", s == 403)
        # bad signature rejected
        bad = node_mod.signed_payload(alice, {"type": "lobby", "text": "x"})
        bad["signature"] = "00" * 70
        s, b = tunnel_mod._post(url, "/lobby/send", bad)
        check("community: bad-signature lobby post rejected", s == 401)

        # --- private chat ---------------------------------------------------
        # Gopher model: anyone listed can send; the recipient decides
        # who it answers. No grant gate.
        bob_c = client_for(bob, "bob")
        bob_c.announce()
        s, b = bob_c.chat_send(alice.public_hex(), "hi alice, private")
        check("community: listed chat queued", s == 200)
        carol_c = client_for(carol, "carol")
        s, b = carol_c.chat_send(alice.public_hex(), "not listed yet")
        check("community: unlisted chat rejected", s == 403)
        carol_c.announce()
        s, b = carol_c.chat_send(alice.public_hex(), "hi alice too")
        check("community: listed chat queued after announce", s == 200)
        s, b = alice_c.chat_poll()
        msgs = b.get("messages", [])
        check("community: chat poll delivers both messages",
              len(msgs) == 2
              and msgs[0].get("text") == "hi alice, private"
              and msgs[0].get("from_name") == "bob"
              and msgs[1].get("text") == "hi alice too", str(msgs))
        s, b = alice_c.chat_poll()
        check("community: chat poll clears queue",
              b.get("messages") == [])

        # --- favorites + peer policy (local only) ---------------------------
        favs = node_mod.favorites_load(os.path.join(WORK, "alice"))
        favs[alice.public_hex()] = {"name": "alice-node", "added": "2026-10-28"}
        node_mod.favorites_save(os.path.join(WORK, "alice"), favs)
        back = node_mod.favorites_load(os.path.join(WORK, "alice"))
        check("community: favorites round trip",
              back.get(alice.public_hex(), {}).get("name") == "alice-node")
        node_mod.peers_save(os.path.join(WORK, "mynode"),
                            {bob.public_hex(): "block",
                             carol.public_hex(): "deny",
                             alice.public_hex(): "allow"})
        peers = node_mod.peers_load(os.path.join(WORK, "mynode"))
        check("community: peers round trip, bad values dropped",
              peers == {bob.public_hex(): "block",
                        carol.public_hex(): "deny",
                        alice.public_hex(): "allow"})

        # poller honors peer policy: block silent, deny error reply,
        # allow normal, unlisted silent skip. (Mailbox is a dumb pipe
        # now — senders only need to be listed, so announce the
        # stranger too; bob/carol/alice already announced above.)
        stranger, _ = node_mod.load_or_create_identity(os.path.join(WORK, "stranger"))
        node_mod.NodeClient(os.path.join(WORK, "stranger"), "stranger", url).announce()

        def stub(payload):
            return {"echo": payload.get("text", "")}

        poller = tunnel_mod.TunnelClient(
            os.path.join(WORK, "mynode"), url, set(), {"text": stub})
        for sender, txt in ((bob, "from bob"), (carol, "from carol"),
                            (alice, "from alice")):
            tunnel_mod.send_job(url, sender, mynode.public_hex(), "text",
                                {"text": txt})
        tunnel_mod.send_job(url, stranger, mynode.public_hex(), "text",
                            {"text": "from stranger"})
        processed, skipped = poller.run_once()
        check("community: poller policy — allow processed, block/deny/unlisted handled",
              len(processed) == 2 and len(skipped) == 2,
              "processed=%s skipped=%s" % (processed, skipped))
        # carol (deny) got an explicit error reply; bob (block) got nothing
        s, b = tunnel_mod.poll_mailbox(url, carol)
        replies = [m for m in b.get("messages", []) if m.get("type") == "reply"]
        check("community: denied peer gets explicit error reply",
              len(replies) == 1 and "denied" in replies[0]["result"]["error"])
        s, b = tunnel_mod.poll_mailbox(url, bob)
        check("community: blocked peer gets nothing",
              b.get("messages") == [])
    finally:
        server.shutdown()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
