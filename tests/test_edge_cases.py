"""Round-2 edge-case tests (see TEST-PLAN.md, "what this does NOT test"
for the manual-only items). The failure modes the first suite did not
cover:

- Mailbox: reply bounce (an awaiter must re-queue a job it meets, not
  destroy it), duplicate-job dedup, a raising handler (poller replies
  with an error and keeps going), unknown capability (skipped, no crash).
- Lobby/chat limits: the 100-message cap, 2000-char truncation, empty
  post/message rejected.
- Announce: re-announce updates capabilities; bad signature rejected.
- Real TTS through the mailbox (the bridge, not a stub).
- Transfer edges: a large multi-chunk 📦, fetch without a listed offer,
  a missing chunk, a manifest that lies about the file hash, a dead
  outbox (clean refusal, not a crash).
- Crypto: a node.py signature verifies under the Hub's hub/package.py
  and vice versa (the byte-compatibility claim in node.py's docstring).
"""

import base64
import hashlib
import json
import os
import shutil
import sys
import threading
import urllib.request
import urllib.error
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node as node_mod  # noqa: E402
import mock_list  # noqa: E402
import tunnel as tunnel_mod  # noqa: E402

sys.path.insert(0, os.path.expanduser("~/workspace/uncensored-node"))
from hub import package as hub_pkg  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "run-edge")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def serve_dir(path):
    handler = partial(SimpleHTTPRequestHandler, directory=path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    node_mod.CHUNK_SIZE = 256 * 1024

    mya, _ = node_mod.load_or_create_identity(os.path.join(WORK, "mya-node"))
    laptop, _ = node_mod.load_or_create_identity(os.path.join(WORK, "laptop"))
    phone, _ = node_mod.load_or_create_identity(os.path.join(WORK, "phone"))

    store = mock_list.Store()
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port

    def client_for(ident, name, caps=None):
        return node_mod.NodeClient(os.path.join(WORK, name), name, url,
                                   True, caps or ["text"], "")

    try:
        mya_c = client_for(mya, "mya-node", ["text", "tts", "image"])
        laptop_c = client_for(laptop, "laptop", ["text"])
        phone_c = client_for(phone, "phone", ["text"])
        mya_c.announce()
        laptop_c.announce()
        phone_c.announce()

        # --- mailbox: reply bounce ----------------------------------------
        # A job TO the laptop sits in the laptop's mailbox. The laptop's
        # reply-awaiting side polls, meets the job, and must bounce it
        # back (re-queue) instead of destroying it — otherwise the
        # laptop's own poller would never see it.
        status, body = tunnel_mod.send_job(
            url, phone, laptop.public_hex(), "text", {"text": "job for laptop"})
        check("bounce: phone sends a job to the laptop",
              status == 200 and body.get("status") == "queued", str(body))
        bounced_id = body.get("id")
        status, body = tunnel_mod.poll_mailbox(url, laptop)
        jobs = [m for m in body.get("messages", []) if m.get("type") == "job"]
        check("bounce: laptop's awaiter meets the job in its mailbox",
              len(jobs) == 1 and jobs[0].get("id") == bounced_id)
        st, _ = tunnel_mod.requeue_message(url, jobs[0])
        check("bounce: awaiter re-queues the job verbatim", st == 200)
        # Verify the bounce landed WITHOUT polling (a poll is
        # fetch-and-clear and would consume the job before the
        # poller sees it): read the mock store directly.
        back = store.mailbox.get(laptop.public_hex(), [])
        check("bounce: the job is back in the laptop's mailbox (not destroyed)",
              len(back) == 1 and back[0].get("id") == bounced_id,
              str([m.get("id") for m in back]))
        # Now the laptop's own poller processes it and replies to the phone.
        laptop_poller = tunnel_mod.TunnelClient(
            os.path.join(WORK, "laptop"), url, {phone.public_hex()},
            {"text": lambda p: {"text": "laptop answers: " + p.get("text", "")}})
        processed, skipped = laptop_poller.run_once()
        check("bounce: laptop poller then processes the bounced job",
              processed == [bounced_id], str((processed, skipped)))
        status, body = tunnel_mod.poll_mailbox(url, phone)
        replies = [m for m in body.get("messages", []) if m.get("type") == "reply"]
        check("bounce: phone gets the reply",
              len(replies) == 1 and replies[0].get("in_reply_to") == bounced_id
              and replies[0].get("result", {}).get("text", "").startswith("laptop answers:"),
              str(replies))

        # --- mailbox: duplicate-job dedup ---------------------------------
        # The same signed job posted twice (a retry after a dropped
        # response). The poller must answer it once, not twice.
        dup_job = node_mod.signed_payload(laptop, {
            "type": "job", "id": "dup-001", "to_pubkey": mya.public_hex(),
            "capability": "text", "payload": {"text": "dup?"}})
        for _ in range(2):
            st, _ = tunnel_mod._post(url, "/mailbox/send", dup_job)
        check("dedup: same job posted twice, both queued", st == 200)
        poller = tunnel_mod.TunnelClient(
            os.path.join(WORK, "mya-node"), url, {laptop.public_hex()},
            {"text": lambda p: {"text": "once"}})
        processed, skipped = poller.run_once()
        check("dedup: poller processes the job exactly once",
              processed == ["dup-001"], str((processed, skipped)))
        status, body = tunnel_mod.poll_mailbox(url, laptop)
        replies = [m for m in body.get("messages", [])
                   if m.get("type") == "reply" and m.get("in_reply_to") == "dup-001"]
        check("dedup: requester gets exactly one reply",
              len(replies) == 1, str(len(replies)))

        # --- mailbox: raising handler + unknown capability ----------------
        calls = {"n": 0}

        def flaky(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return {"text": "ok"}

        for txt in ("first (raises)", "second (works)"):
            tunnel_mod.send_job(url, laptop, mya.public_hex(), "text", {"text": txt})
        status, body = tunnel_mod.send_job(
            url, laptop, mya.public_hex(), "teleport", {"text": "beam me up"})
        teleport_id = body.get("id")
        poller2 = tunnel_mod.TunnelClient(
            os.path.join(WORK, "mya-node"), url, {laptop.public_hex()},
            {"text": flaky})
        processed, skipped = poller2.run_once()
        check("handler: poller survives a raising handler and keeps going",
              len(processed) == 3 and teleport_id not in skipped,
              "processed=%s skipped=%s" % (processed, skipped))
        status, body = tunnel_mod.poll_mailbox(url, laptop)
        replies = {m.get("in_reply_to"): m for m in body.get("messages", [])
                   if m.get("type") == "reply"}
        first = replies.get(processed[0], {}).get("result", {})
        second = replies.get(processed[1], {}).get("result", {})
        check("handler: first job gets an error reply, second gets a good one",
              "boom" in str(first.get("error", "")) and second.get("text") == "ok",
              str((first, second)))
        # Unknown capability is fail-loud, not a silent skip: the
        # sender's UI needs to show why there is no answer. (The
        # Hub tree changed this 2026-11-07; the silent skip made
        # voice jobs "never show" with no error anywhere.)
        check("handler: unknown capability gets an error reply, not silence",
              "not offered" in str(replies.get(teleport_id, {}).get("result", {}).get("error", "")),
              str(replies.get(teleport_id)))

        # --- lobby / chat limits ------------------------------------------
        for i in range(1, 106):
            mya_c.lobby_send("msg %d" % i)
        s, b = mya_c.lobby()
        msgs = b.get("messages", [])
        check("lobby: capped at the last 100 messages",
              len(msgs) == 100 and msgs[0].get("text") == "msg 6"
              and msgs[-1].get("text") == "msg 105",
              "len=%d first=%s" % (len(msgs), msgs[0].get("text") if msgs else None))
        s, b = mya_c.lobby_send("x" * 2500)
        check("lobby: long post accepted", s == 200)
        s, b = mya_c.lobby()
        msgs = b.get("messages", [])
        check("lobby: long post truncated to 2000 chars",
              len(msgs[-1].get("text", "")) == 2000)
        s, b = mya_c.lobby_send("")
        check("lobby: empty post rejected", s == 400, str(b))
        s, b = laptop_c.chat_send(mya.public_hex(), "y" * 2500)
        check("chat: long message accepted", s == 200)
        s, b = mya_c.chat_poll()
        msgs = b.get("messages", [])
        check("chat: long message truncated to 2000 chars",
              len(msgs) == 1 and len(msgs[0].get("text", "")) == 2000)
        s, b = laptop_c.chat_send(mya.public_hex(), "")
        check("chat: empty message rejected", s == 400, str(b))

        # --- announce ------------------------------------------------------
        s, b = laptop_c.announce()
        check("announce: re-announce still lists", s == 200)
        laptop_c2 = client_for(laptop, "laptop", ["text", "tts"])
        s, b = laptop_c2.announce()
        s, b = laptop_c.nodes()
        nodes = {n.get("pubkey"): n for n in b.get("nodes", [])}
        check("announce: re-announce updates capabilities",
              nodes.get(laptop.public_hex(), {}).get("capabilities") == ["text", "tts"],
              str(nodes.get(laptop.public_hex())))
        bad = node_mod.signed_payload(laptop, {
            "type": "announce", "node_id": laptop.node_id,
            "fingerprint": laptop.fingerprint, "name": "laptop",
            "capabilities": ["text"], "endpoint": ""})
        bad["signature"] = "00" * 70
        st, _ = tunnel_mod._post(url, "/announce", bad)
        check("announce: bad-signature announce rejected", st == 401)

        # --- real TTS through the mailbox ----------------------------------
        status, body = tunnel_mod.send_job(
            url, laptop, mya.public_hex(), "tts",
            {"text": "Hello from the edge-case test.", "voice": "mya-03"})
        check("tts: laptop sends a tts job to mya-node",
              status == 200 and body.get("status") == "queued", str(body))
        tts_job = body.get("id")
        tts_poller = tunnel_mod.TunnelClient(
            os.path.join(WORK, "mya-node"), url, {laptop.public_hex()},
            {"tts": lambda p: tunnel_mod.bridge_tts(
                p.get("text", ""), p.get("voice", "mya-03"))})
        processed, skipped = tts_poller.run_once()
        check("tts: mya-node poller processed the tts job",
              processed == [tts_job], str((processed, skipped)))
        status, body = tunnel_mod.poll_mailbox(url, laptop)
        replies = [m for m in body.get("messages", [])
                   if m.get("type") == "reply" and m.get("in_reply_to") == tts_job]
        result = replies[0].get("result", {}) if replies else {}
        audio = b""
        try:
            audio = base64.b64decode(result.get("audio_b64", ""))
        except Exception:
            pass
        check("tts: laptop gets a real MP3 back through the mailbox",
              len(replies) == 1 and result.get("bytes", 0) > 1000
              and "mpeg" in result.get("content_type", "")
              and len(audio) == result.get("bytes", 0) and len(audio) > 1000,
              "bytes=%s ctype=%s" % (result.get("bytes"), result.get("content_type")))

        # --- transfer edges ------------------------------------------------
        # Large multi-chunk 📦 (a 600KB cloud answer, 3 chunks).
        big_answer = ("Cloud answer (bolt-e4b) — 2024 Nobel Chemistry: "
                      "Baker, Hassabis, Jumper.\n" * 8000)[:600 * 1024]
        big_file = os.path.join(WORK, "big-answer.txt")
        with open(big_file, "w") as f:
            f.write(big_answer)
        with open(big_file, "rb") as f:
            big_sha = hashlib.sha256(f.read()).hexdigest()
        mya_c.upload("big-answer.txt", os.path.getsize(big_file), big_sha)
        big_outbox = os.path.join(WORK, "big-outbox")
        big_manifest = node_mod.split_file(mya, big_file, big_outbox, "pkg-big-1")
        check("package: large cloud answer split into 3 chunks",
              big_manifest["total_chunks"] == 3)
        b_server, b_port = serve_dir(big_outbox)
        big_dir = os.path.join(WORK, "big-fetched")
        ok, detail, _ = node_mod.fetch_transfer(
            url, "http://127.0.0.1:%d" % b_port, {mya.public_hex()}, big_dir)
        check("package: large 📦 fetches and verifies", ok, detail)
        with open(os.path.join(big_dir, "big-answer.txt")) as f:
            got = f.read()
        check("package: large cloud answer arrives intact",
              got == big_answer)
        b_server.shutdown()

        # Fetch without a listed offer -> refused.
        src = os.path.join(WORK, "unoffered.bin")
        with open(src, "wb") as f:
            f.write(os.urandom(300 * 1024))
        un_outbox = os.path.join(WORK, "un-outbox")
        node_mod.split_file(mya, src, un_outbox, "tc-unoffered-1")
        u_server, u_port = serve_dir(un_outbox)
        ok, detail, _ = node_mod.fetch_transfer(
            url, "http://127.0.0.1:%d" % u_port, {mya.public_hex()},
            os.path.join(WORK, "un-fetched"))
        check("transfer: fetch without a listed offer refused",
              not ok and "not listed" in detail, detail)
        u_server.shutdown()

        # Missing chunk -> incomplete set, no reassembly (600KB = 3
        # chunks at 256KB; hand the receiver only 2 of 3).
        miss_src = os.path.join(WORK, "miss.bin")
        with open(miss_src, "wb") as f:
            f.write(os.urandom(600 * 1024))
        miss_out = os.path.join(WORK, "miss-out")
        miss_manifest = node_mod.split_file(mya, miss_src, miss_out, "tc-missing-1")
        miss_chunks = sorted(os.path.join(miss_out, p) for p in os.listdir(miss_out)
                             if p.endswith(".tc"))[:2]  # 2 of 3
        miss_ledger = os.path.join(WORK, "miss-ledger.jsonl")
        ok, detail = node_mod.receive_containers(
            miss_manifest, miss_chunks, {mya.public_hex()}, miss_ledger,
            os.path.join(WORK, "miss-reassembled.bin"))
        check("transfer: missing chunk -> incomplete set, no reassembly",
              not ok and "incomplete" in detail
              and not os.path.exists(os.path.join(WORK, "miss-reassembled.bin")),
              detail)

        # Manifest that lies about the file hash (re-signed so the
        # signature is valid — the lie must be caught by the hash,
        # not by the signature).
        lie_out = os.path.join(WORK, "lie-out")
        lie_manifest = node_mod.split_file(mya, src, lie_out, "tc-lie-1")
        lie_manifest["file_sha256"] = "0" * 64
        unsigned = {k: v for k, v in lie_manifest.items() if k != "signature"}
        lie_manifest["signature"] = node_mod.sign_payload(
            mya.secret, node_mod.canonical(unsigned))
        lie_chunks = sorted(os.path.join(lie_out, p) for p in os.listdir(lie_out)
                            if p.endswith(".tc"))
        lie_ledger = os.path.join(WORK, "lie-ledger.jsonl")
        ok, detail = node_mod.receive_containers(
            lie_manifest, lie_chunks, {mya.public_hex()}, lie_ledger,
            os.path.join(WORK, "lie-reassembled.bin"))
        check("transfer: lying manifest caught by the reassembled hash",
              not ok and "hash mismatch" in detail, detail)

        # Dead outbox -> clean refusal, not a crash (2026-11-07 fix in
        # fetch_transfer: the manifest fetch used to raise).
        try:
            ok, detail, man = node_mod.fetch_transfer(
                url, "http://127.0.0.1:9", {mya.public_hex()},
                os.path.join(WORK, "dead-fetched"))
            check("transfer: dead outbox is a clean refusal, not a crash",
                  not ok and "download failed" in detail and man == {},
                  detail)
        except Exception as e:
            check("transfer: dead outbox is a clean refusal, not a crash",
                  False, "raised %s: %s" % (type(e).__name__, e))

        # --- crypto cross-compat -------------------------------------------
        secret = bytes(range(1, 33))
        pub = node_mod.compressed_pubkey(secret).hex()
        payload = node_mod.canonical({"kind": "xcompat", "n": 7})
        check("crypto: node.py and hub/package.py canonical forms agree",
              payload == hub_pkg.canonical({"kind": "xcompat", "n": 7}))
        sig_node = node_mod.sign_payload(secret, payload)
        check("crypto: node.py signature verifies under hub/package.py",
              hub_pkg.verify_signature(pub, payload, sig_node))
        sig_hub = hub_pkg.sign_payload(secret, payload)
        check("crypto: hub/package.py signature verifies under node.py",
              node_mod.verify_signature(pub, payload, sig_hub))
    finally:
        server.shutdown()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
