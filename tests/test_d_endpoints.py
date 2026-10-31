"""0.0.1d endpoint tests: telemetry persistence, live system panels,
chat-image storage. Runs a real Hub Handler on a temp data dir.
"""

import base64
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

from hub import web as web_mod  # noqa: E402
from hub.identity import ensure_identity  # noqa: E402

Handler = web_mod.Handler

PNG_1X1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class DEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_env = os.environ.get("HUB_DATA_DIR")
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["HUB_DATA_DIR"] = cls.tmp.name
        cls.ident, _ = ensure_identity(cls.tmp.name)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if cls._old_env is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = cls._old_env
        cls.tmp.cleanup()

    def _get(self, path):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (self.port, path),
                    timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _get_json(self, path):
        status, raw = self._get(path)
        return status, json.loads(raw.decode("utf-8"))

    def _post(self, path, payload):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_session_telemetry_round_trips(self):
        sid = "chat-d-telemetry"
        msgs = [{"role": "user", "content": "hi", "ts": 1700000000000},
                {"role": "assistant", "content": "hello", "ts": 1700000001000,
                 "tokens": 42, "tok_s": 7.5, "model": "local uncensored",
                 "memories_used": 1,
                 "memories": [{"id": 3, "text": "keeper note",
                               "source": "chat", "model": "m", "score": 0.9}]}]
        status, body = self._post("/api/sessions",
                                  {"id": sid, "title": "hi",
                                   "messages": msgs, "type": "CHAT"})
        self.assertEqual(status, 200)
        status, listing = self._get_json("/api/sessions")
        self.assertEqual(status, 200)
        sess = [s for s in listing.get("sessions", [])
                if isinstance(s, dict) and s.get("id") == sid]
        self.assertTrue(sess, "saved session must list back")
        saved = sess[0]["messages"][1]
        self.assertEqual(saved["tokens"], 42)
        self.assertEqual(saved["tok_s"], 7.5)
        self.assertEqual(saved["memories"][0]["text"], "keeper note")

    def test_system_carries_live_ledger_and_regula(self):
        status, body = self._get_json("/api/system")
        self.assertEqual(status, 200)
        self.assertIn("ledger", body)
        self.assertIn("regula", body)
        for key in ("ok", "height", "blocks", "events"):
            self.assertIn(key, body["ledger"])
        reg = body["regula"]
        self.assertEqual(reg["key_type"], "secp256k1")
        self.assertTrue(reg["fingerprint"].startswith("NODE-"))
        self.assertEqual(reg["pubkey"], self.ident.public_hex())
        self.assertIsInstance(reg["signing_ok"], bool)

    def test_chat_image_save_and_get_round_trip(self):
        status, body = self._post("/api/chat-images/save",
                                  {"data": PNG_1X1, "filename": "peer-test"})
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        name = body["filename"]
        status, raw = self._get("/api/chat-images/" + name)
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\x89PNG"))
        self.assertEqual(raw, base64.b64decode(PNG_1X1))

    def test_chat_image_rejects_non_png_and_traversal(self):
        status, _ = self._post("/api/chat-images/save",
                               {"data": base64.b64encode(b"not a png").decode(),
                                "filename": "x"})
        self.assertEqual(status, 400)
        status, _ = self._get("/api/chat-images/../identity.dat")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
