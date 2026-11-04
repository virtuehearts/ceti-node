"""Research agent tests: lynx extraction, distill parsing, task
lifecycle, local compile, and the promotion gate. No network, no
model servers: fetch and distill are injected where the test needs
them, and the embed server is expected down (memories == [])."""
import os
import sqlite3
import tempfile
import unittest

from hub import research
from hub.lynx import extract_text

SAMPLE_HTML = """
<html><head><title>Fidonet History</title>
<style>.x{display:none}</style><script>var a = 1;</script></head>
<body><nav><a href="/home">Home</a></nav>
<h1>Fidonet</h1>
<p>Fidonet began in 1984 as a network for BBS systems.</p>
<ul><li>Tom Jennings created it</li><li>Nodes exchanged mail overnight</li></ul>
<p>See <a href="https://example.org/nodelist">the nodelist</a>.</p>
</body></html>
"""


class LynxTest(unittest.TestCase):
    def test_extract_text(self):
        out = extract_text(SAMPLE_HTML, base_url="https://example.org/")
        self.assertEqual(out["title"], "Fidonet History")
        self.assertIn("Fidonet began in 1984", out["text"])
        self.assertIn("Tom Jennings created it", out["text"])
        self.assertNotIn("var a = 1", out["text"])
        self.assertNotIn("display:none", out["text"])
        self.assertIn("https://example.org/nodelist", out["links"])

    def test_extract_empty(self):
        out = extract_text("", base_url="")
        self.assertEqual(out["title"], "")
        self.assertEqual(out["text"], "")


class DistillParseTest(unittest.TestCase):
    def test_clean_json(self):
        raw = ('{"qa": [{"q": "What is Fidonet?", "a": "A BBS network."}],'
               ' "facts": [{"subject": "Fidonet", "predicate": "founded",'
               ' "object": "1984"}]}')
        out = research._extract_distill(raw)
        self.assertEqual(len(out["qa"]), 1)
        self.assertEqual(out["facts"][0]["object"], "1984")

    def test_prose_wrapped_json(self):
        raw = ('Here is what I found:\n'
               '{"qa": [{"q": "Q?", "a": "A."}], "facts": []}\n'
               'Hope that helps!')
        out = research._extract_distill(raw)
        self.assertEqual(len(out["qa"]), 1)

    def test_garbage_is_empty(self):
        out = research._extract_distill("no json here at all")
        self.assertEqual(out, {"qa": [], "facts": []})

    def test_url_list_parse(self):
        raw = '["https://example.org/a", "not a url", "https://example.org/b"]'
        self.assertEqual(research._extract_urls(raw),
                         ["https://example.org/a", "https://example.org/b"])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = research.Store(self.tmp)

    def test_task_lifecycle(self):
        tid = self.store.add_task("research", "fidonet", "fidonet", [])
        self.assertEqual(tid, self.store.add_task(
            "research", "fidonet", "fidonet", []))
        self.assertTrue(self.store.claim_task(tid))
        self.assertFalse(self.store.claim_task(tid))
        self.store.finish_task(tid, "done", {"proposals": 2})
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["result"]["proposals"], 2)
        self.assertEqual(self.store.counts(), {"done": 1})

    def test_next_workable_prefers_pending(self):
        a = self.store.add_task("research", "first", "first", [])
        b = self.store.add_task("research", "second", "second", [])
        self.store.finish_task(b, "needs-model", {})
        self.assertEqual(self.store.next_workable(), a)

    def test_proposal_dedupe(self):
        tid = self.store.add_task("research", "t", "t", [])
        p1 = self.store.add_proposal(tid, "fact", subject="Fidonet",
                                     predicate="founded", object="1984")
        p2 = self.store.add_proposal(tid, "fact", subject="Fidonet",
                                     predicate="founded", object="1984")
        self.assertEqual(p1, p2)
        self.assertEqual(self.store.pending_proposal_count(), 1)

    def test_bench_roundtrip(self):
        rid = self.store.add_bench_run(1.0, 2.0, 4, 0.75,
                                       {"misses": [{"question": "q?"}]})
        bench = self.store.last_bench()
        self.assertEqual(bench["id"], rid)
        self.assertEqual(bench["accuracy"], 0.75)


class CompileTest(unittest.TestCase):
    def test_facts_compile(self):
        # Packaged seed brain (tree brain/) resolves by default here.
        out = research.compile_local(tempfile.mkdtemp(), None,
                                     "capital of canada")
        subjects = [f["subject"] for f in out["facts"]]
        self.assertIn("Canada", subjects)
        capitals = [f for f in out["facts"]
                    if f["predicate"] == "capital"]
        self.assertTrue(capitals)
        self.assertEqual(capitals[0]["object"], "Ottawa")

    def test_aiml_compile_by_cassette_name(self):
        out = research.compile_local(tempfile.mkdtemp(), None, "fidonet")
        self.assertTrue(out["aiml"])
        # The fidonet cassette itself surfaces, plus any other
        # cassette with a Fidonet-mentioning pattern.
        self.assertTrue(any("fidonet" in a["evidence"]
                            for a in out["aiml"]))
        self.assertTrue(all(a["answer"] for a in out["aiml"]))


