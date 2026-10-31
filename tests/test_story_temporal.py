"""Story.py integration: toggle-off prompt byte-identical, toggle-on
prompt gains a bounded timeline section, and the beat-gate blocks
violating beats before any generation call. No servers, no model:
hub.story._complete and hub.story.embed_text are stubbed, identity
is None (so rolling retrieval and style refs return early strings).

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub.story as story_mod
from hub.story import StoryWriter
from hub.temporal import TemporalMemory

FIXED_PROSE = ("Jake stood in the vault doorway.\n\n"
               "MEMORY:\n- Lena dies in the vault.\n- Jake learns the vault code.")


class StoryTemporalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self.tmp.name
        os.environ.pop("HUB_TEMPORAL", None)
        self.story_id = "test-integration"
        d = os.path.join(self.tmp.name, "stories", self.story_id)
        os.makedirs(os.path.join(d, "blocks"))
        with open(os.path.join(d, "bible.md"), "w", encoding="utf-8") as f:
            f.write("# Test\n\n1. Lena argues with Jake about the vault code.\n2. Jake opens the vault.\n")
        with open(os.path.join(d, "summary.md"), "w", encoding="utf-8") as f:
            f.write("(no chapters written yet)\n")
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            f.write('{"genre": "mystery", "storyline": "vault", "perspective": "third person limited", "plot": "p", "rating": "PG-13"}')
        self.captured = []
        self.orig_complete = story_mod._complete
        self.orig_embed = story_mod.embed_text

        def fake_complete(system, user, max_tokens, temperature, on_chunk=None):
            self.captured.append(user)
            return FIXED_PROSE

        story_mod._complete = fake_complete
        story_mod.embed_text = lambda text: None  # deterministic, no server
        self.writer = StoryWriter(ident=None)

    def tearDown(self):
        story_mod._complete = self.orig_complete
        story_mod.embed_text = self.orig_embed
        if self.old_data is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self.old_data
        os.environ.pop("HUB_TEMPORAL", None)
        self.tmp.cleanup()

    def _reset_block1(self):
        d = os.path.join(self.tmp.name, "stories", self.story_id)
        p = os.path.join(d, "blocks", "01.md")
        if os.path.exists(p):
            os.remove(p)
        with open(os.path.join(d, "summary.md"), "w", encoding="utf-8") as f:
            f.write("(no chapters written yet)\n")
        # The gate reads arcs, so a previous run's recorded footer
        # lines (e.g. a dead Lena) would poison the next part: wipe
        # this story's temporal rows too, not just the block file.
        from hub import temporal as temporal_mod
        conn = temporal_mod.connect(self.tmp.name)
        for table in ("temporal_events", "temporal_links", "temporal_arcs", "temporal_digests"):
            conn.execute(f"DELETE FROM {table} WHERE identity_hash = ? AND story_id = ?",
                         ("", self.story_id))
        conn.commit()

    def _chapter_prompt(self):
        # next_block also fires an expand call when prose is short;
        # the chapter prompt is the one carrying the bible.
        return next(p for p in reversed(self.captured) if "STORY BIBLE" in p)

    def test_toggle_off_identical_and_toggle_on_adds_bounded_section(self):
        # A: toggle off.
        self.writer.next_block(self.story_id, pages=1)
        prompt_off = self._chapter_prompt()
        self.assertNotIn("TIMELINE", prompt_off)
        self._reset_block1()
        # B: toggle on, empty temporal store -> identical prompt.
        os.environ["HUB_TEMPORAL"] = "1"
        self.writer.next_block(self.story_id, pages=1)
        prompt_on_empty = self._chapter_prompt()
        self.assertEqual(prompt_on_empty, prompt_off)
        self._reset_block1()
        # C: toggle on, seeded store -> timeline section, bounded growth.
        # Seeded line must NOT create a dead/resolved arc for a beat-1
        # entity, or the beat-gate (correctly) blocks before any prompt
        # exists — the gate-block case has its own test below.
        tm = TemporalMemory(self.story_id, identity_hash="")
        tm.record_events("block-001", ["Jake learns the vault code."], embed_fn=None)
        self.writer.next_block(self.story_id, pages=1)
        prompt_on_seeded = self._chapter_prompt()
        self.assertIn("TIMELINE", prompt_on_seeded)
        self.assertIn("Jake learns the vault code.", prompt_on_seeded)
        growth = len(prompt_on_seeded) - len(prompt_off)
        self.assertLessEqual(growth, 1500)
        # D: the seeded run also recorded its own chapter's events.
        events = tm.recent_events(10)
        self.assertGreaterEqual(len(events), 2)

    def test_dead_entity_beat_is_gate_blocked(self):
        # Supersedes the old flag-in-prompt test: a beat staging a dead
        # entity no longer reaches a prompt at all — the gate blocks
        # first, spending zero generation calls.
        tm = TemporalMemory(self.story_id, identity_hash="")
        tm.record_events("block-001", ["Lena dies in the vault."], embed_fn=None)
        os.environ["HUB_TEMPORAL"] = "1"
        # Beat 1 stages Lena ("Lena argues with Jake...").
        result = self.writer.next_block(self.story_id, pages=1)
        self.assertTrue(result["blocked"])
        self.assertIn("Lena", " ".join(result["violations"]))
        self.assertEqual(self.captured, [])  # _complete never called
        d = os.path.join(self.tmp.name, "stories", self.story_id)
        self.assertFalse(os.path.exists(os.path.join(d, "blocks", "01.md")))


if __name__ == "__main__":
    unittest.main()
