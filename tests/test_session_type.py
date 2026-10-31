"""Session workspace-type tests — stdlib unittest only, temp dirs only.

Covers the Draft 4 workspace picker backend: POST /api/sessions accepts
a type (picker key or canonical tag), GET /api/sessions reports the
canonical tag, legacy files without a type default to CHAT, re-saving
without a type preserves the stored type, and unknown types fall back
to CHAT rather than erroring. Served from a real server thread on a
throwaway HUB_DATA_DIR. Never touches the real data/ directory.
"""
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from hub.identity import ensure_identity
from hub.web import Handler


class SessionTypeEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_env = os.environ.get("HUB_DATA_DIR")
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["HUB_DATA_DIR"] = cls.tmp.name
        ensure_identity(cls.tmp.name)
        # A legacy session file with no type field at all.
        os.makedirs(os.path.join(cls.tmp.name, "web-sessions"), exist_ok=True)
        with open(os.path.join(cls.tmp.name, "web-sessions",
                               "chat-legacy.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "chat-legacy", "title": "legacy chat",
                       "messages": [{"role": "user", "content": "hi"}],
                       "saved_at": "2026-10-26 10:00"}, f)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
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

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _sessions(self):
        status, body = self._req("GET", "/api/sessions")
        self.assertEqual(status, 200)
        return {s["id"]: s for s in body["sessions"]}

    def test_picker_key_maps_to_canonical_tag(self):
        status, body = self._req("POST", "/api/sessions",
                                 {"id": "ws-story", "title": "Untitled workspace",
                                  "messages": [], "type": "story"})
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], "ws-story")
        self.assertEqual(self._sessions()["ws-story"]["type"], "STORY/EZINE")

    def test_tag_input_accepted_and_other_keys_mapped(self):
        self._req("POST", "/api/sessions",
                  {"id": "ws-code", "title": "t", "messages": [],
                   "type": "CODE SPACES"})
        self._req("POST", "/api/sessions",
                  {"id": "ws-research", "title": "t", "messages": [],
                   "type": "research"})
        sessions = self._sessions()
        self.assertEqual(sessions["ws-code"]["type"], "CODE SPACES")
        self.assertEqual(sessions["ws-research"]["type"], "RESEARCH")

    def test_missing_type_defaults_to_chat(self):
        self._req("POST", "/api/sessions",
                  {"id": "ws-notype", "title": "t", "messages": []})
        self.assertEqual(self._sessions()["ws-notype"]["type"], "CHAT")

    def test_unknown_type_falls_back_to_chat(self):
        self._req("POST", "/api/sessions",
                  {"id": "ws-weird", "title": "t", "messages": [],
                   "type": "teleport"})
        self.assertEqual(self._sessions()["ws-weird"]["type"], "CHAT")

    def test_legacy_file_without_type_lists_as_chat(self):
        self.assertEqual(self._sessions()["chat-legacy"]["type"], "CHAT")

    def test_resave_without_type_preserves_stored_type(self):
        self._req("POST", "/api/sessions",
                  {"id": "ws-keep", "title": "t", "messages": [], "type": "code"})
        self.assertEqual(self._sessions()["ws-keep"]["type"], "CODE SPACES")
        # Re-save (as the chat UI does after every answer) without a type.
        self._req("POST", "/api/sessions",
                  {"id": "ws-keep", "title": "t2",
                   "messages": [{"role": "user", "content": "hi"}]})
        kept = self._sessions()["ws-keep"]
        self.assertEqual(kept["type"], "CODE SPACES")
        self.assertEqual(kept["title"], "t2")

    def test_single_session_file_carries_type_on_disk(self):
        # The UI switches from list data (no single-session GET exists),
        # so the storage format itself is the contract: the file must
        # carry the canonical tag.
        self._req("POST", "/api/sessions",
                  {"id": "ws-single", "title": "t", "messages": [],
                   "type": "research"})
        with open(os.path.join(self.tmp.name, "web-sessions",
                               "ws-single.json"), encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored["type"], "RESEARCH")
        self.assertEqual(self._sessions()["ws-single"]["type"], "RESEARCH")


if __name__ == "__main__":
    unittest.main()
