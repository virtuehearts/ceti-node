"""Memory vector endpoint tests — stdlib unittest only, temp dirs only.

Covers the constellation/vector backend: memory.get / memory.recent
round-trips (768-dim and 256-dim), and the live HTTP endpoints
GET /api/memories and GET /api/memories/<id>/vector served from a
real server thread on a throwaway HUB_DATA_DIR. Never touches the
real data/ directory.
"""
import array
import json
import os
import random
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from hub import memory
from hub.identity import ensure_identity
from hub.web import Handler


def _vec(rng, dim, base=None, noise=0.0):
    if base is None:
        return [rng.gauss(0, 1) for _ in range(dim)]
    return [b + rng.gauss(0, noise) for b in base]


class MemoryHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ident, _ = ensure_identity(self.tmp.name)
        self.conn = memory.connect(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_round_trips_768_and_256_dims(self):
        rng = random.Random(7)
        v768 = _vec(rng, 768)
        v256 = _vec(rng, 256)
        id768 = memory.add(self.conn, self.ident.hash, "big memory", v768, source="chat")
        id256 = memory.add(self.conn, self.ident.hash, "small memory", v256, source="note")
        got768 = memory.get(self.conn, self.ident.hash, id768)
        got256 = memory.get(self.conn, self.ident.hash, id256)
        self.assertEqual(len(got768["vector"]), 768)
        self.assertEqual(len(got256["vector"]), 256)
        self.assertEqual(got768["vector"], array.array("f", v768).tolist())
        self.assertEqual(got256["vector"], array.array("f", v256).tolist())
        self.assertEqual(got768["text"], "big memory")

    def test_get_is_identity_scoped_and_unknown_is_none(self):
        rng = random.Random(8)
        rid = memory.add(self.conn, self.ident.hash, "mine", _vec(rng, 64), source="chat")
        other_dir = tempfile.mkdtemp()
        other, _ = ensure_identity(other_dir)
        self.assertIsNone(memory.get(self.conn, other.hash, rid))
        self.assertIsNone(memory.get(self.conn, self.ident.hash, rid + 9999))

    def test_recent_is_newest_first(self):
        rng = random.Random(9)
        ids = [memory.add(self.conn, self.ident.hash, "m%d" % i, _vec(rng, 32), source="chat")
               for i in range(3)]
        got = memory.recent(self.conn, self.ident.hash, 2)
        self.assertEqual([g["id"] for g in got], [ids[2], ids[1]])

    def test_model_round_trips_in_add_search_get_recent(self):
        rng = random.Random(10)
        rid = memory.add(self.conn, self.ident.hash, "model memory", _vec(rng, 32),
                         source="web-chat", model="ministral-test-7b")
        hits = memory.search(self.conn, self.ident.hash, _vec(rng, 32), top_k=5)
        self.assertEqual(len(hits[0]), 5)
        self.assertEqual(hits[0][4], "ministral-test-7b")
        self.assertEqual(memory.get(self.conn, self.ident.hash, rid)["model"], "ministral-test-7b")
        self.assertEqual(memory.recent(self.conn, self.ident.hash, 5)[0]["model"], "ministral-test-7b")
        # No model given: stored as empty, never invented.
        rid2 = memory.add(self.conn, self.ident.hash, "unlabelled", _vec(rng, 32), source="chat")
        self.assertEqual(memory.get(self.conn, self.ident.hash, rid2)["model"], "")

    def test_model_column_migrates_old_database(self):
        import sqlite3
        old_dir = tempfile.mkdtemp()
        p = os.path.join(old_dir, "memory.db")
        raw = sqlite3.connect(p)
        raw.executescript(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " identity_hash TEXT NOT NULL, text TEXT NOT NULL,"
            " embedding BLOB NOT NULL, created_at REAL NOT NULL,"
            " source TEXT NOT NULL DEFAULT 'chat', tags TEXT NOT NULL DEFAULT '');")
        blob = array.array("f", [0.5] * 16).tobytes()
        raw.execute("INSERT INTO memories (identity_hash, text, embedding, created_at, source, tags)"
                    " VALUES (?, ?, ?, ?, ?, ?)", ("h-old", "legacy row", blob, 1234.0, "chat", ""))
        raw.commit()
        raw.close()
        conn = memory.connect(old_dir)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
        self.assertIn("model", cols)
        got = memory.get(conn, "h-old", 1)
        self.assertEqual(got["text"], "legacy row")
        self.assertEqual(got["model"], "")
        hits = memory.search(conn, "h-old", [0.5] * 16, top_k=5)
        self.assertEqual(len(hits[0]), 5)
        self.assertEqual(hits[0][4], "")

    def test_usage_log_traces_both_ways(self):
        rng = random.Random(11)
        mid = memory.add(self.conn, self.ident.hash, "source memory", _vec(rng, 32), source="chat")
        reply = memory.add(self.conn, self.ident.hash, "Assistant: used it", _vec(rng, 32),
                           source="web-chat", model="m")
        memory.log_usage(self.conn, mid, reply)
        used = memory.used_by(self.conn, mid)
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0]["id"], reply)
        self.assertIn("used it", used[0]["text_preview"])


class VectorEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_env = os.environ.get("HUB_DATA_DIR")
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["HUB_DATA_DIR"] = cls.tmp.name
        cls.ident, _ = ensure_identity(cls.tmp.name)
        conn = memory.connect(cls.tmp.name)
        rng = random.Random(42)
        base_a = _vec(rng, 768)
        base_b = _vec(rng, 768)
        cls.ids = {}
        cls.ids["light1"] = memory.add(conn, cls.ident.hash,
                                       "lighthouse keeper found a door in the water",
                                       _vec(rng, 768, base_a, 0.08), source="chat")
        cls.ids["light2"] = memory.add(conn, cls.ident.hash,
                                       "the keeper opened the door beneath the lighthouse",
                                       _vec(rng, 768, base_a, 0.08), source="chat")
        cls.ids["relay1"] = memory.add(conn, cls.ident.hash,
                                       "relay node mesh signal unanswered at midnight",
                                       _vec(rng, 768, base_b, 0.08), source="chat")
        cls.ids["small"] = memory.add(conn, cls.ident.hash,
                                      "small vector probe", _vec(rng, 256), source="note")
        cls.session_id = "chat-20261026-lighthouse"
        os.makedirs(os.path.join(cls.tmp.name, "web-sessions"), exist_ok=True)
        with open(os.path.join(cls.tmp.name, "web-sessions", cls.session_id + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump({"id": cls.session_id, "title": "lighthouse session",
                       "messages": [{"role": "user", "content": "lighthouse keeper door water story"},
                                    {"role": "assistant", "content": "the keeper waits"}],
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

    def _get(self, path):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path),
                                        timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _post(self, path, payload):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_vector_round_trip_shape_dim_and_keys(self):
        status, body = self._get("/api/memories/%d/vector" % self.ids["light1"])
        self.assertEqual(status, 200)
        self.assertEqual(set(body.keys()),
                         {"id", "dim", "model", "source", "created_at", "text", "vector"})
        self.assertEqual(body["id"], self.ids["light1"])
        self.assertEqual(body["dim"], 768)
        self.assertEqual(len(body["vector"]), 768)
        self.assertEqual(body["model"], "EmbeddingGemma-2")
        self.assertTrue(all(isinstance(v, float) for v in body["vector"]))

    def test_vector_reports_true_dim_for_other_sizes(self):
        status, body = self._get("/api/memories/%d/vector" % self.ids["small"])
        self.assertEqual(status, 200)
        self.assertEqual(body["dim"], 256)
        self.assertEqual(len(body["vector"]), 256)

    def test_vector_unknown_and_nonnumeric_ids_are_clean_404(self):
        status, body = self._get("/api/memories/999999/vector")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not found"})
        status, body = self._get("/api/memories/abc/vector")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not found"})

    def test_memories_list_shape_edges_and_core_links(self):
        status, body = self._get("/api/memories?session=" + self.session_id)
        self.assertEqual(status, 200)
        self.assertEqual(set(body.keys()), {"nodes", "edges", "core_links", "shown", "total"})
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["shown"], 4)
        node_ids = {n["id"] for n in body["nodes"]}
        self.assertEqual(node_ids, set(self.ids.values()))
        # Cluster members are near-identical, so similarity edges exist.
        self.assertTrue(any(e["score"] > 0.55 for e in body["edges"]))
        for e in body["edges"]:
            self.assertIn(e["a"], node_ids)
            self.assertIn(e["b"], node_ids)
        # The session talks about lighthouse/keeper/door/water: both
        # lighthouse memories must link to the session core.
        self.assertIn(self.ids["light1"], body["core_links"])
        self.assertIn(self.ids["light2"], body["core_links"])

    def test_memories_list_full_view_shape(self):
        status, body = self._get("/api/memories/list?limit=100")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["shown"], 4)
        for n in body["nodes"]:
            self.assertIn("model", n)
            self.assertIn("used_count", n)
            self.assertIn("used_by", n)
            self.assertEqual(n["used_count"], 0)
            self.assertEqual(n["used_by"], [])

    def test_voice_shared_and_ledger_status(self):
        status, body = self._get("/api/voice/shared")
        self.assertEqual(status, 200)
        # Shared files persist across tests in this class (the share
        # test runs first alphabetically), so assert shape, not emptiness.
        self.assertIsInstance(body["files"], list)
        self.assertTrue(body["ledger"]["ok"])
        status, body = self._get("/api/ledger/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["height"], 0)

    def test_voice_share_stores_file_and_ledgers_it(self):
        import base64
        status, body = self._post("/api/voice/share", {
            "filename": "voice-test.mp3",
            "data": base64.b64encode(b"ID3fakeaudio").decode("ascii")})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["filename"], "voice-test.mp3")
        self.assertIn("event_hash", body["ledger"])
        status, body = self._get("/api/voice/shared")
        self.assertEqual(len(body["files"]), 1)
        self.assertEqual(body["files"][0]["filename"], "voice-test.mp3")
        self.assertTrue(body["ledger"]["ok"])
        self.assertGreaterEqual(body["ledger"]["height"], 2)
    def test_session_save_ledgers_and_events(self):
        status, body = self._post("/api/sessions", {
            "id": "ledger-test-session", "title": "ledger test",
            "messages": [{"role": "user", "content": "hello ledger"}],
            "type": "CHAT"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ledger"]["ok"])
        self.assertIn("event_hash", body["ledger"])
        status, body = self._get("/api/ledger/events?limit=50")
        self.assertEqual(status, 200)
        kinds = [(e["kind"], e.get("detail", "")) for e in body["events"]]
        self.assertIn(("session", "CHAT"), kinds)

    def test_blocks_fetch_round_trip(self):
        from hub import regula
        src = os.path.join(self.tmp.name, "chunk-src.bin")
        with open(src, "wb") as f:
            f.write(b"block-data-0123456789")
        _sha, _size, chunks, _root = regula.chunk_file(self.tmp.name, src)
        sha = chunks[0]["sha256"]
        with urllib.request.urlopen("http://127.0.0.1:%d/api/blocks/%s" % (self.port, sha),
                                    timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"block-data-0123456789")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen("http://127.0.0.1:%d/api/blocks/%s" % (self.port, "0" * 64),
                                   timeout=10)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
