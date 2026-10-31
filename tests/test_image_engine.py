"""0.0.1d unit tests: image engine routing + announce engine tags.

Pure helpers only — no network, no list, no server.
"""

import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

import node as node_mod  # noqa: E402
import tunnel as tunnel_mod  # noqa: E402
from hub import nodeapi  # noqa: E402


class EngineOfferedTest(unittest.TestCase):
    def test_blank_means_node_default(self):
        self.assertTrue(tunnel_mod.engine_offered("", "z-image"))
        self.assertTrue(tunnel_mod.engine_offered(None, "z-image"))

    def test_exact_match_offered(self):
        self.assertTrue(tunnel_mod.engine_offered("z-image", "z-image"))
        self.assertTrue(tunnel_mod.engine_offered("stable-diffusion",
                                                  "stable-diffusion"))

    def test_mismatch_not_offered(self):
        self.assertFalse(tunnel_mod.engine_offered("stable-diffusion",
                                                   "z-image"))
        self.assertFalse(tunnel_mod.engine_offered("Z-Image", "z-image"))


class SanitizeEngineTest(unittest.TestCase):
    def test_valid_tags_pass(self):
        self.assertEqual(nodeapi._sanitize_engine("z-image"), "z-image")
        self.assertEqual(nodeapi._sanitize_engine("sd-xl_v1.0"), "sd-xl_v1.0")

    def test_blank_is_none(self):
        self.assertIsNone(nodeapi._sanitize_engine(""))
        self.assertIsNone(nodeapi._sanitize_engine("   "))
        self.assertIsNone(nodeapi._sanitize_engine(None))

    def test_bad_tags_rejected(self):
        self.assertIsNone(nodeapi._sanitize_engine("z image"))
        self.assertIsNone(nodeapi._sanitize_engine("z|image"))
        self.assertIsNone(nodeapi._sanitize_engine("x" * 41))


class AnnounceCapsTest(unittest.TestCase):
    def test_base_caps_unchanged_without_engines(self):
        self.assertEqual(node_mod._announce_caps(["text"], {}), ["text"])
        self.assertEqual(node_mod._announce_caps(["text"], None), ["text"])

    def test_engine_tags_appended(self):
        caps = node_mod._announce_caps(
            ["text", "tts", "image"],
            {"image_engines": ["z-image"], "tts_engine": "pockettts",
             "tts_voice": "mya-03"})
        self.assertEqual(caps, ["text", "tts", "image", "image-engine:z-image",
                                "tts-engine:pockettts", "tts-voice:mya-03"])

    def test_tags_deduped(self):
        caps = node_mod._announce_caps(
            ["text", "image-engine:z-image"],
            {"image_engines": ["z-image"]})
        self.assertEqual(caps, ["text", "image-engine:z-image"])


if __name__ == "__main__":
    unittest.main()
