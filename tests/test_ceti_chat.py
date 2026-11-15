"""Tests for the CETI chat ladder (hub/ceti_chat.py -> hub/reasoning.py)
+ memory links.

Fixtures build a fake CETI data dir (one cassette with a category,
a wildcard category and <fact> elements, including Japan/France facts
for the predicate/composition tests; a facts.sqlite3 row) and point
BRAIN_CETI_DIR at it. Local-model and vector tiers are monkeypatched
on the reasoning layer — no servers, no model, no network.
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
from hub import memory, reasoning


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
               "<fact subject=\"Japan\" predicate=\"capital\" object=\"Tokyo\"/>"
               "<fact subject=\"Japan\" predicate=\"currency\" object=\"Japanese yen\"/>"
               "<fact subject=\"Japan\" predicate=\"currency_code\" object=\"JPY\"/>"
               "<fact subject=\"France\" predicate=\"capital\" object=\"Paris\"/>"
               "<fact subject=\"France\" predicate=\"population\" object=\"69081996\"/>"
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
        # Reasoning-layer monkeypatch hooks (answer() now delegates
        # there; the old ceti_chat._memory_hits/_local_model hooks no
        # longer drive anything).
        cls.orig_vector = reasoning._vector_tier
        cls.orig_model = reasoning._model_tier

    @classmethod
    def tearDownClass(cls):
        reasoning._vector_tier = cls.orig_vector
        reasoning._model_tier = cls.orig_model
        if cls.old_env is None:
            os.environ.pop("BRAIN_CETI_DIR", None)
        else:
            os.environ["BRAIN_CETI_DIR"] = cls.old_env

    def _patch_reasoning(self, vector=None, model=None):
        if vector is not None:
            reasoning._vector_tier = vector
        if model is not None:
            reasoning._model_tier = model

    def _restore_reasoning(self):
        # NOTE: read the originals through the CLASS, not the instance:
        # a plain function stored as a class attribute binds as a
        # method when read via self (the test instance becomes arg 1,
        # so the restored tier then takes 2 args but gets 3).
        reasoning._vector_tier = type(self).orig_vector
        reasoning._model_tier = type(self).orig_model

    def test_db_fact_tier(self):
        r = ceti_chat.answer("what country is toronto in")
        self.assertEqual(r["tier"], "facts")
        self.assertIn("canada", r["answer"])
        self.assertIn("FACTS", r["thinking"])
        self.assertIn("toronto country canada", r["thinking"])

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

    def test_aiml_search_finds_fidonet_template(self):
        # Retrieval, not exact match: "tell me about fidonet" hits
        # no pattern, but the FIDONET category scores on the subject
        # word (pattern x4 + template x2 = 6).
        roots = brain.resolve_roots()
        res = reasoning._aiml_search(
            reasoning._content_words("tell me about fidonet"), roots)
        self.assertTrue(res)
        self.assertEqual(res[0]["cassette"], "one.aiml")
        self.assertEqual(res[0]["pattern"], "WHAT IS FIDONET")
        self.assertEqual(res[0]["score"], 6)
        self.assertEqual(res[0]["template"], "FidoNet is a BBS network.")

    def test_aiml_search_returns_nothing_unrelated(self):
        roots = brain.resolve_roots()
        res = reasoning._aiml_search(
            reasoning._content_words("what is the capital of Mars?"), roots)
        self.assertEqual(res, [])

    def test_aiml_search_fidonet_top_four(self):
        # Q14 regression (2026-11-06, twice): lexicon synonyms
        # crowded the atomic facts out — first 'tell' -> 'state'
        # let "CURRENT STATE OF ..." patterns score on the synonym,
        # then 'about' -> 'around' matched narrative "around 2014"
        # inside long telnet templates. Retrieval now scores on the
        # original question words only, so the top 4 are the summary
        # plus the three atomic facts.
        # 0.0.1n: the modem research bundle adds a verified FidoNet
        # category ("begun by Tom Jennings around Christmas 1983 ...")
        # that outranks the bare-number atomic fact 'Over 120,000',
        # so the top 4 are now that category, the summary, and two
        # atomic facts.
        roots = {"cassettes_dir": os.path.join(ROOT, "brain", "cassettes")}
        res = reasoning._aiml_search(
            reasoning._content_words("tell me about fidonet"), roots)
        self.assertEqual(
            [c["template"] for c in res],
            ["FidoNet, begun by Tom Jennings around Christmas 1983, let "
             "boards exchange mail in cheap night-time calls; by the "
             "early 1990s it had more than 20,000 boards.",
             "FidoNet is a network of interconnected BBSes that "
             "continues to operate with Telnet-based connections. It "
             "is a legacy of the bulletin board system era and "
             "represents a niche but enduring legacy of the internet.",
             "1984", "Over 40,000"])

    def test_aiml_search_real_cassettes_under_250ms(self):
        # Jero's budget: pure-Python retrieval over the real stores
        # must stay under 250ms (no embedding call, no server).
        import time
        roots = {"cassettes_dir": os.path.join(ROOT, "brain", "cassettes")}
        t0 = time.perf_counter()
        res = reasoning._aiml_search(
            reasoning._content_words("tell me about fidonet"), roots)
        dt = (time.perf_counter() - t0) * 1000
        self.assertLess(dt, 250, "AIML retrieval took %.1fms" % dt)
        templates = [c["template"] for c in res]
        self.assertIn("1984", templates)
        self.assertTrue(any("40,000" in t for t in templates))
        # No junk: every returned category is about FidoNet in its
        # pattern or its template (the atomic facts are bare numbers,
        # so the pattern is what names the subject).
        self.assertTrue(all(
            "fidonet" in (c["pattern"] + " " + c["template"]).lower()
            for c in res))

    def test_aiml_context_reaches_local_model(self):
        # The wiring: an open-ended question with no exact pattern,
        # no lexicon definition and no vector hit still reaches the
        # model — grounded in the AIML template, not on nothing.
        captured = {}

        def _capture(q, ctx):
            captured["sections"] = dict(ctx)
            return ({"tier": "local-model", "answer": "model says hi",
                     "evidence": "local model, grounded in: AIML CONTEXT",
                     "numbers_grounded": True},
                    ["LOCAL MODEL — answered"])
        self._patch_reasoning(
            vector=lambda q, h: (
                [], ["VECTOR MEMORY — no nodes above 0.35"]),
            model=_capture)
        try:
            r = ceti_chat.answer("tell me about fidonet")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "local-model")
        self.assertIn("AIML CONTEXT", captured["sections"])
        self.assertIn("FidoNet is a BBS network.",
                      captured["sections"]["AIML CONTEXT"])
        self.assertIn("AIML", r["thinking"])
        self.assertIn("fed to local model as context", r["thinking"])

    def test_math_tier(self):
        r = ceti_chat.answer("What is 17 * 23 + 41?")
        self.assertEqual(r["tier"], "math")
        self.assertEqual(r["answer"], "432")
        self.assertIn("MATH", r["thinking"])
        # Letter x as a multiplication sign (2026-11-06: "17x23+41"
        # used to answer 64 — the "17x" was dropped and 23+41 ran).
        r = ceti_chat.answer("What is 17x23+41?")
        self.assertEqual(r["tier"], "math")
        self.assertEqual(r["answer"], "432")
        r = ceti_chat.answer("What is 17 x 23 + 41?")
        self.assertEqual(r["tier"], "math")
        self.assertEqual(r["answer"], "432")
        r = ceti_chat.answer("What is 15% of 240?")
        self.assertEqual(r["tier"], "math")
        self.assertEqual(r["answer"], "36")

    def test_facts_predicate_aware(self):
        # The old ladder answered both of these with the capital.
        r = ceti_chat.answer("What is the currency of Japan?")
        self.assertEqual(r["tier"], "facts")
        self.assertIn("Japanese yen", r["answer"])
        self.assertNotIn("Tokyo", r["answer"])
        r = ceti_chat.answer("What is the population of France?")
        self.assertEqual(r["tier"], "facts")
        self.assertIn("69081996", r["answer"])
        self.assertNotIn("Paris", r["answer"])

    def test_facts_composition_two_hops(self):
        r = ceti_chat.answer(
            "What is the capital of the country whose currency is the yen?")
        self.assertEqual(r["tier"], "facts-composition")
        self.assertIn("Tokyo", r["answer"])
        self.assertIn("hop 1", r["evidence"])
        self.assertIn("hop 2", r["evidence"])

    def test_local_model_tier_carries_node_ids(self):
        # Hit text shares content words ("prior", "note") with the
        # question, so the empty-context guard lets it through.
        self._patch_reasoning(
            vector=lambda q, h: (
                [{"id": 7, "text": "prior note", "source": "chat",
                  "model": "test-model", "score": 0.9}],
                ["VECTOR MEMORY — 1 node(s) above 0.35: #7 (0.90)"]),
            model=lambda q, ctx: (
                {"tier": "local-model", "answer": "model says hi",
                 "evidence": "local model, grounded in: VECTOR MEMORY",
                 "numbers_grounded": True},
                ["LOCAL MODEL — answered"]))
        try:
            r = ceti_chat.answer("what does the prior note say")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "local-model")
        self.assertEqual(r["memories"][0]["id"], 7)
        self.assertFalse(r["cloud_suggested"])

    def test_cloud_suggested_never_calls_cloud(self):
        # No lexicon hit, no vector hits: the guard skips the model
        # entirely. The model mock raises if it is ever invoked, so
        # this test fails loudly if the guard is bypassed.
        def _boom(query, context_sections):
            raise AssertionError("model must not be called on empty context")
        self._patch_reasoning(
            vector=lambda q, h: (
                [], ["VECTOR MEMORY — no nodes above 0.35"]),
            model=_boom)
        try:
            r = ceti_chat.answer("something unanswerable locally")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "cloud-suggested")
        self.assertTrue(r["cloud_suggested"])

    def test_empty_context_guard_drops_irrelevant_hits(self):
        # The 2-row vector memory case: only irrelevant rows (about
        # Canada's capital) for a World Cup question. The model
        # function must NOT be called at all — patch the real
        # chat_complete path and record any call.
        from hub import servers
        calls = []
        orig_chat = servers.chat_complete

        def _recording(messages, **kw):
            calls.append(messages)
            return "should never be reached", ""

        servers.chat_complete = _recording
        self._patch_reasoning(
            vector=lambda q, h: (
                [{"id": 1, "text": "User: capital of canada",
                  "source": "ceti", "model": "CETI", "score": 0.613},
                 {"id": 2, "text": "Assistant: Canada — capital: Ottawa",
                  "source": "ceti", "model": "CETI", "score": 0.591}],
                ["VECTOR MEMORY — 2 node(s) above 0.35: #1 (0.61), #2 (0.59)"]))
        try:
            r = reasoning.answer_with_reasoning(
                "Who won the 2026 World Cup?", lexicon={})
        finally:
            servers.chat_complete = orig_chat
            self._restore_reasoning()
        self.assertEqual(r["tier"], "cloud-suggested")
        self.assertEqual(calls, [])
        self.assertIn("skipped", r["reasoning_trace"][-2].lower()
                      if len(r["reasoning_trace"]) >= 2 else "")
        self.assertTrue(any("dropped as irrelevant" in s
                            for s in r["reasoning_trace"]))

    def test_no_brain_data_still_honest(self):
        empty = os.path.join(TMP, "empty")
        os.makedirs(empty, exist_ok=True)
        roots = brain.resolve_roots(candidates=[empty])
        self.assertFalse(roots["found"])
        r = ceti_chat.answer("hello", roots=roots)
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
        def _boom(query, context_sections):
            raise AssertionError("model must not be called on empty context")
        self._patch_reasoning(
            vector=lambda q, h: (
                [], ["VECTOR MEMORY — no nodes above 0.35"]),
            model=_boom)
        try:
            r = ceti_chat.answer("something unanswerable locally")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "cloud-suggested")
        for layer in ("MATH", "FACTS", "AIML", "LEXICON", "VECTOR MEMORY",
                      "LOCAL MODEL", "CLOUD"):
            self.assertIn(layer, r["thinking"])
        self.assertIn("never called", r["thinking"])

    def test_local_model_thinking_names_context_nodes(self):
        self._patch_reasoning(
            vector=lambda q, h: (
                [{"id": 7, "text": "prior note", "source": "chat",
                  "model": "test-model", "score": 0.9}],
                ["VECTOR MEMORY — 1 node(s) above 0.35: #7 (0.90)"]),
            model=lambda q, ctx: (
                {"tier": "local-model", "answer": "model says hi",
                 "evidence": "local model, grounded in: VECTOR MEMORY",
                 "numbers_grounded": True},
                ["LOCAL MODEL — answered; numbers guard passed"]))
        try:
            r = ceti_chat.answer("what does the prior note say")
        finally:
            self._restore_reasoning()
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

    def test_record_answer_memory_accepts_math_and_composition(self):
        d = tempfile.mkdtemp()

        def fake_embed(text):
            return [1.0, 0.0]

        def connect_fn(_d):
            return memory.connect(_d)

        ok_math = ceti_chat.record_answer_memory(
            d, "h2", "What is 17 * 23 + 41?", "432", "math",
            embed_fn=fake_embed, connect_fn=connect_fn)
        self.assertTrue(ok_math)
        ok_comp = ceti_chat.record_answer_memory(
            d, "h2", "capital of the country whose currency is the yen",
            "Japan — capital: Tokyo", "facts-composition",
            embed_fn=fake_embed, connect_fn=connect_fn)
        self.assertTrue(ok_comp)
        ok_cloud = ceti_chat.record_answer_memory(
            d, "h2", "who won the 2026 world cup",
            "I don't know", "cloud-suggested",
            embed_fn=fake_embed, connect_fn=connect_fn)
        self.assertFalse(ok_cloud)
        conn = memory.connect(d)
        try:
            self.assertEqual(memory.count(conn, "h2"), 4)
        finally:
            conn.close()

    def test_lexicon_missing_dir_is_seed_only(self):
        self.assertEqual(reasoning.get_lexicon({"ceti_dir": "/nonexistent"}), {})
        self.assertEqual(reasoning.get_lexicon({}), {})
        expanded, expansions, _defs = reasoning.expand_query(
            "tell me about dial-up forums", lexicon={})
        self.assertIn("forum", expansions)

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


    def test_clear_fact_hit_never_calls_model(self):
        # A single clear fact returns directly: the arbitration
        # model must not be consulted at all.
        def _boom(query, context_sections, **kw):
            raise AssertionError("model must not be called on a clear hit")
        self._patch_reasoning(model=_boom)
        try:
            r = ceti_chat.answer("What is the currency of Japan?")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "facts")
        self.assertIn("Japanese yen", r["answer"])

    def test_clear_aiml_hit_never_calls_model(self):
        def _boom(query, context_sections, **kw):
            raise AssertionError("model must not be called on a clear hit")
        self._patch_reasoning(model=_boom)
        try:
            r = ceti_chat.answer("what is fidonet")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "aiml")
        self.assertIn("BBS network", r["answer"])

    def test_ambiguous_facts_arbitrate(self):
        # "What is Japan?": no predicate word, three Japan facts
        # compete. The local model arbitrates over all three instead
        # of the ladder taking the first. (An open-ended "Tell me
        # about Japan" now elaborates instead — see
        # test_tell_me_about_elaborates_over_competing_facts.)
        seen = {}

        def _arb(query, sections, system=None, max_tokens=None,
                 reasoning=None):
            seen["sections"] = dict(sections)
            seen["system"] = system
            seen["reasoning"] = reasoning
            return ({"tier": "local-model",
                     "answer": "Japan's capital is Tokyo.",
                     "evidence": "local model, grounded in: CANDIDATE FACTS",
                     "numbers_grounded": True},
                    ["LOCAL MODEL — answered"])
        self._patch_reasoning(
            vector=lambda q, h: ([], ["VECTOR MEMORY — no nodes above 0.35"]),
            model=_arb)
        try:
            r = ceti_chat.answer("What is Japan?")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "arbitrated")
        self.assertIn("Tokyo", r["answer"])
        self.assertIn("ARBITRATE", r["thinking"])
        facts = seen["sections"].get("CANDIDATE FACTS", "")
        for obj in ("Tokyo", "Japanese yen", "JPY"):
            self.assertIn(obj, facts)
        # Arbitration is selection, not deep reasoning: thinking off.
        self.assertIs(seen["reasoning"], False)
        self.assertIn("Use ONLY the knowledge below", seen["system"])

    def test_arbitration_falls_through_when_model_fails(self):
        # Ambiguous facts, but the model is down: the ladder must
        # fall through to the lower tiers instead of failing.
        self._patch_reasoning(
            vector=lambda q, h: ([], ["VECTOR MEMORY — no nodes above 0.35"]),
            model=lambda q, ctx, **kw: (None, ["LOCAL MODEL — unavailable"]))
        try:
            r = ceti_chat.answer("Tell me about Japan")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "cloud-suggested")
        self.assertTrue(r["cloud_suggested"])

    def test_aiml_all_matches_returns_every_match(self):
        # Two overlapping patterns both match; the helper returns
        # both instead of only the first.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "cassettes", "two.aiml"),
                   "<aiml>"
                   "<category><pattern>HELLO *</pattern>"
                   "<template>Hi!</template></category>"
                   "<category><pattern>HELLO THERE *</pattern>"
                   "<template>Hello there!</template></category>"
                   "</aiml>")
            roots = brain.resolve_roots(candidates=[d])
            got = ceti_chat._aiml_all_matches("hello there friend", roots)
        self.assertEqual(len(got), 2)
        self.assertEqual([g["pattern"] for g in got],
                         ["HELLO *", "HELLO THERE *"])
        self.assertIn("Hello there!", [g["answer"] for g in got])

    def test_ambiguous_aiml_patterns_arbitrate(self):
        # Two AIML patterns match and no fact competes: the model
        # arbitrates over both templates.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, "cassettes", "two.aiml"),
                   "<aiml>"
                   "<category><pattern>HELLO *</pattern>"
                   "<template>Hi!</template></category>"
                   "<category><pattern>HELLO THERE *</pattern>"
                   "<template>Hello there!</template></category>"
                   "</aiml>")
            roots = brain.resolve_roots(candidates=[d])
            seen = {}

            def _arb(query, sections, system=None, max_tokens=None,
                     reasoning=None):
                seen["sections"] = dict(sections)
                return ({"tier": "local-model",
                         "answer": "Hello there!",
                         "evidence": "local model, grounded in: "
                                     "CANDIDATE AIML TEMPLATES",
                         "numbers_grounded": True},
                        ["LOCAL MODEL — answered"])
            self._patch_reasoning(
                vector=lambda q, h: ([], ["VECTOR MEMORY — no nodes above 0.35"]),
                model=_arb)
            try:
                r = ceti_chat.answer("hello there friend", roots=roots)
            finally:
                self._restore_reasoning()
        self.assertEqual(r["tier"], "arbitrated")
        self.assertIn("Hello there!", r["answer"])
        templates = seen["sections"].get("CANDIDATE AIML TEMPLATES", "")
        self.assertIn("HELLO *", templates)
        self.assertIn("HELLO THERE *", templates)

    # ---- Tier 1c elaboration (2026-11-14) ----
    def _fidonet_roots(self, d):
        _write(os.path.join(d, "cassettes", "fidonet.aiml"),
               "<aiml>"
               "<category><pattern>WHAT IS FIDONET</pattern>"
               "<template>FidoNet is a BBS network that began in 1984."
               "</template></category>"
               "<fact subject=\"FidoNet\" predicate=\"begun\" "
               "object=\"by Tom Jennings around Christmas 1983\"/>"
               "</aiml>")
        return brain.resolve_roots(candidates=[d])

    def test_tell_me_more_elaborates_on_fact_hit(self):
        # The 2026-11-14 bug, from Jero's screenshot: "tell me more
        # about fidonet" hit one fact and returned it verbatim — the
        # local model never got the hit knowledge to build on. Now
        # Tier 1c feeds the matched fact + related AIML templates to
        # the local model and it composes the fuller answer.
        with tempfile.TemporaryDirectory() as d:
            roots = self._fidonet_roots(d)
            seen = {}

            def _elab(query, sections, system=None, max_tokens=None,
                      reasoning=None):
                seen["sections"] = dict(sections)
                seen["system"] = system
                seen["reasoning"] = reasoning
                return ({"tier": "local-model",
                         "answer": "FidoNet began when Tom Jennings "
                                   "started it around Christmas 1983, "
                                   "and it grew into a BBS network.",
                         "evidence": "local model, grounded in: "
                                     "MATCHED FACTS",
                         "numbers_grounded": True},
                        ["LOCAL MODEL — answered"])
            self._patch_reasoning(
                vector=lambda q, h: ([], ["VECTOR MEMORY — no nodes above 0.35"]),
                model=_elab)
            try:
                r = ceti_chat.answer("tell me more about fidonet",
                                     roots=roots)
            finally:
                self._restore_reasoning()
        self.assertEqual(r["tier"], "elaborated")
        self.assertIn("Tom Jennings", r["answer"])
        self.assertIn("ELABORATE", r["thinking"])
        facts = seen["sections"].get("MATCHED FACTS", "")
        self.assertIn("Tom Jennings", facts)
        templates = seen["sections"].get("RELATED AIML TEMPLATES", "")
        self.assertIn("BBS network", templates)
        self.assertIn("ONLY the knowledge below", seen["system"])
        # Elaboration is composition, not deep reasoning: thinking off.
        self.assertIs(seen["reasoning"], False)

    def test_elaboration_falls_back_to_fact_when_model_fails(self):
        # Model down on an elaboration question: the bare fact still
        # answers, tier "facts" — elaboration never fails the question.
        with tempfile.TemporaryDirectory() as d:
            roots = self._fidonet_roots(d)
            self._patch_reasoning(
                vector=lambda q, h: ([], ["VECTOR MEMORY — no nodes above 0.35"]),
                model=lambda q, ctx, **kw: (None, ["LOCAL MODEL — unavailable"]))
            try:
                r = ceti_chat.answer("tell me more about fidonet",
                                     roots=roots)
            finally:
                self._restore_reasoning()
        self.assertEqual(r["tier"], "facts")
        self.assertIn("Tom Jennings", r["answer"])

    def test_precise_fact_question_still_returns_directly(self):
        # No elaboration phrase, so the bare fact returns with zero
        # model tokens — even though the hit is subject-only. The
        # model mock raises if it is ever invoked.
        def _boom(query, context_sections, **kw):
            raise AssertionError("model must not be called on a precise hit")
        with tempfile.TemporaryDirectory() as d:
            roots = self._fidonet_roots(d)
            self._patch_reasoning(model=_boom)
            try:
                r = ceti_chat.answer("when did fidonet start operating",
                                     roots=roots)
            finally:
                self._restore_reasoning()
        self.assertEqual(r["tier"], "facts")
        self.assertIn("Tom Jennings", r["answer"])

    def test_tell_me_about_elaborates_over_competing_facts(self):
        # Open-ended "Tell me about Japan": three facts compete, so
        # the old ladder arbitrated (picked one). Now the sysop asked
        # for MORE — the model composes from all three instead.
        seen = {}

        def _elab(query, sections, system=None, max_tokens=None,
                  reasoning=None):
            seen["sections"] = dict(sections)
            return ({"tier": "local-model",
                     "answer": "Japan's capital is Tokyo and its "
                               "currency is the Japanese yen (JPY).",
                     "evidence": "local model, grounded in: MATCHED FACTS",
                     "numbers_grounded": True},
                    ["LOCAL MODEL — answered"])
        self._patch_reasoning(
            vector=lambda q, h: ([], ["VECTOR MEMORY — no nodes above 0.35"]),
            model=_elab)
        try:
            r = ceti_chat.answer("Tell me about Japan")
        finally:
            self._restore_reasoning()
        self.assertEqual(r["tier"], "elaborated")
        self.assertIn("Tokyo", r["answer"])
        self.assertIn("Japanese yen", r["answer"])
        self.assertIn("ELABORATE", r["thinking"])
        facts = seen["sections"].get("MATCHED FACTS", "")
        for obj in ("Tokyo", "Japanese yen", "JPY"):
            self.assertIn(obj, facts)


class AttachmentContextTest(unittest.TestCase):
    """Session attachments ground the ladder: a question answerable
    only from an attached file reaches the local model (never
    cloud-suggested), with the file's text in the model context."""

    IDENT = "test-ident-attach"
    SESSION = "sess-1"
    VEC = [1.0, 0.0, 0.0]

    @classmethod
    def setUpClass(cls):
        # Rows live in the module TMP memory.db (HUB_DATA_DIR).
        # Pin HUB_DATA_DIR back to TMP here: other test modules
        # (test_nodeapi) set it at import time, and when they are
        # imported after this module the attachment lookups (which
        # read data_dir()) would land in the wrong database.
        cls.old_data_dir = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = TMP
        from hub import servers
        cls.orig_embed = servers.embed_text
        servers.embed_text = lambda text, port=None: list(cls.VEC)
        conn = memory.connect(TMP)
        try:
            memory.add(conn, cls.IDENT,
                       "Zorp blaster manual: turn the zorp dial clockwise.",
                       list(cls.VEC),
                       source="attachment:%s:zorp-manual.txt" % cls.SESSION,
                       tags="session:%s" % cls.SESSION, model="attachment")
            memory.add(conn, cls.IDENT,
                       "Unrelated decoy about baking bread.",
                       [0.0, 1.0, 0.0],
                       source="attachment:other-session:recipe.txt",
                       tags="session:other-session", model="attachment")
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        from hub import servers
        servers.embed_text = cls.orig_embed
        if cls.old_data_dir is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = cls.old_data_dir

    def setUp(self):
        self.orig_vector = reasoning._vector_tier
        self.orig_model = reasoning._model_tier

    def tearDown(self):
        reasoning._vector_tier = self.orig_vector
        reasoning._model_tier = self.orig_model

    def test_attachment_hits_session_only(self):
        hits, lines = reasoning._attachment_hits(
            "what does the zorp blaster manual say", self.IDENT,
            self.SESSION)
        self.assertEqual(len(hits), 1)
        self.assertIn("zorp dial", hits[0]["text"])
        self.assertIn("zorp-manual.txt", hits[0]["source"])

    def test_attachment_hits_empty_without_session(self):
        hits, _ = reasoning._attachment_hits(
            "what does the zorp blaster manual say", self.IDENT, None)
        self.assertEqual(hits, [])

    def test_session_file_grounds_model(self):
        seen = {}

        def fake_model(q, ctx):
            seen["ctx"] = ctx
            return ({"tier": "local-model",
                     "answer": "Turn the zorp dial clockwise.",
                     "evidence": "local model, grounded in: SESSION FILES",
                     "numbers_grounded": True},
                    ["LOCAL MODEL — answered"])

        reasoning._vector_tier = lambda q, h: ([], [])
        reasoning._model_tier = fake_model
        r = ceti_chat.answer("what does the zorp blaster manual say",
                             ident_hash=self.IDENT, session_id=self.SESSION)
        self.assertEqual(r["tier"], "local-model")
        self.assertIn("zorp dial", r["answer"])
        self.assertFalse(r["cloud_suggested"])
        sess = [b for t, b in seen["ctx"] if t == "SESSION FILES"]
        self.assertEqual(len(sess), 1)
        self.assertIn("zorp dial", sess[0])


if __name__ == "__main__":
    unittest.main()
