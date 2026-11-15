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

    def test_cloud_distill_used_first_when_key_configured(self):
        # 2026-11-07: cloud-first. The local model provably returns
        # empty content for the JSON distill prompt, so when a cloud
        # key is configured the distill goes straight to cloud — the
        # local attempt is not even made (it wasted minutes).
        calls = {"local": 0}

        def fake_local(prompt):
            calls["local"] += 1
            return "   "

        research._local_complete = fake_local
        research._cloud_complete = lambda cloud, sys, user, **kw: (
            '{"qa": [{"q": "What is Fidonet?", "a": "A BBS network."}], "facts": []}')
        out = research.distill_text("report", "topic",
                                    {"provider": "orcarouter", "api_key": "k",
                                     "model": "orcarouter/auto"})
        self.assertEqual(calls["local"], 0)
        self.assertEqual(out["model_used"], "orcarouter/orcarouter/auto")
        self.assertEqual(out["qa"][0]["q"], "What is Fidonet?")

    def test_cloud_distill_gets_longer_report(self):
        # 2026-11-07: cloud models take 128k+ context, so the cloud
        # distill feeds the whole report (12000 chars), not the
        # 2000-char local slice.
        seen = {}

        def fake_cloud(cloud, sys, user, **kw):
            seen["prompt"] = user
            return '{"qa": [], "facts": []}'

        research._cloud_complete = fake_cloud
        out = research.distill_text("x" * 20000, "topic",
                                    {"provider": "nvidia", "api_key": "k",
                                     "model": "nvidia/bolt"})
        self.assertNotIn("error", out)
        report_part = seen["prompt"].split("REPORT:\n", 1)[1].rstrip("\n")
        self.assertGreater(len(report_part), research._REPORT_CHARS)
        self.assertLessEqual(len(report_part), research._REPORT_CHARS_CLOUD)

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

    def test_distill_report_fits_local_context(self):
        # 2026-11-06: the Nobel task parked as needs-model twice —
        # the 8000-char report overflowed the local chat server's
        # 2048-token context and llama-server rejected the call.
        # The prompt the model actually receives must stay small.
        seen = {}

        def fake_local(prompt):
            seen["prompt"] = prompt
            return '{"qa": [], "facts": []}'

        research._local_complete = fake_local
        out = research.distill_text("x" * 20000, "topic", None)
        self.assertNotIn("error", out)
        prompt = seen["prompt"]
        report_part = prompt.split("REPORT:\n", 1)[1].rstrip("\n")
        self.assertLessEqual(len(report_part), research._REPORT_CHARS)
        self.assertLessEqual(research._REPORT_CHARS, 2000)

    def test_stuck_running_task_reclaimable_by_force(self):
        # 2026-11-07: the Nobel task sat 'running' for 45 minutes
        # after two Hub restarts killed its worker thread. claim_task
        # only accepted pending/needs-model, so every re-run silently
        # no-opped ("task not pending") and the task was a zombie.
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task("research", "t", "t", ["file:///tmp/x.html"])
        self.assertTrue(store.claim_task(tid))
        self.assertEqual(store.get_task(tid)["status"], "running")
        # A normal claim refuses a running task (no double-run).
        self.assertFalse(store.claim_task(tid))
        # An explicit re-run force-claims it.
        self.assertTrue(store.claim_task(tid, force=True))
        # And a parked needs-model task is not auto-claimed by the
        # tick loop (only pending is) — it would otherwise re-run
        # forever, each distill failing the same way (17 attempts
        # on the Nobel task). An explicit re-run may still take it.
        store.finish_task(tid, "needs-model", {"error": "x"})
        self.assertFalse(store.claim_task(tid))
        self.assertTrue(store.claim_task(tid, force=True))
        out = research.run_task(d, None, tid, force=True,
                                distill_fn=lambda r, t: {
                                    "qa": [{"q": "Q?", "a": "A."}],
                                    "facts": []},
                                fetch_fn=lambda u: {
                                    "url": u, "title": "T", "text": "x",
                                    "links": [], "error": ""})
        self.assertEqual(out["status"], "done")

    def test_article_body_strips_wikipedia_nav_junk(self):
        # 2026-11-07: the Nobel distill failed three times because
        # the report's first 2000 chars were Wikipedia chrome —
        # "Jump to content", the search box, the 101-language list
        # — with no article text at all. The model read a language
        # list and returned no output.
        junk = ("Jump to content Search Search\n"
                "Nobel Prize in Chemistry\n"
                "101 languages\n"
                "Afrikaans\nAlemannisch\nالعربية\nAsturianu\n")
        lead = ("The Nobel Prize in Chemistry is awarded annually by the "
                "Royal Swedish Academy of Sciences to scientists in the "
                "various fields of chemistry. The 2024 Nobel Prize in "
                "Chemistry was awarded to David Baker, Demis Hassabis, "
                "and John Jumper for computational protein design and "
                "protein structure prediction, a breakthrough that has "
                "transformed biology and medicine worldwide.")
        body = research._article_body(junk + lead)
        self.assertTrue(body.startswith("The Nobel Prize in Chemistry"))
        self.assertNotIn("101 languages", body)
        self.assertIn("David Baker", body)


