"""Tests for tunnel.py's "text" handler (added 2026-10-27, after the
Android v1 build, which shipped with only a tts handler).

The phone app sends {"text": ...} and expects {"text": ...} back.
Here the serving side is a real TunnelClient over the mock list,
with ollama_text pointed at a tiny fake Ollama server (so the test
never depends on a real Ollama process), plus a live check that
the real Ollama port is currently down — recorded as a check, not
a failure of the handler shape, because the handler is supposed
to fail loud when Ollama is down.
"""

import json
import os
import shutil
import sys
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import node as node_mod  # noqa: E402
import mock_list  # noqa: E402
import tunnel as tunnel_mod  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "run-text-handler")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


class FakeOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode() or "{}")
        assert body.get("model"), "model missing"
        assert body.get("messages"), "messages missing"
        out = json.dumps({"message": {"content": "hello back: " + body["messages"][-1]["content"]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class FakeLlama(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode() or "{}")
        FakeLlama.seen.append(body)
        content = "  " if "empty-test" in body.get("prompt", "") else "llama says hi"
        out = json.dumps({"content": content}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    # Fake Ollama server on localhost.
    ollama = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=ollama.serve_forever, daemon=True).start()
    ollama_url = "http://127.0.0.1:%d" % ollama.server_address[1]

    phone, _ = node_mod.load_or_create_identity(os.path.join(WORK, "phone"))
    mynode, _ = node_mod.load_or_create_identity(os.path.join(WORK, "mynode"))

    store = mock_list.Store()
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port
    try:
        # phone must be listed to queue (announce first)
        node_mod.NodeClient(os.path.join(WORK, "phone"), "phone", url).announce()
        status, body = tunnel_mod.send_job(
            url, phone, mynode.public_hex(), "text", {"text": "hi node"})
        check("text: announced text job queued", status == 200 and body.get("status") == "queued", str(body))
        job_id = body.get("id")

        def text_h(payload):
            return tunnel_mod.ollama_text(payload.get("text", ""), url=ollama_url)

        poller = tunnel_mod.TunnelClient(
            os.path.join(WORK, "mynode"), url, {phone.public_hex()},
            {"text": text_h})
        processed, skipped = poller.run_once()
        check("text: poller processed text job", processed == [job_id], str((processed, skipped)))

        status, body = tunnel_mod.poll_mailbox(url, phone)
        replies = [m for m in body.get("messages", []) if m.get("type") == "reply"]
        check("text: phone receives {text} reply in app shape",
              status == 200 and len(replies) == 1
              and replies[0].get("in_reply_to") == job_id
              and replies[0].get("result", {}).get("text") == "hello back: hi node",
              str(replies))

        # Shape check (ollama_text directly, via fake Ollama).
        out = tunnel_mod.ollama_text("shape?", url=ollama_url)
        check("text: ollama_text returns {text} only", set(out.keys()) == {"text"} and out["text"].startswith("hello back:"), str(out))

        # llama_text (LFM3 path) via a fake llama-server: shape, prompt
        # format, and fail-loud on empty content.
        llama = HTTPServer(("127.0.0.1", 0), FakeLlama)
        threading.Thread(target=llama.serve_forever, daemon=True).start()
        llama_url = "http://127.0.0.1:%d" % llama.server_address[1]
        try:
            out = tunnel_mod.llama_text("shape?", url=llama_url)
            check("text: llama_text returns {text} only",
                  set(out.keys()) == {"text"} and out["text"] == "llama says hi", str(out))
            req = FakeLlama.seen[-1]
            check("text: llama_text prompt is User/Assistant with User stops",
                  req.get("prompt", "").startswith("User: shape?")
                  and req.get("prompt", "").endswith("Assistant:")
                  and "User:" in req.get("stop", [])
                  and req.get("n_predict") == 512, str(req))
            try:
                tunnel_mod.llama_text("empty-test", url=llama_url)
                check("text: llama_text empty content -> raises", False, "no raise")
            except Exception as e:
                check("text: llama_text empty content -> raises (fail loud)",
                      True, type(e).__name__)
        finally:
            llama.shutdown()

        # Unreachable-backend check (deterministic): the handler must
        # raise, not echo — that is the fail-loud path.
        try:
            tunnel_mod.ollama_text("x", url="http://127.0.0.1:9")
            check("text: unreachable backend -> handler raises", False, "no raise")
        except Exception as e:
            check("text: unreachable backend -> handler raises (fail loud)",
                  True, type(e).__name__)
    finally:
        server.shutdown()
        ollama.shutdown()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
