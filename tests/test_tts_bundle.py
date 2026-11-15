#!/usr/bin/env python3
"""Tests for the bundled Mya TTS (tts/) in the Hub release tree.

Stdlib unittest only — no live server, no network, no model load.
Checks the bundle is present and complete, the voice registry holds
exactly mya-02 + mya-03 (no other voices, no admin credentials), and
the pack script includes tts/ so the next release ships it.
"""
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TTS = os.path.join(ROOT, "tts")


class TTSBundleTest(unittest.TestCase):
    def test_tts_dir_exists(self):
        self.assertTrue(os.path.isdir(TTS), "tts/ missing from the Hub tree")

    def test_app_code_present(self):
        for rel in ("app/main.py", "app/config.py", "app/db.py",
                    "app/api/tts.py", "app/api/health.py",
                    "app/api/voices.py",
                    "app/services/pockettts.py",
                    "app/services/voice_registry.py",
                    "app/templates/index.html", "app/static/styles.css",
                    "src/pockettts_bridge/cli.py"):
            self.assertTrue(os.path.isfile(os.path.join(TTS, rel)), rel)

    def test_no_pycache_in_bundle(self):
        for root, _dirs, files in os.walk(TTS):
            for name in files:
                self.assertFalse(name.endswith(".pyc"), name)
            self.assertNotIn("__pycache__", root)

    def test_support_files_present(self):
        for rel in ("requirements.txt", "Dockerfile", "pyproject.toml",
                    "README.md", "start_tts.sh", "start_tts.bat"):
            self.assertTrue(os.path.isfile(os.path.join(TTS, rel)), rel)

    def test_start_scripts_are_idempotent(self):
        # Both scripts guard the venv creation and the pip install
        # (marker file), so a second run is a no-op for setup.
        sh = open(os.path.join(TTS, "start_tts.sh")).read()
        self.assertIn(".tts-installed", sh)
        self.assertIn("if [ ! -x", sh)
        bat = open(os.path.join(TTS, "start_tts.bat")).read()
        self.assertIn(".tts-installed", bat)
        self.assertIn("if not exist", bat)

    def test_voices_present(self):
        for rel, min_bytes in (
                ("data/voices/mya-02.wav", 1_000_000),
                ("data/voices/mya-03.wav", 1_000_000),
                ("data/embeddings/mya-02.safetensors", 10_000_000),
                ("data/embeddings/mya-03.safetensors", 10_000_000)):
            p = os.path.join(TTS, rel)
            self.assertTrue(os.path.isfile(p), rel)
            self.assertGreaterEqual(os.path.getsize(p), min_bytes, rel)

    def test_no_other_voices_shipped(self):
        voices = sorted(os.listdir(os.path.join(TTS, "data", "voices")))
        self.assertEqual([v for v in voices if v.endswith(".wav")],
                         ["mya-02.wav", "mya-03.wav"])
        embs = sorted(os.listdir(os.path.join(TTS, "data", "embeddings")))
        self.assertEqual([e for e in embs if e.endswith(".safetensors")],
                         ["mya-02.safetensors", "mya-03.safetensors"])

    def test_voices_db_only_mya02_mya03(self):
        db = os.path.join(TTS, "data", "voices.db")
        self.assertTrue(os.path.isfile(db))
        conn = sqlite3.connect(db)
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM voices ORDER BY id")]
        self.assertEqual(ids, ["mya-02", "mya-03"])
        for row in conn.execute(
                "SELECT sample_path, embedding_path FROM voices"):
            self.assertTrue(row[0].startswith("data/voices/"))
            self.assertTrue(row[1].startswith("data/embeddings/"))
        # No admin credentials ship with the bundle.
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM app_config").fetchone()[0], 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0], 0)
        conn.close()

    def test_no_model_cache_or_output_shipped(self):
        cache = os.path.join(TTS, "data", "hf-cache")
        self.assertTrue(os.path.isdir(cache))
        self.assertEqual(
            [f for f in os.listdir(cache) if f != ".gitkeep"], [])
        out = os.path.join(TTS, "data", "output")
        self.assertTrue(os.path.isdir(out))
        self.assertEqual(
            [f for f in os.listdir(out) if f != ".gitkeep"], [])

    def test_pack_script_includes_tts(self):
        pack = os.path.join(
            os.path.dirname(ROOT), "pack_uncensored_node.py")
        src = open(pack).read()
        self.assertIn('"tts/"', src)
        self.assertIn('os.path.join(TREE, "tts")', src)

    def test_autorun_starts_tts_fail_soft(self):
        src = open(os.path.join(ROOT, "autorun.py")).read()
        self.assertIn("def start_tts", src)
        self.assertIn("def stop_tts", src)
        self.assertIn("start_tts(", src)
        # Fail-soft: a missing tts/ folder or a failed start never
        # blocks the Hub.
        self.assertIn("continuing without local TTS", src)


if __name__ == "__main__":
    unittest.main()