class WikiTableRowsTest(unittest.TestCase):
    """The 2024 Nobel winners live in a Wikipedia table, which the
    plain-text fetch drops entirely — the fetched prose names no
    winners at all. The article's wikitext still holds the table:
    _wiki_table_rows pulls the lines around the topic's keywords
    and cleans the markup. No live API in tests: fetch_json is
    injected with canned wikitext."""

    # A slice of the real List_of_Nobel_laureates_in_Chemistry
    # table, line-numbered so the 2023 block sits outside the
    # +20-line window of the 2024 hit (lines 0-24 kept, 25+ cut).
    WIKITEXT_LINES = [
        "The Nobel Prize in Chemistry was established in the 1895 will of Alfred Bernhard Nobel.",
        "== Laureates ==",
        '{| class="wikitable"',
        "|-",
        '|rowspan=3 id="2024" |2024',
        "|[[File:David Baker.jpg|75px]]",
        "|{{sortname|David|Baker}}<br>(1962–present)",
        "|{{flagdeco|United States}} American",
        '|rowspan=3 |"for computational protein design"',
        "|-",
        "|[[File:Demis Hassabis.jpg|75px]]",
        "|{{sortname|Demis|Hassabis}}<br>(1976–present)",
        "|{{flagdeco|United Kingdom}} British",
        '|"for protein structure prediction"',
        "|-",
        "|[[File:John Jumper.jpg|75px]]",
        "|{{sortname|John|Jumper}}<br>(1985–present)",
        "|{{flagdeco|United States}} American",
        '|"for protein structure prediction with AlphaFold2"',
        "|-",
        "Filler line one with no keywords at all.",
        "Filler line two with no keywords at all.",
        "Filler line three with no keywords at all.",
        "Filler line four with no keywords at all.",
        "Filler line five with no keywords at all.",
        '|rowspan=2 id="2023" |2023',
        "|[[File:Moungi Bawendi.jpg|75px]]",
        "|{{sortname|Moungi|Bawendi}}<br>(1961–present)",
        "|{{flagdeco|United States}} American",
        "|}",
    ]

    def _fake_fetch(self, url):
        return {"query": {"pages": {"123": {"revisions": [{"slots": {
            "main": {"*": "\n".join(self.WIKITEXT_LINES)}}}]}}}}

    def test_topic_keywords(self):
        keys = research._topic_keywords(
            "Who won the 2024 Nobel Prize in Chemistry?")
        self.assertIn("2024", keys)
        self.assertIn("Chemistry", keys)
        for stop in ("who", "won", "prize", "nobel"):
            self.assertNotIn(stop, [k.lower() for k in keys])

    def test_table_rows_from_canned_wikitext(self):
        rows = research._wiki_table_rows(
            "List of Nobel laureates in Chemistry",
            "Who won the 2024 Nobel Prize in Chemistry?",
            fetch_json=self._fake_fetch)
        for name in ("David Baker", "Demis Hassabis", "John Jumper"):
            self.assertIn(name, rows)
        self.assertIn("2024", rows)
        # The 2023 block sits outside the window.
        self.assertNotIn("Moungi Bawendi", rows)
        # No wikitext markup survives.
        self.assertNotIn("[[", rows)
        self.assertNotIn("{{", rows)
        self.assertNotIn("File:", rows)
        self.assertNotIn("rowspan", rows)
        self.assertLessEqual(len(rows), 3000)

    def test_table_rows_drops_title_words(self):
        # 2026-11-07 regression: "Chemistry" is a topic keyword AND
        # appears in the article title — on the real 96KB wikitext it
        # matches hundreds of early lines, so the 3000-char cap fills
        # with intro/template lines and the 2024 table rows never
        # survive (live probe: Baker/Hassabis/Jumper absent). Title
        # words are dropped, leaving the distinctive year anchor.
        filler = ["Chemistry is the science of matter and its changes. "
                  "The field covers organic and inorganic Chemistry."] * 60
        lines = filler + self.WIKITEXT_LINES
        fetch = lambda url: {"query": {"pages": {"123": {"revisions": [
            {"slots": {"main": {"*": "\n".join(lines)}}}]}}}}
        rows = research._wiki_table_rows(
            "List of Nobel laureates in Chemistry",
            "Who won the 2024 Nobel Prize in Chemistry?",
            fetch_json=fetch)
        for name in ("David Baker", "Demis Hassabis", "John Jumper"):
            self.assertIn(name, rows)
        self.assertLessEqual(len(rows), 3000)

    def test_year_block_is_exact(self):
        # 2026-11-07 regression: the keyword windows mixed in other
        # years' laureates and the distill called Richard Henderson
        # (2017) a 2024 winner. The year block holds exactly one
        # year's winners — Bawendi (2023) must not survive.
        rows = research._wiki_table_rows(
            "List of Nobel laureates in Chemistry",
            "Who won the 2024 Nobel Prize in Chemistry?",
            fetch_json=self._fake_fetch)
        for name in ("David Baker", "Demis Hassabis", "John Jumper"):
            self.assertIn(name, rows)
        self.assertNotIn("Moungi Bawendi", rows)

    def test_year_block_falls_back_without_year(self):
        # No year in the topic: the year block is skipped and the
        # keyword windows still find the winners (less precisely).
        rows = research._wiki_table_rows(
            "List of Nobel laureates in Chemistry",
            "Who won the Nobel Prize in Chemistry?",
            fetch_json=self._fake_fetch)
        for name in ("David Baker", "Demis Hassabis", "John Jumper"):
            self.assertIn(name, rows)

    def test_table_rows_empty_without_keywords(self):
        rows = research._wiki_table_rows(
            "List of Nobel laureates in Chemistry",
            "Who won the prize?", fetch_json=self._fake_fetch)
        self.assertEqual(rows, "")

    def test_table_rows_fail_soft(self):
        def boom(url):
            raise RuntimeError("api down")

        rows = research._wiki_table_rows(
            "List of Nobel laureates in Chemistry",
            "Who won the 2024 Nobel Prize in Chemistry?",
            fetch_json=boom)
        self.assertEqual(rows, "")

    def test_run_task_appends_table_rows_first(self):
        # The hook: a fetched wikipedia.org page gets its table
        # rows prepended to its contribution, ahead of the prose,
        # so even the 2000-char local distill slice sees them.
        old_rows = research._wiki_table_rows
        seen = {}
        try:
            def fake_rows(title, topic, fetch_json=None):
                seen["title"] = title
                return "2024\nDavid Baker (1962–present)"

            research._wiki_table_rows = fake_rows
            d = tempfile.mkdtemp()
            store = research.Store(d)
            tid = store.add_task(
                "research", "t", "Who won the 2024 Nobel Prize in Chemistry?",
                ["https://en.wikipedia.org/wiki/Nobel_Prize_in_Chemistry"])
            captured = {}

            def fake_fetch(url):
                return {"url": url, "title": "Nobel Prize in Chemistry",
                        "text": "The Nobel Prize in Chemistry is awarded "
                                "annually by the Royal Swedish Academy of "
                                "Sciences to scientists.",
                        "links": [], "error": ""}

            def fake_distill(report, topic):
                captured["report"] = report
                return {"qa": [{"q": "Who won? ", "a": "Baker."}],
                        "facts": []}

            out = research.run_task(d, None, tid, distill_fn=fake_distill,
                                    fetch_fn=fake_fetch)
            self.assertEqual(out["status"], "done")
            self.assertEqual(seen["title"], "Nobel Prize in Chemistry")
            report = captured["report"]
            self.assertIn("TABLE ROWS", report)
            self.assertIn("David Baker", report)
            self.assertLess(report.index("TABLE ROWS"),
                            report.index("awarded annually"))
        finally:
            research._wiki_table_rows = old_rows


