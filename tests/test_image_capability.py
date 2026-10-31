"""Tests for the image capability (Gopher model).

The mailbox is a dumb pipe: a listed sender's job queues, and the
receiving node's own allow/deny/block policy decides what runs.
No central grant gate.

Image capability: an image job appends seed|name|WxH|prompt to the node's
Z-Image queue; when the render lands in test_output/<name>.png, the
poller replies with the PNG base64 (small) or a signed TC manifest
(large, fetched through the normal offer -> fetch chain).
"""

import base64
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
WORK = os.path.join(BASE, "run-image")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def post(url, path, payload):
    req = urllib.request.Request(url + path, method="POST",
                                 data=json.dumps(payload).encode())
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    phone, _ = node_mod.load_or_create_identity(os.path.join(WORK, "phone"))
    laptop, _ = node_mod.load_or_create_identity(os.path.join(WORK, "laptop"))
    stranger, _ = node_mod.load_or_create_identity(os.path.join(WORK, "stranger"))
    mynode, _ = node_mod.load_or_create_identity(os.path.join(WORK, "mynode"))

    store = mock_list.Store()
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port
    try:
        # Gopher model: senders must be listed (announced) to queue;
        # the receiving node's own policy decides what runs.
        for ident, name in ((phone, "phone"), (laptop, "laptop"),
                            (stranger, "stranger")):
            node_mod.NodeClient(os.path.join(WORK, name), name, url).announce()

        # mailbox gates: listed senders queue (dumb pipe); unlisted 403
        s, b = tunnel_mod.send_job(url, phone, mynode.public_hex(), "image",
                                   {"prompt": ""})
        check("gates: listed job queued", s == 200 and b.get("status") == "queued")
        phone_job = b.get("id")
        s, b = tunnel_mod.send_job(url, laptop, mynode.public_hex(), "image",
                                   {"prompt": "x"})
        check("gates: listed non-allow job queued (poller decides)", s == 200)
        laptop_job = b.get("id")
        s, b = tunnel_mod.send_job(url, stranger, mynode.public_hex(), "image",
                                   {"prompt": "x"})
        check("gates: stranger job queued (poller decides)", s == 200)
        stranger_job = b.get("id")
        ghost, _ = node_mod.load_or_create_identity(os.path.join(WORK, "ghost"))
        s, b = tunnel_mod.send_job(url, ghost, mynode.public_hex(), "image",
                                   {"prompt": "x"})
        check("gates: unlisted job rejected", s == 403)

        # poller: image queue + outdir, allow-list = {phone}
        qpath = os.path.join(WORK, "queue.txt")
        outdir = os.path.join(WORK, "test_output")
        os.makedirs(outdir)
        open(qpath, "w").close()
        poller = tunnel_mod.TunnelClient(
            os.path.join(WORK, "mynode"), url, {phone.public_hex()}, {},
            image_queue=qpath, image_outdir=outdir)
        # the earlier listed phone job (empty prompt) is handled:
        # queue_image_job raises -> error reply, still processed, and
        # no queue line or ledger entry is created for it; the
        # non-allow jobs are skipped silently by the poller
        processed, skipped = poller.run_once()
        check("gates: poller processed phone job, skipped laptop + stranger",
              processed == [phone_job]
              and skipped == [laptop_job, stranger_job],
              str((processed, skipped)))
        tunnel_mod.poll_mailbox(url, phone)  # drain the error reply

        # real image job: prompt sanitization + queue line
        s, b = tunnel_mod.send_job(
            url, phone, mynode.public_hex(), "image",
            {"prompt": "a test|with pipe\nnewline", "width": 256, "height": 256,
             "seed": 42, "name": "test-img"})
        img_job = b.get("id")
        processed, _ = poller.run_once()
        with open(qpath) as f:
            line = f.read().strip()
        check("image: queue line sanitized + exact",
              processed == [img_job]
              and line == "42|test-img|256x256|a test/with pipe newline", line)
        s, b = tunnel_mod.poll_mailbox(url, phone)
        replies = [m for m in b.get("messages", []) if m.get("type") == "reply"]
        check("image: phone gets queued reply",
              len(replies) == 1 and replies[0].get("result", {}).get("job") == "test-img",
              str(replies))

        # sweep: finished render -> base64 mailbox reply
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
        with open(os.path.join(outdir, "test-img.png"), "wb") as f:
            f.write(png)
        poller.run_once()
        check("image: sweep delivered via mailbox",
              poller.last_deliveries == [(img_job, "mailbox")],
              str(poller.last_deliveries))
        s, b = tunnel_mod.poll_mailbox(url, phone)
        replies = [m for m in b.get("messages", []) if m.get("type") == "reply"]
        got = base64.b64decode(replies[0]["result"]["image_b64"]) if replies else b""
        check("image: reply PNG bytes match render",
              len(replies) == 1 and got == png
              and replies[0]["result"]["content_type"] == "image/png")

        # TC fallback: force mailbox-max tiny, then full offer->accept->fetch
        tunnel_mod.IMAGE_MAILBOX_MAX = 50
        s, b = tunnel_mod.send_job(
            url, phone, mynode.public_hex(), "image",
            {"prompt": "big one", "seed": 7, "name": "big-img"})
        big_job = b.get("id")
        poller.run_once()
        big_png = b"\x89PNG\r\n\x1a\n" + b"y" * 100
        with open(os.path.join(outdir, "big-img.png"), "wb") as f:
            f.write(big_png)
        poller.run_once()
        check("image: large render goes via TC",
              poller.last_deliveries == [(big_job, "tc")],
              str(poller.last_deliveries))
        s, b = tunnel_mod.poll_mailbox(url, phone)
        replies = [m for m in b.get("messages", []) if m.get("type") == "reply"]
        tc_replies = [m for m in replies if "tc_manifest" in m.get("result", {})]
        res = tc_replies[0]["result"] if tc_replies else {}
        manifest = res.get("tc_manifest", {})
        check("image: TC manifest verifies",
              res.get("tc_transfer") == "img-" + big_job
              and node_mod.verify_transfer_manifest(manifest), str(res)[:200])
        # offer -> fetch round trip (no central accept step)
        offer = node_mod.signed_payload(
            mynode, {"file_sha256": manifest["file_sha256"],
                     "filename": manifest["filename"]})
        s, b = post(url, "/upload", offer)
        check("image: TC offer listed", s == 200 and b.get("status") == "offered")
        outbox = os.path.join(WORK, "mynode", "outbox", "img-" + big_job)
        handler = partial(SimpleHTTPRequestHandler, directory=outbox)
        http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % http.server_address[1]
        fetchdir = os.path.join(WORK, "fetched")
        ok, detail, _ = node_mod.fetch_transfer(
            url, base, {mynode.public_hex()}, fetchdir)
        with open(os.path.join(fetchdir, "big-img.png"), "rb") as f:
            fetched = f.read()
        check("image: TC fetch reassembles exact bytes",
              ok and fetched == big_png, detail)
        http.shutdown()
    finally:
        server.shutdown()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
