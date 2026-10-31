"""Tests for the tunnel client (firewalled node serving via mailbox).

Flow under test: phone -> mailbox job (tts) -> poller (my node) runs a
local handler -> signed reply -> phone polls its mailbox and reads it.
Also: listed-only send gate, poller allow-list gate, bad-signature
rejection, and a live reachability check of the real PocketTTS bridge
(the service the tunnel will front) — stub handler in the tunnel run
itself, so the tunnel test never depends on bridge uptime.
"""

import json
import os
import shutil
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node as node_mod  # noqa: E402
import mock_list  # noqa: E402
import tunnel as tunnel_mod  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "run-tunnel")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    phone, _ = node_mod.load_or_create_identity(os.path.join(WORK, "phone"))
    laptop, _ = node_mod.load_or_create_identity(os.path.join(WORK, "laptop"))
    mynode, _ = node_mod.load_or_create_identity(os.path.join(WORK, "mynode"))
    stranger, _ = node_mod.load_or_create_identity(os.path.join(WORK, "stranger"))

    store = mock_list.Store()
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port
    try:
        # Gopher model: the mailbox is a dumb pipe — senders must be
        # LISTED (announced) to queue, and the receiving node's own
        # allow-list decides what runs. Announce all three senders.
        for ident, name in ((phone, "phone"), (laptop, "laptop"),
                            (stranger, "stranger")):
            c = node_mod.NodeClient(os.path.join(WORK, name), name, url)
            c.announce()
        # phone -> mynode tts job: listed sender, queued
        status, body = tunnel_mod.send_job(
            url, phone, mynode.public_hex(), "tts", {"text": "Hello from the phone"})
        check("tunnel: announced tts job queued",
              status == 200 and body.get("status") == "queued", str(body))
        job_id = body.get("id")

        # laptop (listed, but NOT on mynode's allow-list) -> queued,
        # but the poller must skip it
        status, body = tunnel_mod.send_job(
            url, laptop, mynode.public_hex(), "tts", {"text": "laptop"})
        check("tunnel: listed non-allow job queued (poller decides)",
              status == 200, str(body))
        laptop_job = body.get("id")

        # stranger (listed, NOT on mynode's allow-list) -> queued,
        # but the poller must skip it
        status, body = tunnel_mod.send_job(
            url, stranger, mynode.public_hex(), "tts", {"text": "stranger"})
        check("tunnel: stranger job queued (poller decides)", status == 200)
        stranger_job = body.get("id")

        # ghost (never announced) -> rejected at the mailbox
        ghost, _ = node_mod.load_or_create_identity(os.path.join(WORK, "ghost"))
        status, body = tunnel_mod.send_job(
            url, ghost, mynode.public_hex(), "tts", {"text": "ghost"})
        check("tunnel: unlisted sender job rejected at mailbox",
              status == 403, str(body))

        # poller: allow-list = {phone} only; stub tts handler
        def stub_tts(payload):
            return {"spoken": payload.get("text", ""), "voice": "stub"}

        poller = tunnel_mod.TunnelClient(
            os.path.join(WORK, "mynode"), url, {phone.public_hex()},
            {"tts": stub_tts})
        processed, skipped = poller.run_once()
        check("tunnel: poller processed phone job, skipped laptop + stranger",
              processed == [job_id]
              and skipped == [laptop_job, stranger_job],
              "processed=%s skipped=%s" % (processed, skipped))

        # phone polls its mailbox and gets the reply
        status, body = tunnel_mod.poll_mailbox(url, phone)
        replies = [m for m in body.get("messages", []) if m.get("type") == "reply"]
        check("tunnel: phone receives signed reply to its job",
              status == 200 and len(replies) == 1
              and replies[0].get("in_reply_to") == job_id
              and replies[0].get("result", {}).get("spoken") == "Hello from the phone",
              str(replies))

        # stranger gets no reply (its job was skipped, never executed)
        status, body = tunnel_mod.poll_mailbox(url, stranger)
        check("tunnel: stranger gets no reply", status == 200 and body.get("messages") == [])

        # Replies in the serving node's own mailbox must BOUNCE, not
        # die: a node both serves (this poller) and awaits replies
        # (its Jobs tab, a self-test). Fetch-and-clear would otherwise
        # let the poller eat replies meant for the awaiting side.
        status, body = tunnel_mod.reply_to(
            url, phone, mynode.public_hex(), "some-job", {"text": "reply text"})
        check("tunnel: reply queued to serving node", status == 200, str(body))
        processed, skipped = poller.run_once()
        check("tunnel: poller bounces a reply (neither processed nor skipped)",
              processed == [] and skipped == [], "processed=%s skipped=%s" % (processed, skipped))
        status, body = tunnel_mod.poll_mailbox(url, mynode)
        bounced = [m for m in body.get("messages", []) if m.get("type") == "reply"]
        check("tunnel: bounced reply still in mailbox for its awaiter",
              status == 200 and len(bounced) == 1
              and bounced[0].get("in_reply_to") == "some-job"
              and bounced[0].get("result", {}).get("text") == "reply text",
              str(bounced))
        # ...but the bounce is bounded (KV writes are quota'd): after
        # MAX_REQUEUES cycles an unfetched reply is dropped.
        status, body = tunnel_mod.reply_to(
            url, phone, mynode.public_hex(), "stale-job", {"text": "stale"})
        check("tunnel: stale reply queued", status == 200)
        for _ in range(tunnel_mod.MAX_REQUEUES + 1):
            poller.run_once()
        status, body = tunnel_mod.poll_mailbox(url, mynode)
        check("tunnel: stale reply dropped after MAX_REQUEUES bounces",
              status == 200 and body.get("messages") == [], str(body))

        # Dedup: identical copies of one job (retries after an
        # ambiguous post) are answered once, not once per copy.
        dup = node_mod.signed_payload(phone, {
            "type": "job", "id": "dup1", "to_pubkey": mynode.public_hex(),
            "capability": "tts", "payload": {"text": "dup"}})
        for _ in range(2):
            req = urllib.request.Request(url + "/mailbox/send", method="POST",
                                         data=json.dumps(dup).encode())
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as r:
                assert r.status == 200
        processed, skipped = poller.run_once()
        check("tunnel: duplicate job answered once",
              processed == ["dup1"] and skipped == [],
              "processed=%s skipped=%s" % (processed, skipped))
        bad = node_mod.signed_payload(phone, {
            "type": "job", "id": "bad1", "to_pubkey": mynode.public_hex(),
            "capability": "tts", "payload": {"text": "x"}})
        bad["signature"] = "00" * 70
        req = urllib.request.Request(url + "/mailbox/send", method="POST",
                                     data=json.dumps(bad).encode())
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                bad_status = r.status
        except urllib.error.HTTPError as e:
            bad_status = e.code
        check("tunnel: bad-signature job rejected", bad_status == 401)

        # live bridge reachability (the real service the tunnel fronts)
        # — MP3, the format the Pixel app plays
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8000/v1/audio/speech", method="POST",
                data=json.dumps({"input": "Tunnel test.", "voice": "mya-03",
                                 "response_format": "mp3"}).encode())
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=120) as r:
                audio = r.read()
                ctype = r.headers.get("Content-Type", "")
            check("bridge live: real MP3 synthesis returns audio",
                  len(audio) > 1000 and "mpeg" in ctype,
                  "%d bytes, %s" % (len(audio), ctype))
        except Exception as e:
            check("bridge live: real MP3 synthesis returns audio", False, str(e)[:200])

        # ---- 0.0.1i: failed job replies are held + retried, not lost ----
        work2 = os.path.join(WORK, "holder")
        os.makedirs(work2, exist_ok=True)
        holder, _ = node_mod.load_or_create_identity(work2)
        client = tunnel_mod.TunnelClient(work2, url, set(),
                                         {"text": lambda p: {"text": "hi"}})
        real_reply = tunnel_mod.reply_to

        def failing_reply(list_url, identity, to, jid, result):
            return 0, {"error": "proxy down"}
        tunnel_mod.reply_to = failing_reply
        try:
            rst = client._reply_or_hold(holder.public_hex(), "job-1", {"text": "hi"})
            check("i: failed reply is held, not lost",
                  rst == 0 and len(client._pending_replies) == 1,
                  str(client._pending_replies))
            client._reply_or_hold(holder.public_hex(), "job-1", {"text": "hi"})
            check("i: same job held only once", len(client._pending_replies) == 1)

            def ok_reply(list_url, identity, to, jid, result):
                return 200, {}
            tunnel_mod.reply_to = ok_reply
            client._flush_pending()
            check("i: held reply delivered on a later cycle",
                  client._pending_replies == [] and "job-1" in client._seen_jobs)

            tunnel_mod.reply_to = failing_reply
            client._pending_replies = [{"to": holder.public_hex(), "jid": "job-2",
                                        "result": {"text": "x"}, "tries": 29}]
            client._flush_pending()
            check("i: reply dropped after MAX_REPLY_TRIES",
                  client._pending_replies == [])
        finally:
            tunnel_mod.reply_to = real_reply
    finally:
        server.shutdown()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
