"""Tests for file transfer in node.py (TC containers over HTTP).

Covers: default 50MB chunk size, cross-compatibility with tc-prototype
containers in both directions, the full offer -> fetch
flow, the receiver allow-list gate, tamper rejection,
wrong-sender denial, and a CLI send/fetch smoke run.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node as node_mod  # noqa: E402
import mock_list  # noqa: E402

sys.path.insert(0, os.path.expanduser("~/workspace/tc-prototype"))
import tc as tc_mod  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
NODE_PY = os.path.join(ROOT, "node.py")
WORK = os.path.join(BASE, "run-transfer")
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

    check("transfer: default chunk size is exactly 50MB",
          node_mod.CHUNK_SIZE == 50 * 1024 * 1024)
    # Small chunks for speed in this test only (real default stays 50MB).
    node_mod.CHUNK_SIZE = 256 * 1024

    pixel, _ = node_mod.load_or_create_identity(os.path.join(WORK, "pixel"))
    desktop, _ = node_mod.load_or_create_identity(os.path.join(WORK, "desktop"))
    sysop, _ = node_mod.load_or_create_identity(os.path.join(WORK, "sysop"))

    # 600KB test file -> 3 chunks at 256KB.
    src = os.path.join(WORK, "model.bin")
    with open(src, "wb") as f:
        remaining = 600 * 1024
        while remaining:
            block = os.urandom(min(64 * 1024, remaining))
            f.write(block)
            remaining -= len(block)
    with open(src, "rb") as f:
        orig_sha = hashlib.sha256(f.read()).hexdigest()

    # --- cross-compat with tc-prototype -----------------------------------
    # tc.split_file expects a Node with .secret/.pubkey_hex/.fingerprint.
    class Adapter:
        def __init__(self, ident):
            self.secret = ident.secret
            self.pubkey_hex = ident.public_hex()
            self.fingerprint = ident.fingerprint

        def sign(self, payload):
            return tc_mod.sign_payload(self.secret, payload)
    tc_out = os.path.join(WORK, "tc-out")
    tc_manifest = tc_mod.split_file(Adapter(pixel), src, tc_out, "tc-xcompat-1")
    tc_chunks = sorted(os.path.join(tc_out, p) for p in os.listdir(tc_out)
                       if p.endswith(".tc"))
    ledger = os.path.join(WORK, "xcompat-ledger.jsonl")
    ok, detail = node_mod.receive_containers(
        tc_manifest, tc_chunks, {pixel.public_hex()}, ledger,
        os.path.join(WORK, "xcompat-reassembled.bin"))
    check("xcompat: tc.py containers verify under node.py receiver", ok, detail)

    node_out = os.path.join(WORK, "node-out")
    node_manifest = node_mod.split_file(pixel, src, node_out, "tc-xcompat-2")
    node_chunks = sorted(os.path.join(node_out, p) for p in os.listdir(node_out)
                         if p.endswith(".tc"))
    check("xcompat: node.py split gives 3 chunks at 256KB",
          len(node_chunks) == 3 and node_manifest["total_chunks"] == 3)
    ledger2 = os.path.join(WORK, "xcompat-ledger2.jsonl")
    ok, detail = tc_mod.receive(
        node_manifest, node_chunks, {pixel.public_hex()}, set(), ledger2,
        os.path.join(WORK, "xcompat-reassembled2.bin"))
    check("xcompat: node.py containers verify under tc.py receiver", ok, detail)

    # --- full flow over HTTP (no central accept step) ----------------------
    store = mock_list.Store()
    server, port = mock_list.serve(store)
    list_url = "http://127.0.0.1:%d" % port
    pixel_client = node_mod.NodeClient(os.path.join(WORK, "pixel"), "pixel-7",
                                       list_url, True, ["text"], "")
    try:
        status, body = pixel_client.upload("model.bin", os.path.getsize(src), orig_sha)
        check("flow: upload offer listed", status == 200 and body.get("status") == "offered")

        outbox = os.path.join(WORK, "outbox")
        manifest = node_mod.split_file(pixel, src, outbox, "tc-flow-1")
        http_server, http_port = serve_dir(outbox)
        base_url = "http://127.0.0.1:%d" % http_port
        out_dir = os.path.join(WORK, "fetched")
        # receiver allow-list is the gate now: non-allow-listed refused
        # (into its own dir, so its rejected ledger entries do not mix
        # with the successful fetch's ledger below)
        ok, detail, _ = node_mod.fetch_transfer(
            list_url, base_url, set(), os.path.join(WORK, "fetched-refused"))
        check("flow: fetch with non-allow-listed sender refused",
              not ok and "not allowed" in detail, detail)
        ok, detail, _ = node_mod.fetch_transfer(
            list_url, base_url, {pixel.public_hex()}, out_dir)
        check("flow: fetch with allow-listed sender verifies + reassembles", ok, detail)
        with open(os.path.join(out_dir, "model.bin"), "rb") as f:
            got_sha = hashlib.sha256(f.read()).hexdigest()
        check("flow: reassembled file hash matches original", got_sha == orig_sha)
        entries = [json.loads(l) for l in open(os.path.join(out_dir, "transfer-ledger.jsonl"))]
        check("flow: ledger has 3 accepted entries",
              len(entries) == 3 and all(e["decision"] == "accepted" for e in entries))

        # tampered chunk over HTTP -> refused, no reassembly
        with open(os.path.join(outbox, "chunk-000001.tc"), "rb") as f:
            data = bytearray(f.read())
        data[-1] ^= 0xFF
        tamper_dir = os.path.join(WORK, "tampered-outbox")
        shutil.copytree(outbox, tamper_dir)
        with open(os.path.join(tamper_dir, "chunk-000001.tc"), "wb") as f:
            f.write(bytes(data))
        t_server, t_port = serve_dir(tamper_dir)
        out_dir2 = os.path.join(WORK, "fetched-tampered")
        ok, detail, _ = node_mod.fetch_transfer(
            list_url, "http://127.0.0.1:%d" % t_port, {pixel.public_hex()}, out_dir2)
        check("flow: tampered chunk refused, no reassembly",
              not ok and not os.path.exists(os.path.join(out_dir2, "model.bin")), detail)
        t_server.shutdown()

        # wrong sender -> denied
        out_dir3 = os.path.join(WORK, "fetched-wrong")
        ok, detail, _ = node_mod.fetch_transfer(
            list_url, base_url, {desktop.public_hex()}, out_dir3)
        check("flow: wrong-sender fetch denied",
              not ok and "not allowed" in detail, detail)
        http_server.shutdown()

        # --- CLI smoke: send + fetch --------------------------------------
        cli_outbox = os.path.join(WORK, "cli-outbox")
        r = subprocess.run([sys.executable, NODE_PY,
                            "--data-dir", os.path.join(WORK, "pixel"),
                            "--name", "pixel-7",
                            "send", "--file", src, "--outbox", cli_outbox],
                           capture_output=True, text=True, timeout=120)
        # Note: the subprocess uses the real 50MB default, so the 600KB
        # file is 1 chunk there (the in-process tests use 256KB chunks).
        cli_manifest = {}
        if os.path.exists(os.path.join(cli_outbox, "manifest.json")):
            with open(os.path.join(cli_outbox, "manifest.json")) as f:
                cli_manifest = json.load(f)
        cli_chunks = [p for p in os.listdir(cli_outbox) if p.endswith(".tc")] \
            if os.path.exists(cli_outbox) else []
        check("cli: send writes manifest + chunks",
              r.returncode == 0
              and cli_manifest.get("total_chunks") == len(cli_chunks) >= 1,
              r.stderr[-200:] if r.returncode else "")
        # Offer the CLI-sent file, then fetch via CLI (no accept step).
        with open(src, "rb") as f:
            cli_sha = hashlib.sha256(f.read()).hexdigest()
        pixel_client.upload("model.bin", os.path.getsize(src), cli_sha)
        c_server, c_port = serve_dir(cli_outbox)
        cli_out = os.path.join(WORK, "cli-fetched")
        r = subprocess.run([sys.executable, NODE_PY,
                            "--data-dir", os.path.join(WORK, "desktop"),
                            "--name", "desk", "--list-url", list_url,
                            "fetch", "--base-url", "http://127.0.0.1:%d" % c_port,
                            "--out", cli_out,
                            "--allow-pubkey", pixel.public_hex()],
                           capture_output=True, text=True, timeout=120)
        with open(os.path.join(cli_out, "model.bin"), "rb") as f:
            cli_got = hashlib.sha256(f.read()).hexdigest()
        check("cli: fetch reassembles with matching hash",
              r.returncode == 0 and cli_got == orig_sha,
              r.stderr[-200:] if r.returncode else "")
        c_server.shutdown()
    finally:
        server.shutdown()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
