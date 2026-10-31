"""Beat-gate + output check tests. Stdlib unittest only, no servers,
no model. Two layers:

- TemporalMemory.check_beat / check_prose unit tests (fake identity,
  temp HUB_DATA_DIR): staged-vs-mention, framing downgrades,
  resolved reopen, possession/location warnings, empty store.
- StoryWriter integration (stubbed _complete, like
  test_story_temporal.py): blocked beats spend zero generation calls,
  flagged drafts are saved and never compiled, force paths work,
  checker errors fail open, toggle-off behaviour is unchanged.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub.story as story_mod
from hub import temporal as temporal_mod
from hub.story import StoryWriter
from hub.temporal import TemporalMemory, classify_mention


def _seed(tm, lines):
    return tm.record_events("block-001", lines, embed_fn=None)


class GateUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self.tmp.name
        os.environ.pop("HUB_TEMPORAL", None)
        self.tm = TemporalMemory("gate-unit", identity_hash="test")

    def tearDown(self):
        if self.old_data is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self.old_data
        os.environ.pop("HUB_TEMPORAL", None)
        self.tmp.cleanup()

    def test_staged_dead_beat_blocks(self):
        _seed(self.tm, ["Lena dies in the vault."])
        r = self.tm.check_beat("Lena argues with Jake about the vault code.")
        self.assertTrue(r["blocked"])
        self.assertTrue(any("Lena" in v and "dead" in v for v in r["violations"]))

    def test_object_position_staged_blocks(self):
        _seed(self.tm, ["Lena dies in the vault."])
        r = self.tm.check_beat("Jake talks to Lena.")
        self.assertTrue(r["blocked"])

    def test_mention_only_does_not_fire(self):
        _seed(self.tm, ["Lena dies in the vault."])
        for beat in ("Jake remembers Lena.",
                     "Jake visits the vault where Lena died.",
                     "Jake picks up Lena's key.",
                     "Jake buries Lena."):
            r = self.tm.check_beat(beat)
            self.assertFalse(r["blocked"], beat)
            self.assertEqual(r["violations"], [], beat)

    def test_flashback_and_dream_downgrade_to_warning(self):
        _seed(self.tm, ["Lena dies in the vault."])
        for beat in ("Flashback: Lena argues with Jake.",
                     "In a dream, Lena speaks to Jake."):
            r = self.tm.check_beat(beat)
            self.assertFalse(r["blocked"], beat)
            self.assertTrue(r["warnings"], beat)
            self.assertEqual(r["violations"], [], beat)

    def test_resolved_reopen_fires_and_memory_does_not(self):
        _seed(self.tm, ["Signal resolved."])
        r = self.tm.check_beat("Jake follows the signal again.")
        self.assertTrue(r["blocked"])
        self.assertTrue(any("RESOLVED" in v for v in r["violations"]))
        r2 = self.tm.check_beat("Jake remembers the signal.")
        self.assertFalse(r2["blocked"])
        self.assertEqual(r2["violations"], [])

    def test_possession_conflict_warns_not_blocks(self):
        # Lena must be ALIVE here, or the dead rule (a block) would
        # mask the possession warning being tested.
        _seed(self.tm, ["Jake takes the brass key.", "Lena arrives at the vault."])
        r = self.tm.check_beat("Lena holds the brass key.")
        self.assertFalse(r["blocked"])
        self.assertTrue(any("POSSESSION" in w and "Jake" in w for w in r["warnings"]))

    def test_possession_transfer_does_not_warn(self):
        _seed(self.tm, ["Jake takes the brass key.", "Lena arrives at the vault."])
        r = self.tm.check_beat("Lena takes the brass key from Jake.")
        self.assertFalse(r["blocked"])
        self.assertEqual([w for w in r["warnings"] if "POSSESSION" in w], [])

    def test_location_mismatch_warns_movement_does_not(self):
        _seed(self.tm, ["Jake arrives at the vault."])
        r = self.tm.check_beat("Jake is at the tower.")
        self.assertFalse(r["blocked"])
        self.assertTrue(any("LOCATION" in w for w in r["warnings"]))
        r2 = self.tm.check_beat("Jake leaves the vault.")
        self.assertEqual([w for w in r2["warnings"] if "LOCATION" in w], [])
        r3 = self.tm.check_beat("Jake is at the vault.")
        self.assertEqual(r3["warnings"], [])

    def test_output_check_catches_staged_prose(self):
        _seed(self.tm, ["Lena dies in the vault."])
        r = self.tm.check_prose("Jake walked into the vault alone. Lena walked in and said hello.")
        self.assertTrue(r["blocked"])
        r2 = self.tm.check_prose("Jake walked into the vault alone, thinking of Lena.")
        self.assertFalse(r2["blocked"])

    def test_empty_store_never_blocks(self):
        r = self.tm.check_beat("Lena argues with Jake.")
        self.assertFalse(r["blocked"])
        self.assertEqual(r["violations"], [])
        self.assertEqual(r["warnings"], [])

    def test_classify_mention_units(self):
        self.assertEqual(classify_mention("Lena argues with Jake.", "Lena"), "staged")
        self.assertEqual(classify_mention("Jake talks to Lena.", "Lena"), "staged")
        self.assertEqual(classify_mention("Lena is here.", "Lena"), "staged")
        self.assertEqual(classify_mention("Jake remembers Lena.", "Lena"), "mention")
        self.assertEqual(classify_mention("Lena died.", "Lena"), "mention")
        self.assertEqual(classify_mention("She argues with Jake.", "Lena"), "mention")  # pronouns invisible
        self.assertEqual(classify_mention("Jake, remembering Lena, opened the door.", "Lena"), "mention")


class GateStoryTest(unittest.TestCase):
    """StoryWriter integration with stubbed generation."""

    CLEAN_PROSE = "Jake stood in the vault doorway.\n\nMEMORY:\n- Jake opens the vault."
    VIOLATING_PROSE = ("Lena walked into the vault and said hello to Jake.\n\n"
                       "MEMORY:\n- Jake opens the vault.")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self.tmp.name
        os.environ.pop("HUB_TEMPORAL", None)
        self.story_id = "gate-story"
        d = os.path.join(self.tmp.name, "stories", self.story_id)
        os.makedirs(os.path.join(d, "blocks"))
        with open(os.path.join(d, "bible.md"), "w", encoding="utf-8") as f:
            f.write("# Test\n\n1. Lena argues with Jake about the vault code.\n2. Jake opens the vault.\n")
        with open(os.path.join(d, "summary.md"), "w", encoding="utf-8") as f:
            f.write("(no chapters written yet)\n")
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            f.write('{"genre": "mystery", "storyline": "vault", "perspective": "third person limited", "plot": "p", "rating": "PG-13"}')
        self.captured = []
        self.prose = self.CLEAN_PROSE
        self.orig_complete = story_mod._complete
        self.orig_embed = story_mod.embed_text

        def fake_complete(system, user, max_tokens, temperature, on_chunk=None):
            self.captured.append(user)
            return self.prose

        story_mod._complete = fake_complete
        story_mod.embed_text = lambda text: None
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

    def _seed_dead(self):
        TemporalMemory(self.story_id, identity_hash="").record_events(
            "block-001", ["Lena dies in the vault."], embed_fn=None)

    def _story_dir(self):
        return os.path.join(self.tmp.name, "stories", self.story_id)

    def test_toggle_off_violating_beat_generates_unchanged(self):
        self._seed_dead()
        # Toggle OFF: no gate keys, generation runs exactly as before.
        r = self.writer.next_block(self.story_id, pages=1)
        self.assertNotIn("blocked", r)
        self.assertNotIn("gate", r)
        self.assertTrue(self.captured)
        self.assertTrue(os.path.exists(os.path.join(self._story_dir(), "blocks", "01.md")))

    def test_blocked_beat_spends_zero_generation(self):
        self._seed_dead()
        os.environ["HUB_TEMPORAL"] = "1"
        r = self.writer.next_block(self.story_id, pages=1)
        self.assertTrue(r["blocked"])
        self.assertEqual(self.captured, [])
        self.assertFalse(os.path.exists(os.path.join(self._story_dir(), "blocks", "01.md")))

    def test_force_override_proceeds_past_gate(self):
        self._seed_dead()
        os.environ["HUB_TEMPORAL"] = "1"
        r = self.writer.next_block(self.story_id, pages=1, force=True)
        self.assertNotEqual(r.get("blocked"), True)
        self.assertTrue(self.captured)
        self.assertTrue(os.path.exists(os.path.join(self._story_dir(), "blocks", "01.md")))

    def test_output_check_flags_draft_not_compiled(self):
        # Clean beat (block 2's beat is "Jake opens the vault."): first
        # write a clean block 1 so block 2 is next, then violate in prose.
        os.environ["HUB_TEMPORAL"] = "1"
        r1 = self.writer.next_block(self.story_id, pages=1)
        self.assertTrue(os.path.exists(os.path.join(self._story_dir(), "blocks", "01.md")))
        # Seed dead Lena AFTER block 1 (block 2's beat is clean, but the
        # violating prose stages Lena, so the output check must fire).
        self._seed_dead()
        self.prose = self.VIOLATING_PROSE
        r2 = self.writer.next_block(self.story_id, pages=1)
        self.assertFalse(r2["accepted"])
        self.assertIn("flagged_draft", r2)
        self.assertTrue(os.path.exists(r2["flagged_draft"]))
        self.assertFalse(os.path.exists(os.path.join(self._story_dir(), "blocks", "02.md")))
        # Drafts live in a subdir compile()/status() never read.
        c = self.writer.compile(self.story_id)
        with open(c["md"], encoding="utf-8") as f:
            compiled = f.read()
        self.assertNotIn("said hello", compiled)
        self.assertEqual(self.writer.status(self.story_id)["blocks"], 1)

    def test_force_accepts_violating_prose(self):
        os.environ["HUB_TEMPORAL"] = "1"
        self._seed_dead()
        # Beat 1 is blocked by the gate, so force past it AND past the
        # output check: violating prose must land in the normal file.
        self.prose = self.VIOLATING_PROSE
        r = self.writer.next_block(self.story_id, pages=1, force=True)
        self.assertTrue(r.get("accepted", True))
        self.assertTrue(os.path.exists(os.path.join(self._story_dir(), "blocks", "01.md")))
        self.assertFalse(os.path.exists(os.path.join(self._story_dir(), "blocks", "drafts", "01.draft.md")))

    def test_checker_error_fails_open(self):
        os.environ["HUB_TEMPORAL"] = "1"
        orig_beat = temporal_mod.TemporalMemory.check_beat
        orig_prose = temporal_mod.TemporalMemory.check_prose
        temporal_mod.TemporalMemory.check_beat = lambda self, t: (_ for _ in ()).throw(RuntimeError("boom"))
        temporal_mod.TemporalMemory.check_prose = lambda self, t: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            r = self.writer.next_block(self.story_id, pages=1)
        finally:
            temporal_mod.TemporalMemory.check_beat = orig_beat
            temporal_mod.TemporalMemory.check_prose = orig_prose
        # Generation proceeded despite both checks exploding.
        self.assertTrue(self.captured)
        self.assertTrue(os.path.exists(os.path.join(self._story_dir(), "blocks", "01.md")))
        self.assertIn("failed", r["gate"]["note"])

    def test_chapter_part_blocked_and_flagged(self):
        os.environ["HUB_TEMPORAL"] = "1"
        self._seed_dead()
        # Chapter 1 beat 1 stages Lena -> part blocked, no part file.
        r = self.writer.write_chapter_part(self.story_id, 1, 1, 2, pages=1)
        self.assertTrue(r["blocked"])
        self.assertEqual(self.captured, [])
        cdir = os.path.join(self._story_dir(), "chapters", "ch01")
        self.assertFalse(os.path.exists(os.path.join(cdir, "part01.md")))
        # Force past the gate with violating prose -> accepted: a
        # single explicit force covers the whole operation (one human
        # decision for beat + output), by design. To reach a flagged
        # PART draft instead, use a clean beat + violating prose.
        with open(os.path.join(self._story_dir(), "bible.md"), "w", encoding="utf-8") as f:
            f.write("# Test\n\n1. Jake opens the vault.\n2. Jake leaves the vault.\n")
        self.prose = self.VIOLATING_PROSE
        r2 = self.writer.write_chapter_part(self.story_id, 1, 1, 2, pages=1)
        self.assertFalse(r2["accepted"])
        self.assertTrue(os.path.exists(r2["flagged_draft"]))
        self.assertFalse(os.path.exists(os.path.join(cdir, "part01.md")))
        with self.assertRaises(ValueError):
            self.writer.compile_chapter(self.story_id, 1)

    def test_cli_write_blocked_stops_and_force_proceeds(self):
        from hub.cli import cmd_story
        from hub.config import data_dir
        from hub.identity import ensure_identity
        # cmd_story builds its own writer from the real identity, so
        # seed the dead arc under THAT hash, not the "" test hash.
        ident, _ = ensure_identity(data_dir())
        TemporalMemory(self.story_id, identity_hash=ident.hash).record_events(
            "block-001", ["Lena dies in the vault."], embed_fn=None)
        os.environ["HUB_TEMPORAL"] = "1"
        args = SimpleNamespace(story_cmd="write", story_id=self.story_id,
                               pages=1, count=1, force=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_story(args)
        out = buf.getvalue()
        self.assertIn("GATE BLOCKED", out)
        self.assertEqual(self.captured, [])
        self.assertFalse(os.path.exists(os.path.join(self._story_dir(), "blocks", "01.md")))
        args.force = True
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_story(args)
        self.assertIn("block 1:", buf.getvalue())
        self.assertTrue(self.captured)

    def test_cli_gate_subcommand(self):
        from hub.cli import cmd_story
        from hub.config import data_dir
        from hub.identity import ensure_identity
        ident, _ = ensure_identity(data_dir())
        TemporalMemory(self.story_id, identity_hash=ident.hash).record_events(
            "block-001", ["Lena dies in the vault."], embed_fn=None)
        args = SimpleNamespace(story_cmd="gate", story_id=self.story_id, beat="")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_story(args)
        out = buf.getvalue()
        self.assertIn("BLOCKED", out)
        self.assertIn("Lena", out)


if __name__ == "__main__":
    unittest.main()
