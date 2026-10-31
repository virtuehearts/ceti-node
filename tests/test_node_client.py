"""Tests for the portable node client + list flow.

Runs against the in-memory mock list server (no network, no Cloudflare).
Also proves crypto compatibility with the Hub: signatures cross-verify
between node.py and hub/package.py in both directions.
"""

import json
import os
import shutil
import stat
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node as node_mod  # noqa: E402
import mock_list  # noqa: E402

sys.path.insert(0, os.path.expanduser("~/workspace/uncensored-hub"))
from hub.package import sign_payload as hub_sign  # noqa: E402
from hub.package import verify_signature as hub_verify  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "run")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def post(url, payload):
    req = urllib.request.Request(url, method="POST",
                                 data=json.dumps(payload).encode())
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    # --- identities -------------------------------------------------------
    pixel, existed = node_mod.load_or_create_identity(os.path.join(WORK, "pixel"))
    check("identity: first run creates identity.dat", not existed)
    pixel2, existed2 = node_mod.load_or_create_identity(os.path.join(WORK, "pixel"))
    check("identity: reload gives same pubkey",
          existed2 and pixel2.public_hex() == pixel.public_hex(),
          pixel2.fingerprint)
    mode = stat.S_IMODE(os.stat(os.path.join(WORK, "pixel", "identity.dat")).st_mode)
    check("identity: identity.dat mode 0600", mode == 0o600, oct(mode))
    desktop, _ = node_mod.load_or_create_identity(os.path.join(WORK, "desktop"))

    # --- crypto cross-compat with the Hub ---------------------------------
    msg = b"hello from the pixel"
    sig_node = node_mod.sign_payload(pixel.secret, msg)
    check("crypto: node.py signature verifies under hub/package.py",
          hub_verify(pixel.public_hex(), msg, sig_node))
    sig_hub = hub_sign(pixel.secret, msg)
    check("crypto: hub/package.py signature verifies under node.py",
          node_mod.verify_signature(pixel.public_hex(), msg, sig_hub))

    # --- mock list server ---------------------------------------------------
    store = mock_list.Store()
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port
    try:
        # gossip compat with the current discovery worker
        status, body = post(url + "/gossip", {"hello": "world"})
        check("compat: POST /gossip echoes", status == 200 and body.get("echo") == {"hello": "world"})

        pixel_client = node_mod.NodeClient(os.path.join(WORK, "pixel"), "pixel-7",
                                           url, True, ["text"], "local-test")
        desk_client = node_mod.NodeClient(os.path.join(WORK, "desktop"), "desk",
                                          url, True, ["text"], "local-test")

        # announce yes -> listed immediately, with announced caps
        status, body = pixel_client.announce()
        check("announce: yes -> listed",
              status == 200 and body.get("status") == "listed")
        status, body = pixel_client.nodes()
        mine = [n for n in body["nodes"]
                if n["pubkey"] == pixel_client.identity.public_hex()]
        check("announce: listed node shows announced caps",
              status == 200 and len(mine) == 1
              and mine[0].get("capabilities") == ["text"]
              and mine[0].get("name") == "pixel-7", str(mine))

        # announce no -> no request at all
        quiet = node_mod.NodeClient(os.path.join(WORK, "quiet"), "quiet",
                                    url, False, ["text"], "")
        status, body = quiet.announce()
        check("announce: no -> skipped, nothing sent",
              status == "skipped" and body.get("skipped") is True
              and "quiet" not in json.dumps(store.pending_announces))

        # bad-signature announce rejected
        bad = node_mod.signed_payload(pixel, {
            "type": "announce", "node_id": "node-x", "fingerprint": "NODE-X",
            "name": "evil", "capabilities": ["text"], "endpoint": ""})
        bad["signature"] = "00" * 70
        status, _ = post(url + "/announce", bad)
        check("announce: bad signature rejected", status == 401)

        # uploads: offered to the public list at once; each receiver
        # decides its own downloads (no central accept step)
        status, body = pixel_client.upload("model.gguf", 2146495872, "deadbeef")
        check("upload: lands offered", status == 200 and body.get("status") == "offered")
        status, body = get(url + "/uploads")
        check("upload: offer listed immediately",
              status == 200 and len(body["uploads"]) == 1
              and body["uploads"][0]["filename"] == "model.gguf")
    finally:
        server.shutdown()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
