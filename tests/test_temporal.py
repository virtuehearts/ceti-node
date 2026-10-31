"""Temporal lobe unit tests — stdlib unittest only, no servers, no model.

Fake embeddings are tiny deterministic topic vectors, so link
creation is fully predictable: events sharing a topic word link,
events on disjoint topics do not.

Run from the repo root (HUB_DATA_DIR is set to a temp dir per test):
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import temporal
from hub.temporal import TemporalMemory

TOPICS = {"vault": [1.0, 0.0, 0.0], "lena": [0.0, 1.0, 0.0], "signal": [0.0, 0.0, 1.0]}


def fake_emb(text):
    vec = [0.0, 0.0, 0.0]
    low = text.lower()
    for word, v in TOPICS.items():
        if word in low:
            for i in range(3):
                vec[i] += v[i]
    return vec


class TemporalTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self.tmp.name
        os.environ.pop("HUB_TEMPORAL", None)
        self.tm = TemporalMemory("test-story", identity_hash="test-ident")

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self.old_env
        os.environ.pop("HUB_TEMPORAL", None)
        self.tmp.cleanup()


class TestBucketing(TemporalTestBase):
    def test_day_partitions_and_ordering(self):
        self.tm.record_events("block-001", ["Lena enters the vault.", "Jake follows her."], embed_fn=None)
        self.tm.record_events("block-002", ["Lena dies in the vault."], embed_fn=None)
        day1 = self.tm.day_events("block-001")
        day2 = self.tm.day_events("block-002")
        self.assertEqual(len(day1), 2)
        self.assertEqual(len(day2), 1)
        self.assertEqual([e["text"] for e in day1],
                         ["Lena enters the vault.", "Jake follows her."])
        self.assertTrue(day1[0]["seq"] < day1[1]["seq"] < day2[0]["seq"])
        recent = self.tm.recent_events()
        self.assertEqual([e["text"] for e in recent],
                         ["Lena enters the vault.", "Jake follows her.", "Lena dies in the vault."])

    def test_story_day_index(self):
        self.tm.record_events("block-001", ["Day 3: Lena enters the vault."], embed_fn=None)
        self.tm.record_events("block-002", ["Jake follows her."], embed_fn=None,
                              story_day="day-4")
        by_day3 = self.tm.events_by_story_day("day-3")
        by_day4 = self.tm.events_by_story_day("day-4")
        self.assertEqual(len(by_day3), 1)
        self.assertEqual(len(by_day4), 1)
        self.assertEqual(by_day3[0]["story_day"], "day-3")
        self.assertEqual(by_day4[0]["story_day"], "day-4")

    def test_record_block_uses_beat_day(self):
        self.tm.record_block(7, "Day 4: the vault", ["Lena dies in the vault."], embed_fn=None)
        events = self.tm.day_events("block-007")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["story_day"], "day-4")


class TestLinks(TemporalTestBase):
    def test_link_creation_from_fake_embeddings(self):
        self.tm.record_events("block-001", ["Lena dies in the vault."], embed_fn=fake_emb)
        self.tm.record_events("block-002", ["Jake learns the vault code."], embed_fn=fake_emb)
        self.tm.record_events("block-003", ["The relay signal stays unanswered."], embed_fn=fake_emb)
        conn = temporal.connect(os.environ["HUB_DATA_DIR"])
        links = conn.execute("SELECT event_id, linked_event_id FROM temporal_links").fetchall()
        # Vault events share a topic (cos 0.707 > 0.5); signal is orthogonal.
        self.assertEqual(len(links), 1)
        texts = {e["id"]: e["text"] for e in self.tm.recent_events(10)}
        pair = {texts[links[0][0]], texts[links[0][1]]}
        self.assertEqual(pair, {"Lena dies in the vault.", "Jake learns the vault code."})

    def test_link_walk_bounded_and_ordered(self):
        # A~B (lena), B~C (vault), A not~C: a chain, not a clique.
        self.tm.record_events("block-001", ["Lena waits."], embed_fn=fake_emb)
        self.tm.record_events("block-002", ["Lena enters the vault."], embed_fn=fake_emb)
        self.tm.record_events("block-003", ["Jake learns the vault code."], embed_fn=fake_emb)
        events = self.tm.recent_events(10)
        ids = {e["text"]: e["id"] for e in events}
        one_hop = self.tm.walk([ids["Lena waits."]], max_hops=1)
        two_hop = self.tm.walk([ids["Lena waits."]], max_hops=2)
        self.assertEqual({e["text"] for e in one_hop}, {"Lena waits.", "Lena enters the vault."})
        self.assertEqual({e["text"] for e in two_hop},
                         {"Lena waits.", "Lena enters the vault.", "Jake learns the vault code."})
        seqs = [e["seq"] for e in two_hop]
        self.assertEqual(seqs, sorted(seqs))


class TestArcs(TemporalTestBase):
    def test_arc_transitions(self):
        self.tm.record_events("block-001", ["Lena dies in the vault."], embed_fn=None)
        self.tm.record_events("block-002", ["Jake learns the vault code."], embed_fn=None)
        self.tm.record_events("block-003", ["Jake promises to return."], embed_fn=None)
        arcs = {a["arc_key"]: a for a in self.tm.active_arcs()}
        self.assertEqual(arcs["Lena"]["status"], "resolved")
        self.assertEqual(arcs["Lena"]["state"], "dead")
        self.assertEqual(arcs["Jake"]["status"], "open")
        self.assertEqual(arcs["Jake"]["state"], "open")

    def test_death_is_sticky(self):
        self.tm.record_events("block-001", ["Lena dies in the vault."], embed_fn=None)
        self.tm.record_events("block-002", ["Lena smiles at Jake."], embed_fn=None)
        arcs = {a["arc_key"]: a for a in self.tm.active_arcs()}
        self.assertEqual(arcs["Lena"]["state"], "dead")
        self.assertEqual(arcs["Lena"]["status"], "resolved")


class TestDigests(TemporalTestBase):
    def test_compaction_keeps_protected_lines(self):
        lines = [
            "Lena dies in the vault.",
            "Jake learns the vault code.",
            "Jake takes the brass key.",
            "Jake arrives at the relay mast.",
            "The room is dusty and quiet.",
            "Someone hums an old song.",
        ]
        self.tm.record_events("block-001", lines, embed_fn=None)
        digest = self.tm.compact_day("block-001")
        for protected in lines[:4]:
            self.assertIn(protected, digest)
        self.assertIn("more events", digest)
        self.assertNotIn("Someone hums an old song.", digest)
        # Timeline for the compacted day uses the digest, not raw events.
        section = self.tm.timeline_section("Jake opens the vault.")
        self.assertIn("digest", section)
        self.assertIn("Lena dies in the vault.", section)
        self.assertNotIn("Someone hums an old song.", section)


class TestContradictions(TemporalTestBase):
    def test_flag_fires_and_not(self):
        self.tm.record_events("block-001", ["Lena dies in the vault."], embed_fn=None)
        flags = self.tm.check_contradictions("Lena argues with Jake about the code.")
        self.assertEqual(len(flags), 1)
        self.assertIn("Lena", flags[0])
        self.assertIn("dead", flags[0])
        self.assertEqual(self.tm.check_contradictions("Jake opens the vault alone."), [])
        self.assertEqual(self.tm.check_contradictions("The vault door creaks open."), [])


class TestGracefulDegradation(TemporalTestBase):
    def test_embed_none_records_and_backfills(self):
        res = self.tm.record_events("block-001", ["Lena dies in the vault."], embed_fn=lambda t: None)
        self.assertEqual(res["events"], 1)
        conn = temporal.connect(os.environ["HUB_DATA_DIR"])
        pending = conn.execute("SELECT COUNT(*) FROM temporal_events WHERE links_pending = 1").fetchone()[0]
        self.assertEqual(pending, 1)
        # Timeline still works with no embeddings at all.
        section = self.tm.timeline_section("Jake opens the vault.")
        self.assertIn("Lena dies in the vault.", section)
        # Backfill links once the server returns.
        self.tm.record_events("block-002", ["Jake learns the vault code."], embed_fn=lambda t: None)
        back = self.tm.backfill_links(fake_emb)
        self.assertEqual(back["backfilled"], 2)
        self.assertGreaterEqual(back["links"], 1)
        pending = conn.execute("SELECT COUNT(*) FROM temporal_events WHERE links_pending = 1").fetchone()[0]
        self.assertEqual(pending, 0)


class TestToggleAndBounds(TemporalTestBase):
    def test_toggle_env(self):
        self.assertFalse(temporal.temporal_enabled())
        os.environ["HUB_TEMPORAL"] = "1"
        self.assertTrue(temporal.temporal_enabled())
        os.environ["HUB_TEMPORAL"] = "0"
        self.assertFalse(temporal.temporal_enabled())

    def test_with_timeline_empty_is_identity(self):
        from hub.story import _BLOCK_USER, _with_timeline
        prompt = _BLOCK_USER.format(bible="B", summary="S", style="ST", n=1, beat="beat",
                                    pages=3, words=600, genre="g", perspective="p",
                                    rating="PG-13", plot="pl", canon="C",
                                    retrieved="R", tail="T")
        self.assertEqual(_with_timeline(prompt, ""), prompt)
        sectioned = _with_timeline(prompt, "ACTIVE ARCS: Lena (resolved, dead)")
        self.assertIn("TIMELINE", sectioned)
        self.assertLess(sectioned.index("TIMELINE"), sectioned.index("Write Chapter 1:"))

    def test_timeline_section_bounded(self):
        lines = [f"Jake notes detail number {i} in the vault." for i in range(30)]
        self.tm.record_events("block-001", lines, embed_fn=None)
        section = self.tm.timeline_section("Jake opens the vault.", max_chars=1200)
        self.assertLessEqual(len(section), 1200)


if __name__ == "__main__":
    unittest.main()
