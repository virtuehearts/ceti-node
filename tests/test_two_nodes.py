"""End-to-end two-node scenario test (see TEST-PLAN.md).

Two agents talking, in order, the way real agents would:
  mya-node (serving, this box) <-> laptop (requesting, Jero's laptop)
  + phone (sysop-chat leg) + stranger/ghost (negative gates).

Sequence: announce + discover -> sysop chat back-and-forth -> lobby
-> text job to mya-node's REAL LFM3 -> file transfer (TC) -> a cloud
answer shared as a 📦 (TC container) -> negative gates.

Runs against the in-memory mock list (same routes as the real
worker-node-list.js) — stdlib only, no Cloudflare, no KV, no cloud
call, no money. The ONE live service it touches is the local LFM3
on :11436, for the step that must prove a real model answers. If
the LFM3 is down that check fails loud (the node cannot serve text),
which is the correct signal — it is not faked with a stub.
"""

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

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "run-two-nodes")
PASS, FAIL = [], []

# A canned cloud answer, standing in for a real bolt-e4b reply. No key,
# no cloud call, no money in the test. On the real network the serving
# node writes its actual cloud answer into the same file and ships it
# the same way — the 📦 does not care where the text came from.
CLOUD_ANSWER = (
    "Cloud answer (bolt-e4b) — Who won the 2024 Nobel Prize in Chemistry?\n"
    "David Baker (American), Demis Hassabis (British), and John M. Jumper\n"
    "(American) won the 2024 Nobel Prize in Chemistry, for computational\n"
    "protein design and protein structure prediction.\n"
)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def serve_dir(path):
    handler = partial(SimpleHTTPRequestHandler, directory=path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def verify_signed_msg(msg):
    """Verify a signed mailbox/chat message (same rule as mock_list)."""
    sig = msg.get("signature", "")
    pubkey = msg.get("pubkey", "")
    unsigned = {k: v for k, v in msg.items()
                if k not in ("signature", "pubkey")}
    signed_form = dict(unsigned)
    signed_form["pubkey"] = pubkey
    return node_mod.verify_signature(pubkey, node_mod.canonical(signed_form), sig)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    # Small chunks for speed in this test only (real default stays 50MB).
    node_mod.CHUNK_SIZE = 256 * 1024

    mya, _ = node_mod.load_or_create_identity(os.path.join(WORK, "mya-node"))
    laptop, _ = node_mod.load_or_create_identity(os.path.join(WORK, "laptop"))
    phone, _ = node_mod.load_or_create_identity(os.path.join(WORK, "phone"))
    stranger, _ = node_mod.load_or_create_identity(os.path.join(WORK, "stranger"))
    ghost, _ = node_mod.load_or_create_identity(os.path.join(WORK, "ghost"))

    store = mock_list.Store()
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port

    def client_for(ident, name, caps=None):
        return node_mod.NodeClient(os.path.join(WORK, name), name, url,
                                   True, caps or ["text"], "")

    try:
        # --- 1. Announce + discover --------------------------------------
        mya_c = client_for(mya, "mya-node", ["text", "tts", "image"])
        laptop_c = client_for(laptop, "laptop", ["text"])
        phone_c = client_for(phone, "phone", ["text"])
        s, b = mya_c.announce()
        check("two-nodes: mya-node announces and is listed",
              s == 200 and b.get("status") == "listed", str(b))
        s, b = laptop_c.announce()
        check("two-nodes: laptop announces and is listed",
              s == 200 and b.get("status") == "listed")
        phone_c.announce()
        s, b = laptop_c.nodes()
        nodes = {n.get("pubkey"): n for n in b.get("nodes", [])}
        check("two-nodes: laptop sees mya-node with its claimed caps",
              mya.public_hex() in nodes
              and nodes[mya.public_hex()].get("capabilities") == ["text", "tts", "image"]
              and "verified" not in nodes[mya.public_hex()], str(nodes.get(mya.public_hex())))
        check("two-nodes: mya-node sees laptop with text cap",
              laptop.public_hex() in nodes
              and nodes[laptop.public_hex()].get("capabilities") == ["text"])

        # --- 2. Sysop-to-sysop chat, back and forth ----------------------
        s, b = laptop_c.chat_send(mya.public_hex(),
                                  "hello, can I use your text model?")
        check("chat: laptop -> mya-node queued", s == 200, str(b))
        s, b = mya_c.chat_poll()
        msgs = b.get("messages", [])
        check("chat: mya-node polls and sees laptop's message with its name",
              len(msgs) == 1 and msgs[0].get("text") == "hello, can I use your text model?"
              and msgs[0].get("from_name") == "laptop", str(msgs))
        s, b = mya_c.chat_send(laptop.public_hex(),
                               "yes, you're allowed — sending you the allow.")
        check("chat: mya-node -> laptop queued", s == 200)
        s, b = laptop_c.chat_poll()
        msgs = b.get("messages", [])
        check("chat: laptop polls and sees mya-node's reply with its name",
              len(msgs) == 1 and msgs[0].get("text", "").startswith("yes, you're allowed")
              and msgs[0].get("from_name") == "mya-node", str(msgs))
        s, b = laptop_c.chat_poll()
        check("chat: second poll is empty (queue cleared)",
              b.get("messages") == [])
        # phone reaches the sysop too; chat never mixes with the mailbox
        s, b = phone_c.chat_send(mya.public_hex(), "phone here, sysop?")
        check("chat: phone -> mya-node queued", s == 200)
        s, b = mya_c.chat_poll()
        msgs = b.get("messages", [])
        check("chat: mya-node sees phone's message",
              len(msgs) == 1 and msgs[0].get("from_name") == "phone", str(msgs))
        s, b = tunnel_mod.poll_mailbox(url, mya)
        check("chat: chatbox never lands in the job mailbox",
              b.get("messages") == [])

        # --- 3. Lobby -----------------------------------------------------
        s, b = mya_c.lobby_send("mya-node is up: text/tts/image, ask in sysop chat")
        check("lobby: mya-node posts", s == 200 and b.get("status") == "posted")
        s, b = laptop_c.lobby()
        msgs = b.get("messages", [])
        check("lobby: laptop reads mya-node's post",
              len(msgs) == 1 and msgs[0].get("name") == "mya-node"
              and "text/tts/image" in msgs[0].get("text", ""), str(msgs))
        ghost_c = client_for(ghost, "ghost", ["text"])
        s, b = ghost_c.lobby_send("i am not listed")
        check("lobby: unlisted node cannot post", s == 403)

        # --- 4. Use another node's text model ----------------------------
        # mya-node's poller: allow-list = {laptop} only; text handler is
        # the REAL LFM3 (tunnel.llama_text, :11436) — no stub here.
        status, body = tunnel_mod.send_job(
            url, laptop, mya.public_hex(), "text", {"text": "What is 2+2?"})
        check("text: laptop sends text job to mya-node",
              status == 200 and body.get("status") == "queued", str(body))
        job_id = body.get("id")
        poller = tunnel_mod.TunnelClient(
            os.path.join(WORK, "mya-node"), url, {laptop.public_hex()},
            {"text": lambda p: tunnel_mod.llama_text(p.get("text", ""))})
        processed, skipped = poller.run_once()
        check("text: mya-node poller processed the laptop job",
              processed == [job_id], "processed=%s skipped=%s" % (processed, skipped))
        status, body = tunnel_mod.poll_mailbox(url, laptop)
        replies = [m for m in body.get("messages", []) if m.get("type") == "reply"]
        answer = ""
        if replies:
            answer = replies[0].get("result", {}).get("text", "")
        check("text: laptop gets a real model answer back through the mailbox",
              status == 200 and len(replies) == 1
              and replies[0].get("in_reply_to") == job_id
              and isinstance(answer, str) and len(answer.strip()) > 0,
              "answer=%r" % (answer[:120],))
        check("text: the model actually answered the question (2+2 = 4)",
              "4" in answer, "answer=%r" % (answer[:200],))
        check("text: reply is signed by mya-node",
              bool(replies) and verify_signed_msg(replies[0])
              and replies[0].get("pubkey") == mya.public_hex())

        # --- 5. File transfer (TC containers) -----------------------------
        src = os.path.join(WORK, "model.bin")
        with open(src, "wb") as f:
            remaining = 600 * 1024
            while remaining:
                block = os.urandom(min(64 * 1024, remaining))
                f.write(block)
                remaining -= len(block)
        with open(src, "rb") as f:
            orig_sha = hashlib.sha256(f.read()).hexdigest()
        status, body = mya_c.upload("model.bin", os.path.getsize(src), orig_sha)
        check("transfer: mya-node offers the file on the list",
              status == 200 and body.get("status") == "offered", str(body))
        outbox = os.path.join(WORK, "outbox")
        manifest = node_mod.split_file(mya, src, outbox, "tc-two-nodes-1")
        check("transfer: split into 3 signed containers at 256KB",
              manifest["total_chunks"] == 3)
        http_server, http_port = serve_dir(outbox)
        base_url = "http://127.0.0.1:%d" % http_port
        ok, detail, _ = node_mod.fetch_transfer(
            url, base_url, set(), os.path.join(WORK, "fetched-refused"))
        check("transfer: non-allow-listed fetch refused",
              not ok and "not allowed" in detail, detail)
        out_dir = os.path.join(WORK, "fetched")
        ok, detail, _ = node_mod.fetch_transfer(
            url, base_url, {mya.public_hex()}, out_dir)
        check("transfer: allow-listed fetch verifies + reassembles", ok, detail)
        with open(os.path.join(out_dir, "model.bin"), "rb") as f:
            got_sha = hashlib.sha256(f.read()).hexdigest()
        check("transfer: reassembled file hash matches original",
              got_sha == orig_sha)
        entries = [json.loads(l) for l in open(os.path.join(out_dir, "transfer-ledger.jsonl"))]
        check("transfer: ledger has 3 accepted entries",
              len(entries) == 3 and all(e["decision"] == "accepted" for e in entries))
        http_server.shutdown()

        # --- 6. Cloud answer via a 📦 --------------------------------------
        # mya-node has a cloud answer (canned here — no key, no cloud
        # call; on the real network it writes its actual bolt-e4b
        # answer into the same file). It ships as a signed TC
        # container, not inside a mailbox JSON the proxy can truncate.
        cloud_file = os.path.join(WORK, "cloud-answer.txt")
        with open(cloud_file, "w") as f:
            f.write(CLOUD_ANSWER)
        with open(cloud_file, "rb") as f:
            cloud_sha = hashlib.sha256(f.read()).hexdigest()
        status, body = mya_c.upload("cloud-answer.txt",
                                    os.path.getsize(cloud_file), cloud_sha)
        check("package: mya-node offers the cloud-answer file",
              status == 200 and body.get("status") == "offered", str(body))
        pkg_outbox = os.path.join(WORK, "pkg-outbox")
        pkg_manifest = node_mod.split_file(mya, cloud_file, pkg_outbox,
                                           "pkg-cloud-answer-1")
        check("package: cloud answer split into a signed 📦 (1 chunk)",
              pkg_manifest["total_chunks"] == 1
              and node_mod.verify_transfer_manifest(pkg_manifest),
              str(pkg_manifest.get("total_chunks")))
        p_server, p_port = serve_dir(pkg_outbox)
        pkg_dir = os.path.join(WORK, "pkg-fetched")
        ok, detail, got_manifest = node_mod.fetch_transfer(
            url, "http://127.0.0.1:%d" % p_port, {mya.public_hex()}, pkg_dir)
        check("package: laptop fetches the 📦 and verifies it", ok, detail)
        with open(os.path.join(pkg_dir, "cloud-answer.txt")) as f:
            got_answer = f.read()
        check("package: the cloud answer arrives intact, as a file",
              got_answer == CLOUD_ANSWER, repr(got_answer[:80]))
        check("package: fetched manifest is signed by mya-node",
              got_manifest.get("sender_pubkey") == mya.public_hex()
              and node_mod.verify_transfer_manifest(got_manifest))
        p_server.shutdown()

        # --- 7. Negative gates ---------------------------------------------
        status, body = tunnel_mod.send_job(
            url, ghost, mya.public_hex(), "text", {"text": "ghost job"})
        check("gates: unlisted sender job rejected at mailbox",
              status == 403, str(body))
        bad = node_mod.signed_payload(laptop, {
            "type": "job", "id": "bad1", "to_pubkey": mya.public_hex(),
            "capability": "text", "payload": {"text": "x"}})
        bad["signature"] = "00" * 70
        req = urllib.request.Request(url + "/mailbox/send", method="POST",
                                     data=json.dumps(bad).encode())
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                bad_status = r.status
        except urllib.error.HTTPError as e:
            bad_status = e.code
        check("gates: forged-signature job rejected", bad_status == 401)
        # stranger is announced but NOT allow-listed: queued, then skipped
        node_mod.NodeClient(os.path.join(WORK, "stranger"), "stranger",
                            url).announce()
        status, body = tunnel_mod.send_job(
            url, stranger, mya.public_hex(), "text", {"text": "from stranger"})
        stranger_job = body.get("id")
        processed, skipped = poller.run_once()
        check("gates: poller skips non-allow-listed stranger silently",
              stranger_job in skipped and stranger_job not in processed,
              "processed=%s skipped=%s" % (processed, skipped))
        status, body = tunnel_mod.poll_mailbox(url, stranger)
        check("gates: stranger gets no reply",
              status == 200 and body.get("messages") == [])
        # tampered 📦 chunk -> refused, no reassembly
        tamper_dir = os.path.join(WORK, "tampered-outbox")
        shutil.copytree(pkg_outbox, tamper_dir)
        with open(os.path.join(tamper_dir, "chunk-000000.tc"), "rb") as f:
            data = bytearray(f.read())
        data[-1] ^= 0xFF
        with open(os.path.join(tamper_dir, "chunk-000000.tc"), "wb") as f:
            f.write(bytes(data))
        t_server, t_port = serve_dir(tamper_dir)
        ok, detail, _ = node_mod.fetch_transfer(
            url, "http://127.0.0.1:%d" % t_port, {mya.public_hex()},
            os.path.join(WORK, "pkg-fetched-tampered"))
        check("gates: tampered 📦 chunk refused, no reassembly",
              not ok and not os.path.exists(
                  os.path.join(WORK, "pkg-fetched-tampered", "cloud-answer.txt")),
              detail)
        t_server.shutdown()
    finally:
        server.shutdown()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
