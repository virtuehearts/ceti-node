"""Tests for the CETI chat ladder (hub/ceti_chat.py) + memory links.

Fixtures build a fake CETI data dir (one cassette with a category,
a wildcard category and a <fact> element; a facts.sqlite3 row) and
point BRAIN_CETI_DIR at it. Local-model and memory tiers are
monkeypatched — no servers, no model, no network.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="ceti-chat-test-")
os.environ["HUB_DATA_DIR"] = TMP

from hub import brain, ceti_chat
from hub import memory


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class CetiChatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = os.path.join(TMP, "ceti-data")
        _write(os.path.join(cls.fixture, "cassettes", "one.aiml"),
               "<aiml>"
               "<category><pattern>WHAT IS FIDONET</pattern>"
               "<template>FidoNet is a BBS network.</template></category>"
               "<category><pattern>HELLO *</pattern>"
               "<template>Hi <star/>!</template></category>"
               "<fact subject=\"Mars\" predicate=\"color\" object=\"red\"/>"
               "</aiml>")
        conn = sqlite3.connect(os.path.join(cls.fixture, "facts.sqlite3"))
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY,"
                     " subject TEXT, predicate TEXT, object TEXT,"
                     " confidence REAL, source TEXT, created REAL,"
                     " superseded INT, superseded_by INT)")
        conn.execute("INSERT INTO facts VALUES (1,'toronto','country',"
                     "'canada',0.9,'test',0,0,0)")
        conn.commit()
        conn.close()
        cls.old_env = os.environ.get("BRAIN_CETI_DIR")
        os.environ["BRAIN_CETI_DIR"] = cls.fixture
        cls.orig_mem = ceti_chat._memory_hits
        cls.orig_model = ceti_chat._local_model

    @classmethod
    def tearDownClass(cls):
        ceti_chat._memory_hits = cls.orig_mem
        ceti_chat._local_model = cls.orig_model
        if cls.old_env is None:
            os.environ.pop("BRAIN_CETI_DIR", None)
        else:
            os.environ["BRAIN_CETI_DIR"] = cls.old_env

    def test_db_fact_tier(self):
        r = ceti_chat.answer("what country is toronto in")
        self.assertEqual(r["tier"], "facts")
        self.assertIn("canada", r["answer"])
        self.assertIn("FACTS", r["thinking"])
        self.assertIn("hit", r["thinking"])

    def test_cassette_fact_tier(self):
        r = ceti_chat.answer("what color is mars")
        self.assertEqual(r["tier"], "facts")
        self.assertIn("red", r["answer"])

    def test_aiml_exact_tier(self):
        r = ceti_chat.answer("what is fidonet")
        self.assertEqual(r["tier"], "aiml")
        self.assertIn("BBS network", r["answer"])

    def test_aiml_wildcard_star(self):
        r = ceti_chat.answer("hello there friend")
        self.assertEqual(r["tier"], "aiml")
        self.assertIn("THERE FRIEND", r["answer"])

    def test_local_model_tier_carries_node_ids(self):
        ceti_chat._memory_hits = lambda q, h: [
            {"id": 7, "text": "prior note", "source": "chat",
             "model": "test-model", "score": 0.9}]
        ceti_chat._local_model = lambda msgs: "model says hi"
        try:
            r = ceti_chat.answer("something unanswerable locally")
        finally:
            ceti_chat._memory_hits = self.orig_mem
            ceti_chat._local_model = self.orig_model
        self.assertEqual(r["tier"], "local-model")
        self.assertEqual(r["memories"][0]["id"], 7)
        self.assertFalse(r["cloud_suggested"])

    def test_cloud_suggested_never_calls_cloud(self):
        ceti_chat._memory_hits = lambda q, h: []
        ceti_chat._local_model = lambda msgs: (_ for _ in ()).throw(
            RuntimeError("no chat server"))
        try:
            r = ceti_chat.answer("something unanswerable locally")
        finally:
            ceti_chat._memory_hits = self.orig_mem
            ceti_chat._local_model = self.orig_model
        self.assertEqual(r["tier"], "cloud-suggested")
        self.assertTrue(r["cloud_suggested"])

    def test_no_brain_data_still_honest(self):
        empty = os.path.join(TMP, "empty")
        os.makedirs(empty, exist_ok=True)
        roots = brain.resolve_roots(candidates=[empty])
        self.assertFalse(roots["found"])
        ceti_chat._memory_hits = lambda q, h: []
        ceti_chat._local_model = lambda msgs: (_ for _ in ()).throw(
            RuntimeError("no chat server"))
        try:
            r = ceti_chat.answer("hello", roots=roots)
        finally:
            ceti_chat._memory_hits = self.orig_mem
            ceti_chat._local_model = self.orig_model
        self.assertEqual(r["tier"], "cloud-suggested")

    def test_used_by_links(self):
        work = os.path.join(TMP, "memdb")
        os.makedirs(work, exist_ok=True)
        conn = memory.connect(work)
        mid = memory.add(conn, "h", "first memory", [0.1, 0.2],
                         source="chat", model="m1")
        rid = memory.add(conn, "h", "reply that used it", [0.3, 0.4],
                         source="chat", model="m1")
        memory.log_usage(conn, mid, rid)
        used = memory.used_by(conn, mid)
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0]["id"], rid)
        self.assertIn("reply that used it", used[0]["text_preview"])


    def test_thinking_trace_lists_every_layer_on_miss(self):
        ceti_chat._memory_hits = lambda q, h: []
        ceti_chat._local_model = lambda msgs: (_ for _ in ()).throw(
            RuntimeError("no chat server"))
        try:
            r = ceti_chat.answer("something unanswerable locally")
        finally:
            ceti_chat._memory_hits = self.orig_mem
            ceti_chat._local_model = self.orig_model
        self.assertEqual(r["tier"], "cloud-suggested")
        for layer in ("FACTS", "AIML", "VECTOR MEMORY", "LOCAL MODEL", "CLOUD"):
            self.assertIn(layer, r["thinking"])
        self.assertIn("never called", r["thinking"])

    def test_local_model_thinking_names_context_nodes(self):
        ceti_chat._memory_hits = lambda q, h: [
            {"id": 7, "text": "prior note", "source": "chat",
             "model": "test-model", "score": 0.9}]
        ceti_chat._local_model = lambda msgs: "model says hi"
        try:
            r = ceti_chat.answer("something unanswerable locally")
        finally:
            ceti_chat._memory_hits = self.orig_mem
            ceti_chat._local_model = self.orig_model
        self.assertEqual(r["tier"], "local-model")
        self.assertIn("VECTOR MEMORY", r["thinking"])
        self.assertIn("#7", r["thinking"])

    def test_facts_search_hits_both_stores(self):
        db = ceti_chat.facts_search("what country is toronto in")
        self.assertEqual(len(db["matches"]), 1)
        self.assertEqual(db["matches"][0]["subject"], "toronto")
        self.assertEqual(db["matches"][0]["source"], "fact store")
        cas = ceti_chat.facts_search("what color is mars")
        self.assertEqual(len(cas["matches"]), 1)
        self.assertEqual(cas["matches"][0]["subject"], "Mars")
        self.assertEqual(cas["matches"][0]["source"], "one.aiml")
        self.assertEqual(ceti_chat.facts_search("unrelated nothing")["matches"], [])

    def test_record_answer_memory_writes_and_dedupes(self):
        from hub import memory
        d = tempfile.mkdtemp()
        conn = memory.connect(d)
        conn.close()

        def fake_embed(text):
            return [1.0, 0.0]

        def connect_fn(_d):
            return memory.connect(_d)

        ok = ceti_chat.record_answer_memory(
            d, "testhash", "capital of canada",
            "Canada — capital: Ottawa", "facts",
            embed_fn=fake_embed, connect_fn=connect_fn)
        self.assertTrue(ok)
        conn = memory.connect(d)
        try:
            self.assertEqual(memory.count(conn, "testhash"), 2)
            ok2 = ceti_chat.record_answer_memory(
                d, "testhash", "capital of canada",
                "Canada — capital: Ottawa", "facts",
                embed_fn=fake_embed, connect_fn=connect_fn)
            self.assertFalse(ok2)
            self.assertEqual(memory.count(conn, "testhash"), 2)
            ok3 = ceti_chat.record_answer_memory(
                d, "testhash", "what is fidonet",
                "some miss text", "cloud-suggested",
                embed_fn=fake_embed, connect_fn=connect_fn)
            self.assertFalse(ok3)
            self.assertEqual(memory.count(conn, "testhash"), 2)
        finally:
            conn.close()

    def test_record_miss_dedupes_and_caps(self):
        with tempfile.TemporaryDirectory() as d:
            ceti_chat.record_miss(d, "what is fidonet")
            ceti_chat.record_miss(d, "what is fidonet")
            got = ceti_chat.read_suggestions(d)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["query"], "what is fidonet")
            for i in range(ceti_chat._SUGGESTIONS_CAP + 5):
                ceti_chat.record_miss(d, "q%d" % i)
            self.assertEqual(len(ceti_chat.read_suggestions(d)),
                             ceti_chat._SUGGESTIONS_CAP)
            self.assertEqual(ceti_chat.read_suggestions(d)[0]["query"],
                             "q%d" % (ceti_chat._SUGGESTIONS_CAP + 4))


if __name__ == "__main__":
    unittest.main()