class RunTaskTest(unittest.TestCase):
    def test_full_run_to_proposals(self):
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task("research", "fidonet", "fidonet",
                             ["file:///tmp/does-not-matter.html"])

        def fake_fetch(url):
            return {"url": url, "title": "Fidonet",
                    "text": "Fidonet began in 1984. Tom Jennings created it.",
                    "links": [], "error": ""}

        def fake_distill(report, topic):
            self.assertIn("1984", report)
            return {"qa": [{"q": "What is Fidonet?",
                            "a": "A network for BBS systems."}],
                    "facts": [{"subject": "Fidonet", "predicate": "founded",
                               "object": "1984"}],
                    "model_used": "test"}

        out = research.run_task(d, None, tid, distill_fn=fake_distill,
                                fetch_fn=fake_fetch)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["proposals"], 2)
        props = store.list_proposals(task_id=tid)
        self.assertEqual(len(props), 2)
        self.assertTrue(all(p["status"] == "pending" for p in props))

    def test_no_model_parks_task(self):
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task("research", "t", "t", ["file:///tmp/x.html"])

        def fake_fetch(url):
            return {"url": url, "title": "T", "text": "some text",
                    "links": [], "error": ""}

        def fake_distill(report, topic):
            return {"error": "no model available (local chat down, no cloud key)"}

        out = research.run_task(d, None, tid, distill_fn=fake_distill,
                                fetch_fn=fake_fetch)
        self.assertEqual(out["status"], "needs-model")
        self.assertEqual(store.get_task(tid)["status"], "needs-model")

    def test_failed_fetch_fails_task(self):
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task("research", "t", "t", ["https://invalid/"])

        def fake_fetch(url):
            return {"url": url, "title": "", "text": "", "links": [],
                    "error": "fetch failed: boom"}

        out = research.run_task(d, None, tid, fetch_fn=fake_fetch)
        self.assertEqual(out["status"], "failed")


class DistillEmptyTest(unittest.TestCase):
    """A reasoning model that spends its budget in <think> returns an
    empty answer. That must fall back to cloud, or park the task as
    needs-model — never land "done" with zero proposals."""

    def setUp(self):
        self._old_local = research._local_complete
        self._old_cloud = research._cloud_complete

    def tearDown(self):
        research._local_complete = self._old_local
        research._cloud_complete = self._old_cloud

    def test_empty_local_no_cloud_parks(self):
        research._local_complete = lambda prompt: ""
        out = research.distill_text("report", "topic", None)
        self.assertIn("error", out)
        self.assertIn("returned no output", out["error"])

    def test_empty_local_falls_back_to_cloud(self):
        research._local_complete = lambda prompt: "   "
        research._cloud_complete = lambda cloud, sys, user, **kw: (
            '{"qa": [{"q": "What is Fidonet?", "a": "A BBS network."}], "facts": []}')
        out = research.distill_text("report", "topic",
                                    {"provider": "orcarouter", "api_key": "k",
                                     "model": "orcarouter/auto"})
        self.assertEqual(out["model_used"], "orcarouter/orcarouter/auto")
        self.assertEqual(out["qa"][0]["q"], "What is Fidonet?")

    def test_empty_output_task_parks_needs_model(self):
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task("research", "t", "t", ["file:///tmp/x.html"])

        def fake_fetch(url):
            return {"url": url, "title": "T", "text": "some text",
                    "links": [], "error": ""}

        def fake_distill(report, topic):
            return {"error": "local model returned no output (no cloud key to fall back to)"}

        out = research.run_task(d, None, tid, distill_fn=fake_distill,
                                fetch_fn=fake_fetch)
        self.assertEqual(out["status"], "needs-model")


class ApproveTest(unittest.TestCase):
    def test_fact_approval_lands_in_operator_brain(self):
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task("research", "t", "t", [])
        pid = store.add_proposal(tid, "fact", subject="Fidonet",
                                 predicate="founded", object="1984")
        out = research.approve_proposal(d, None, pid)
        self.assertEqual(out["status"], "approved")
        self.assertIn("fact store", out["landed"])
        db = sqlite3.connect(os.path.join(d, "brain", "facts.sqlite3"))
        row = db.execute(
            "SELECT subject, predicate, object, confidence, source"
            " FROM facts WHERE subject='Fidonet'").fetchone()
        db.close()
        self.assertEqual(row, ("Fidonet", "founded", "1984", 0.9, "research"))
        # Approving again is an error, not a duplicate row.
        out2 = research.approve_proposal(d, None, pid)
        self.assertIn("error", out2)

    def test_qa_approval_writes_cassette(self):
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task("research", "t", "t", [])
        pid = store.add_proposal(tid, "qa", question="What is Fidonet?",
                                 answer="A network for BBS systems.")
        out = research.approve_proposal(d, None, pid)
        self.assertEqual(out["status"], "approved")
        self.assertIn("cassette research-learned.aiml", out["landed"])
        path = os.path.join(d, "brain", "cassettes", "research-learned.aiml")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("<pattern>WHAT IS FIDONET</pattern>", content)
        self.assertIn("A network for BBS systems.", content)


class KeyTermsTest(unittest.TestCase):
    def test_key_terms(self):
        terms = research.key_terms(
            "Fidonet began in 1984 as a network for bulletin board systems.")
        self.assertIn("fidonet", terms)
        self.assertIn("network", terms)
        self.assertIn("began", terms)  # 5 letters counts
        self.assertNotIn("for", terms)  # short words excluded
        self.assertLessEqual(len(terms), 6)


if __name__ == "__main__":
    unittest.main()
