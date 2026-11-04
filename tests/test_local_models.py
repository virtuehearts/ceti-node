"""Local chat model selection: override persistence, effective model
resolution, the System-tab list payload, and RAM reading.

The override lives in <data>/local-chat-model.json (operator-local,
never shipped); the model files live in models/, which the tests
redirect to a throwaway directory.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import config
from hub import hfmodels
from hub import web


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        self._tmp_data = tempfile.TemporaryDirectory()
        self._tmp_models = tempfile.TemporaryDirectory()
        self._old_data = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self._tmp_data.name
        self._old_cfg_models = config.models_dir
        self._old_hf_models = hfmodels.models_dir
        tmp = self._tmp_models.name
        config.models_dir = lambda: tmp
        hfmodels.models_dir = lambda: tmp
        # web.py bound models_dir at import; the payload helper reads
        # it through hfmodels + config, so patching those two suffices.
        self._seed("my-custom-Q4_K_M.gguf")
        self._seed("other.gguf")

    def tearDown(self):
        config.models_dir = self._old_cfg_models
        hfmodels.models_dir = self._old_hf_models
        if self._old_data is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self._old_data
        self._tmp_data.cleanup()
        self._tmp_models.cleanup()

    def _seed(self, name, size=1234):
        pth = os.path.join(self._tmp_models.name, name)
        with open(pth, "wb") as f:
            f.write(b"G" * size)
        return pth

    def test_default_without_override(self):
        eff = config.effective_chat_model()
        self.assertIsNotNone(eff)
        self.assertFalse(eff["overridden"])
        self.assertEqual(eff["file"],
                         config.default_model("chat")["file"])

    def test_select_custom_persists_and_wins(self):
        eff = config.select_local_model("my-custom-Q4_K_M.gguf")
        self.assertTrue(eff["overridden"])
        self.assertEqual(eff["file"], "my-custom-Q4_K_M.gguf")
        self.assertEqual(config.get_chat_model_override(),
                         "my-custom-Q4_K_M.gguf")
        self.assertTrue(config.effective_chat_model()["overridden"])

    def test_select_default_clears_override(self):
        config.select_local_model("my-custom-Q4_K_M.gguf")
        default_file = config.default_model("chat")["file"]
        self._seed(default_file)
        eff = config.select_local_model(default_file)
        self.assertFalse(eff["overridden"])
        self.assertIsNone(config.get_chat_model_override())

    def test_select_bad_names_rejected(self):
        for bad in ("", "../escape.gguf", "a/b.gguf", "model.txt",
                    "model.gguf/extra"):
            with self.assertRaises(ValueError, msg=bad):
                config.select_local_model(bad)

    def test_select_missing_file_rejected(self):
        with self.assertRaises(FileNotFoundError):
            config.select_local_model("not-here.gguf")

    def test_effective_falls_back_when_override_file_deleted(self):
        config.select_local_model("my-custom-Q4_K_M.gguf")
        os.remove(os.path.join(self._tmp_models.name,
                               "my-custom-Q4_K_M.gguf"))
        eff = config.effective_chat_model()
        self.assertFalse(eff["overridden"])
        self.assertEqual(eff["file"],
                         config.default_model("chat")["file"])

    def test_payload_merges_catalog_and_disk(self):
        payload = web._local_models_payload()
        by_file = {m["file"]: m for m in payload["models"]}
        default_file = config.default_model("chat")["file"]
        self.assertIn(default_file, by_file)
        self.assertEqual(by_file[default_file]["source"], "catalog")
        self.assertIn("my-custom-Q4_K_M.gguf", by_file)
        self.assertEqual(by_file["my-custom-Q4_K_M.gguf"]["source"], "disk")
        self.assertTrue(by_file[default_file]["active"])
        self.assertEqual(payload["active_file"], default_file)
        config.select_local_model("other.gguf")
        payload = web._local_models_payload()
        by_file = {m["file"]: m for m in payload["models"]}
        self.assertTrue(by_file["other.gguf"]["active"])
        self.assertFalse(by_file[default_file]["active"])
        self.assertTrue(payload["overridden"])

    def test_system_ram_sane(self):
        ram = web.system_ram()
        self.assertGreater(ram["total_mb"], 0)
        self.assertLessEqual(ram["free_mb"], ram["total_mb"])
        # Floor division on each field can leave a 1 MB rounding gap.
        self.assertLessEqual(abs(ram["used_mb"] + ram["free_mb"]
                                 - ram["total_mb"]), 1)


if __name__ == "__main__":
    unittest.main()