class FocusedLocalReportTest(unittest.TestCase):
    """Local distill only: the full report drifts the small model
    off-topic (the Nobel task's 16 proposals were general Nobel
    facts + the 2026 winners, no 2024 winners). The local report
    leads with the topic's table rows and adds only a short lead of
    topic sentences — nothing else from the other pages' bodies.
    The cloud path keeps the full report."""

    TOPIC = "Who won the 2024 Nobel Prize in Chemistry?"
    ROWS = ("2024\nDavid Baker\nDemis Hassabis\nJohn Jumper\n"
            "for computational protein design")
    # The article body that makes the model drift: the 2026
    # winners, the 1901-2026 totals, the women winners, the 1901
    # winner — all real, none of it the 2024 answer.
    BODY = ("The Nobel Prize in Chemistry is awarded annually by the "
            "Royal Swedish Academy of Sciences. The 2026 Nobel Prize "
            "in Chemistry was awarded to Henri B. Kagan and Kenso "
            "Soai. From 1901 to 2026, the prize has been awarded to "
            "202 individuals. Eight women have won the prize, "
            "including Marie Curie in 1911. The first prize in 1901 "
            "went to Jacobus Henricus van 't Hoff.")
    NOISE = ("Kenso Soai", "Henri B. Kagan", "2026", "202 individuals",
             "Marie Curie", "van 't Hoff")

    def setUp(self):
        self._old_rows = research._wiki_table_rows
        research._wiki_table_rows = lambda t, topic, fetch_json=None: self.ROWS

    def tearDown(self):
        research._wiki_table_rows = self._old_rows

    def _run(self, cloud):
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task(
            "research", "t", self.TOPIC,
            ["https://en.wikipedia.org/wiki/Nobel_Prize_in_Chemistry",
             "https://en.wikipedia.org/wiki/List_of_Nobel_laureates_in_Chemistry",
             "https://example.com/nobel-prize-chemistry"])
        captured = {}

        def fake_fetch(url):
            if "List_of_Nobel" in url:
                return {"url": url, "title": "List of Nobel laureates",
                        "text": "The list of Nobel laureates in Chemistry.",
                        "links": [], "error": ""}
            if "wikipedia.org" in url:
                return {"url": url, "title": "Nobel Prize in Chemistry",
                        "text": self.BODY, "links": [], "error": ""}
            return {"url": url, "title": "Nobel Prize",
                    "text": "The 2026 Nobel Prize in Chemistry was "
                            "awarded to Henri B. Kagan and Kenso Soai. "
                            "The prize has been awarded to 202 "
                            "individuals from 1901 to 2026.",
                    "links": [], "error": ""}

        def fake_distill(report, topic):
            captured["report"] = report
            return {"qa": [{"q": "Who won? ", "a": "Baker."}],
                    "facts": []}

        out = research.run_task(d, None, tid, cloud=cloud,
                                distill_fn=fake_distill,
                                fetch_fn=fake_fetch)
        return out, captured["report"]

    def test_local_report_leads_with_year_rows_and_drops_noise(self):
        out, report = self._run(cloud=None)
        self.assertEqual(out["status"], "done")
        # The 2024 rows come first.
        self.assertIn("David Baker", report)
        self.assertIn("Demis Hassabis", report)
        self.assertIn("John Jumper", report)
        self.assertLess(report.index("David Baker"),
                        report.index("awarded annually"))
        # The general noise is absent — no 2026 winners, no totals,
        # no women winners, no 1901 winner, and nothing from the
        # second page's body at all.
        for noise in self.NOISE:
            self.assertNotIn(noise, report)
        # Short: rows + a one-sentence lead, not five pages.
        self.assertLess(len(report), 1200)

    def test_cloud_report_keeps_the_full_text(self):
        # The cloud path is unchanged: the full report (prose +
        # rows) reaches the distill, noise included.
        out, report = self._run(cloud={"provider": "nvidia",
                                       "api_key": "k", "model": "m"})
        self.assertEqual(out["status"], "done")
        self.assertIn("David Baker", report)
        for noise in ("Kenso Soai", "2026", "202 individuals"):
            self.assertIn(noise, report)

    def test_capped_rows_sets_are_dropped(self):
        # 2026-11-07 live failure: the focused report joined rows
        # from all five pages, and three of them were 3000-char
        # keyword-window sets (the main article's lead prose, the
        # controversies infobox, the Indian laureates prose) — the
        # 9342-char "focused" report was as noisy as the full one,
        # and the model answered the 2024 question with the 2026
        # winners. A rows set that hits the 3000-char cap is a loose
        # window match, not a precise year block: drop it, keep the
        # short sets (the 184-char 2024 block).
        old_rows = research._wiki_table_rows
        try:
            noise = ("NOISE-MARKER " + "The 2026 Nobel Prize in "
                     "Chemistry was awarded to Henri B. Kagan. ") * 40
            noise = noise[:3000]
            self.assertEqual(len(noise), 3000)

            def fake_rows(title, topic, fetch_json=None):
                if "List of Nobel" in title:
                    return self.ROWS
                return noise

            research._wiki_table_rows = fake_rows
            out, report = self._run(cloud=None)
            self.assertEqual(out["status"], "done")
            self.assertIn("David Baker", report)
            self.assertNotIn("NOISE-MARKER", report)
            self.assertNotIn("Henri B. Kagan", report)
            self.assertLess(len(report), 1200)
        finally:
            research._wiki_table_rows = old_rows


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


