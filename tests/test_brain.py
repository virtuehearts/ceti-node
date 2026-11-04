"""Tests for the Brain pane readers (hub/brain.py).

Read-only, stdlib only, no servers, no model. Fixtures build a fake
CETI data dir (cassettes, persona, fact store, task queue, researcher
log) and BRAIN_CETI_DIR points at it, so the real trees are untouched.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="brain-test-")
os.environ["HUB_DATA_DIR"] = TMP

from hub import brain


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _db(path, stmts):
    conn = sqlite3.connect(path)
    for s in stmts:
        conn.execute(s)
    conn.commit()
    conn.close()


class BrainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = os.path.join(TMP, "ceti-data")
        _write(os.path.join(cls.fixture, "cassettes", "a.aiml"),
               "<aiml><category><pattern>HI</pattern><template>hello</template>"
               "</category><category><pattern>BYE</pattern><template>bye"
               "</template></category></aiml>")
        _write(os.path.join(cls.fixture, "cassettes", "geo", "b.aiml"),
               "<aiml><category><pattern>PARIS</pattern><template>France"
               "</template></category></aiml>")
        _write(os.path.join(cls.fixture, "cassettes", "note.txt"), "ignored")
        _write(os.path.join(cls.fixture, "persona.md"),
               "# Persona\n\nYou are the intellect.\n")
        _db(os.path.join(cls.fixture, "facts.sqlite3"), [
            "CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT,"
            " predicate TEXT, object TEXT, confidence REAL, source TEXT,"
            " created REAL, superseded INT, superseded_by INT)",
            "INSERT INTO facts VALUES (1,'paris','capital','france',0.9,"
            "'test',0,0,0)",
            "INSERT INTO facts VALUES (2,'c64','introduced','1982',0.9,"
            "'test',0,0,0)",
        ])
        _db(os.path.join(cls.fixture, "tasks.db"), [
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, kind TEXT,"
            " title TEXT, status TEXT, payload TEXT, run_after REAL,"
            " interval_s REAL, attempts INT, result TEXT, created REAL,"
            " updated REAL)",
            "INSERT INTO tasks VALUES (1,'benchmark','weekly self-exam',"
            "'pending','',0,0,0,'',0,1)",
            "INSERT INTO tasks VALUES (2,'research','old topic','done',"
            "'',0,0,0,'',0,2)",
            "CREATE TABLE gaps (question TEXT, count INT, first_seen REAL,"
            " last_seen REAL, tasked INT)",
            "INSERT INTO gaps VALUES ('why?',3,0,0,0)",
        ])
        _db(os.path.join(cls.fixture, "researcher_log.sqlite3"), [
            "CREATE TABLE log (topic TEXT, started REAL, finished REAL,"
            " qa_added INT, report TEXT)",
            "INSERT INTO log VALUES ('first',0,1,5,'')",
            "INSERT INTO log VALUES ('second',0,2,7,'')",
        ])
        cls.old_env = os.environ.get("BRAIN_CETI_DIR")
        os.environ["BRAIN_CETI_DIR"] = cls.fixture

    @classmethod
    def tearDownClass(cls):
        if cls.old_env is None:
            os.environ.pop("BRAIN_CETI_DIR", None)
        else:
            os.environ["BRAIN_CETI_DIR"] = cls.old_env

    def test_resolve_and_summary(self):
        roots = brain.resolve_roots()
        self.assertTrue(roots["found"])
        s = brain.summary(roots)
        self.assertTrue(s["found"])
        self.assertEqual(s["aiml"]["files"], 2)
        self.assertEqual(s["aiml"]["categories"], 3)
        self.assertTrue(s["bios"]["found"])
        self.assertGreater(s["bios"]["persona_chars"], 0)
        self.assertEqual(s["memory"]["facts"], 2)
        self.assertEqual(s["research"]["task_states"]["pending"], 1)
        self.assertEqual(s["research"]["task_states"]["done"], 1)
        self.assertEqual(s["research"]["gaps"], 1)
        self.assertEqual(s["research"]["last_topic"], "second")
        self.assertEqual(brain.research(roots)["gaps_list"],
                         [{"question": "why?", "count": 3}])
        self.assertEqual(s["library"]["files"], 0)
        self.assertEqual(s["knowledge"]["qa"], 0)

    def test_list_modules(self):
        mods = brain.list_modules()
        self.assertEqual([m["name"] for m in mods], ["a.aiml", "geo/b.aiml"])
        self.assertEqual(mods[0]["categories"], 2)
        self.assertEqual(mods[1]["categories"], 1)

    def test_read_module_and_guards(self):
        got = brain.read_module("geo/b.aiml")
        self.assertIn("PARIS", got["text"])
        for bad in ("../a.aiml", "/abs.aiml", "missing.aiml", "note.txt"):
            with self.assertRaises(ValueError):
                brain.read_module(bad)

    def test_facts(self):
        f = brain.facts(1)
        self.assertTrue(f["found"])
        self.assertEqual(f["count"], 2)
        self.assertEqual(len(f["rows"]), 1)
        self.assertEqual(f["rows"][0]["subject"], "c64")  # latest first

    def test_not_found(self):
        empty = os.path.join(TMP, "empty")
        os.makedirs(empty, exist_ok=True)
        roots = brain.resolve_roots(candidates=[empty])
        self.assertFalse(roots["found"])
        s = brain.summary(roots)
        self.assertFalse(s["found"])
        self.assertEqual(s["aiml"]["files"], 0)
        self.assertEqual(brain.list_modules(roots), [])
        with self.assertRaises(ValueError):
            brain.read_module("a.aiml", roots)


if __name__ == "__main__":
    unittest.main()
