"""HF models tests — stdlib unittest only, temp dirs only.

Covers hub/hfmodels.py: the GPU-memory estimate formula, quant parsing
from filenames, collection/repo listing parsing against mocked HF API
responses, local models scanning, and the token rule (a token passed to
a download is forwarded to the downloader but never appears in any job
record, file, or status payload). Never touches the real data/ or
models/ directories.
"""
import json
import os
import tempfile
import time
import unittest

import hub
from hub import hfmodels


class TestEstimate(unittest.TestCase):
    def test_formula_weights_plus_20pct(self):
        # 2 GiB file -> 2.4 GiB estimate (x1.2 overhead allowance).
        self.assertEqual(hfmodels.estimate_gpu_gb(2 * 1024 ** 3), 2.4)
        self.assertEqual(hfmodels.estimate_gpu_gb(0), 0.0)
        self.assertIsNone(hfmodels.estimate_gpu_gb(None))

    def test_formula_matches_documented_factor(self):
        self.assertEqual(hfmodels.OVERHEAD_FACTOR, 1.2)
        size = 5 * 1024 ** 3
        self.assertAlmostEqual(hfmodels.estimate_gpu_gb(size), 6.0)


class TestQuant(unittest.TestCase):
    def test_known_quants(self):
        self.assertEqual(hfmodels.parse_quant("model-Q4_K_M.gguf"), "Q4_K_M")
        self.assertEqual(hfmodels.parse_quant("model-Q8_0.gguf"), "Q8_0")
        self.assertEqual(hfmodels.parse_quant("model-F16.gguf"), "F16")
        self.assertEqual(hfmodels.parse_quant("model-Q5_K_S.gguf"), "Q5_K_S")

    def test_unknown_is_none_not_a_guess(self):
        self.assertIsNone(hfmodels.parse_quant("plain-model.gguf"))
        self.assertIsNone(hfmodels.parse_quant("model.gguf"))


class TestListingParsing(unittest.TestCase):
    def setUp(self):
        self._orig = hfmodels.hf_get_json

    def tearDown(self):
        hfmodels.hf_get_json = self._orig

    def test_collection_filters_models_only(self):
        def fake(url, token=None):
            return {"items": [
                {"type": "model", "id": "a/b"},
                {"type": "dataset", "id": "a/data"},
                {"type": "space", "id": "a/app"},
                {"type": "model", "id": "c/d"},
            ]}
        hfmodels.hf_get_json = fake
        self.assertEqual(hfmodels.list_collection("x/y"), [{"id": "a/b"}, {"id": "c/d"}])

    def test_model_detail_gguf_only_and_dashes(self):
        def fake(url, token=None):
            return {"id": "a/b", "downloads": 42, "lastModified": "2026-01-01",
                    "siblings": [
                        {"rfilename": "m-Q4_K_M.gguf", "size": 2147483648,
                         "lfs": {"oid": "abc123"}},
                        {"rfilename": "README.md", "size": 100},
                        {"rfilename": "n.gguf", "size": None},
                    ]}
        hfmodels.hf_get_json = fake
        d = hfmodels.model_detail("a/b")
        self.assertEqual(d["downloads"], 42)
        self.assertEqual(len(d["files"]), 2)
        self.assertEqual(d["files"][0]["quant"], "Q4_K_M")
        self.assertEqual(d["files"][0]["gpu_gb_est"], 2.4)
        self.assertEqual(d["files"][0]["sha256"], "abc123")
        self.assertIsNone(d["files"][1]["size"])
        self.assertIsNone(d["files"][1]["gpu_gb_est"])

    def test_model_detail_fills_sizes_from_tree(self):
        def fake(url, token=None):
            if url.endswith("/tree/main"):
                return [
                    {"type": "file", "path": "m-Q4_K_M.gguf", "size": 2147483648,
                     "lfs": {"oid": "tree-sha"}},
                ]
            return {"id": "a/b", "downloads": 1, "lastModified": "2026-01-01",
                    "siblings": [{"rfilename": "m-Q4_K_M.gguf"}]}
        hfmodels.hf_get_json = fake
        d = hfmodels.model_detail("a/b")
        self.assertEqual(d["files"][0]["size"], 2147483648)
        self.assertEqual(d["files"][0]["sha256"], "tree-sha")
        self.assertEqual(d["files"][0]["gpu_gb_est"], 2.4)

    def test_local_models_scans_dir_only(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "m-Q4_K_M.gguf"), "wb") as f:
                f.write(b"GGUF" + b"\x00" * 100)
            with open(os.path.join(td, "notes.txt"), "w") as f:
                f.write("x")
            hfmodels.models_dir = lambda: td
            try:
                got = hfmodels.local_models()
            finally:
                import hub.config as _cfg
                hfmodels.models_dir = _cfg.models_dir
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["name"], "m-Q4_K_M.gguf")
            self.assertEqual(got[0]["quant"], "Q4_K_M")


class TestTokenHandling(unittest.TestCase):
    def test_token_forwarded_but_never_stored(self):
        import hub.hfdownload as _hfdl
        seen = {}
        orig = _hfdl.download_model

        def fake_download(repo, filename, dest, size=None, sha256=None, token=None):
            seen["token"] = token
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(b"GGUF" + b"\x00" * 16)
            return dest, "mocked"
        _hfdl.download_model = fake_download
        with tempfile.TemporaryDirectory() as td:
            hfmodels.models_dir = lambda: td
            try:
                job = hfmodels.start_download("a/b", "m.gguf", token="SECRET-TOKEN-123")
                for _ in range(100):
                    st = hfmodels.job_status(job)
                    if st["state"] != "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(st["state"], "done")
                self.assertEqual(seen.get("token"), "SECRET-TOKEN-123")
                blob = json.dumps(st)
                self.assertNotIn("SECRET-TOKEN-123", blob)
                with open(os.path.join(td, "m.gguf"), "rb") as f:
                    self.assertNotIn(b"SECRET-TOKEN-123", f.read())
            finally:
                import hub.config as _cfg
                hfmodels.models_dir = _cfg.models_dir
                _hfdl.download_model = orig


class TestVersion(unittest.TestCase):
    def test_version_is_0_0_1(self):
        self.assertEqual(hub.__version__, "0.0.1")

    def test_ui_version_marker(self):
        ui = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "hub", "web_ui.html")
        with open(ui, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("HUB VERSION", html)
        self.assertIn("0.0.1", html)


if __name__ == "__main__":
    unittest.main()