class SearchSourcesTest(unittest.TestCase):
    """Real web search for research topics: Wikipedia API first,
    Google second. No live network: the URL fetcher and the lynx
    harness are stubbed."""

    def setUp(self):
        self.orig_urlopen = research._urlopen
        self.orig_fetch = research._fetch_text
        self.orig_wiki = research._wikipedia_urls
        self.orig_google = research._google_urls

    def tearDown(self):
        research._urlopen = self.orig_urlopen
        research._fetch_text = self.orig_fetch
        research._wikipedia_urls = self.orig_wiki
        research._google_urls = self.orig_google

    def test_wikipedia_urls(self):
        import json

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return json.dumps({"query": {"search": [
                    {"title": "FidoNet"},
                    {"title": "Bulletin board system"}]}}).encode()

        research._urlopen = lambda req, timeout=None: FakeResp()
        urls = research._wikipedia_urls("fidonet")
        self.assertEqual(urls, [
            "https://en.wikipedia.org/wiki/FidoNet",
            "https://en.wikipedia.org/wiki/Bulletin_board_system"])

    def test_wikipedia_failure_is_empty(self):
        def boom(req, timeout=None):
            raise Exception("no network")

        research._urlopen = boom
        self.assertEqual(research._wikipedia_urls("fidonet"), [])

    def test_google_urls_filters_internals(self):
        research._fetch_text = lambda url: {
            "url": url, "title": "", "text": "", "error": "",
            "links": ["https://www.google.com/search?q=x",
                      "https://en.wikipedia.org/wiki/FidoNet",
                      "https://example.org/history"]}
        urls = research._google_urls("fidonet")
        self.assertEqual(urls, ["https://en.wikipedia.org/wiki/FidoNet",
                                "https://example.org/history"])

    def test_search_sources_wikipedia_first(self):
        calls = []
        research._wikipedia_urls = lambda topic, limit=5: [
            "https://w/%d" % i for i in range(5)]
        research._google_urls = lambda topic, limit=5: calls.append(1) or []
        urls = research.search_sources("fidonet")
        self.assertEqual(len(urls), 5)
        self.assertEqual(calls, [])

    def test_search_sources_tops_up_from_google(self):
        research._wikipedia_urls = lambda topic, limit=5: [
            "https://en.wikipedia.org/wiki/FidoNet"]
        research._google_urls = lambda topic, limit=5: [
            "https://example.org/history"]
        urls = research.search_sources("fidonet")
        self.assertEqual(urls, ["https://en.wikipedia.org/wiki/FidoNet",
                                "https://example.org/history"])

    def test_run_task_web_searches_when_no_urls(self):
        d = tempfile.mkdtemp()
        store = research.Store(d)
        tid = store.add_task("research", "fidonet", "fidonet", [])
        seen = []
        research._wikipedia_urls = lambda topic, limit=5: [
            "https://en.wikipedia.org/wiki/FidoNet"]

        def fake_fetch(url):
            seen.append(url)
            return {"url": url, "title": "FidoNet",
                    "text": "Fidonet began in 1984.", "links": [],
                    "error": ""}

        def fake_distill(report, topic):
            return {"qa": [], "facts": [], "model_used": "test"}

        out = research.run_task(d, None, tid, distill_fn=fake_distill,
                                fetch_fn=fake_fetch)
        self.assertEqual(out["status"], "done")
        self.assertEqual(seen, ["https://en.wikipedia.org/wiki/FidoNet"])


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
