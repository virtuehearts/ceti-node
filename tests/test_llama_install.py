"""llama-server install tests (0.0.1g nightly-aware discovery). Stdlib
unittest only, no network, no real download: the GitHub API and the
zip download are stubbed. Covers the 0.0.1f laptop crash — the
'latest' release is now a versioned stable (v0.8.0) whose only asset
is nightly-tag.txt, so discovery must fall through to the nightly
bXXXX releases, must never pick a cudart DLL bundle, and must pick
the right architecture.

Also covers ensure_servers tolerating a missing server binary per
server (chat/embed False) instead of raising out of autorun.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub.llama as llama_mod
import hub.servers as servers_mod


def _asset(name):
    return {"name": name, "browser_download_url": "https://example.invalid/" + name}


class PickAssetTest(unittest.TestCase):
    def test_vulkan_preferred_over_cuda_and_cpu(self):
        assets = [_asset("llama-b12012-bin-win-cpu-x64.zip"),
                  _asset("llama-b12012-bin-win-cuda-cu12.4-x64.zip"),
                  _asset("llama-b12012-bin-win-vulkan-x64.zip")]
        self.assertEqual(llama_mod._pick_asset(assets, True, "x64")["name"],
                         "llama-b12012-bin-win-vulkan-x64.zip")

    def test_cudart_bundle_never_wins(self):
        # cudart-*.zip matches naive 'cuda'+'win' but holds no server.
        assets = [_asset("cudart-llama-bin-win-cuda-12.4-x64.zip"),
                  _asset("llama-b12012-bin-win-cuda-cu12.4-x64.zip")]
        self.assertEqual(llama_mod._pick_asset(assets, True, "x64")["name"],
                         "llama-b12012-bin-win-cuda-cu12.4-x64.zip")

    def test_wrong_arch_excluded(self):
        assets = [_asset("llama-b12012-bin-win-cpu-arm64.zip")]
        self.assertIsNone(llama_mod._pick_asset(assets, True, "x64"))
        self.assertEqual(llama_mod._pick_asset(assets, True, "arm64")["name"],
                         "llama-b12012-bin-win-cpu-arm64.zip")

    def test_cpu_fallback_and_no_match(self):
        assets = [_asset("llama-b12012-bin-win-cpu-x64.zip")]
        self.assertEqual(llama_mod._pick_asset(assets, True, "x64")["name"],
                         "llama-b12012-bin-win-cpu-x64.zip")
        self.assertIsNone(llama_mod._pick_asset([_asset("nightly-tag.txt")], True, "x64"))
        self.assertIsNone(llama_mod._pick_asset([], True, "x64"))

    def test_linux_prefers_ubuntu_vulkan(self):
        assets = [_asset("llama-b12012-bin-linux-cpu-x64.zip"),
                  _asset("llama-b12012-bin-ubuntu-vulkan-x64.zip")]
        self.assertEqual(llama_mod._pick_asset(assets, False, "x64")["name"],
                         "llama-b12012-bin-ubuntu-vulkan-x64.zip")


class InstallServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_json = llama_mod._github_json
        self._old_bin = llama_mod.bin_dir
        llama_mod.bin_dir = lambda: self.tmp.name
        self.downloaded = []

    def tearDown(self):
        llama_mod._github_json = self._old_json
        llama_mod.bin_dir = self._old_bin
        self.tmp.cleanup()

    def _fake_download(self, url, dest):
        # A real zip holding a dummy server binary, as install_server
        # expects to extract from the downloaded asset.
        self.downloaded.append(url)
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("llama-server", "#!/bin/sh\necho fake\n")

    def _run_install(self, latest_assets, list_releases):
        import urllib.request
        calls = []

        def fake_json(url):
            calls.append(url)
            if url.endswith("/releases/latest"):
                return {"tag_name": "v0.8.0", "assets": latest_assets}
            return list_releases

        llama_mod._github_json = fake_json
        old_retrieve = urllib.request.urlretrieve
        urllib.request.urlretrieve = self._fake_download
        try:
            server = llama_mod.install_server()
        finally:
            urllib.request.urlretrieve = old_retrieve
        return server, calls

    def test_falls_through_to_nightly_releases(self):
        # 0.0.1f crash shape: latest has only nightly-tag.txt.
        nightly = [{"tag_name": "b12012", "assets": [
            _asset("cudart-llama-bin-win-cuda-12.4-x64.zip"),
            _asset("llama-b12012-bin-ubuntu-vulkan-x64.zip")]}]
        server, calls = self._run_install([_asset("nightly-tag.txt")], nightly)
        self.assertTrue(server.endswith("llama-server"))
        self.assertIn("vulkan", self.downloaded[0])
        self.assertEqual(len(calls), 2)

    def test_latest_with_binaries_wins_directly(self):
        latest = [_asset("llama-b12012-bin-ubuntu-vulkan-x64.zip")]
        server, calls = self._run_install(latest, [])
        self.assertTrue(server.endswith("llama-server"))
        self.assertEqual(len(calls), 1)

    def test_nothing_anywhere_raises(self):
        with self.assertRaises(RuntimeError):
            self._run_install([_asset("nightly-tag.txt")], [{"tag_name": "b12012", "assets": []}])


class EnsureServersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = os.path.join(self.tmp.name, "chat.gguf")
        self.emb = os.path.join(self.tmp.name, "emb.gguf")
        open(self.chat, "w").close()
        open(self.emb, "w").close()
        self._saved = {}
        for name in ("_health", "_start", "_wait_health", "_stop"):
            self._saved[name] = getattr(servers_mod, name)

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(servers_mod, name, fn)
        self.tmp.cleanup()

    def _stub(self, start_fn):
        servers_mod._health = lambda port: False
        servers_mod._start = start_fn
        servers_mod._wait_health = lambda port, timeout, proc=None: True
        servers_mod._stop = lambda port: False

    def test_missing_server_is_per_server_failure_not_raise(self):
        def boom(*a, **k):
            raise RuntimeError("no llama-server found; run: python -m hub doctor --install")
        self._stub(boom)
        self.assertEqual(
            servers_mod.ensure_servers(self.chat, self.emb, gpu=False, ctx=32768),
            (False, False))

    def test_chat_ok_embed_missing(self):
        def start(model, port, ctx, ngl, extra):
            if port == servers_mod.embed_port():
                raise RuntimeError("no llama-server found")
            return object()
        self._stub(start)
        self.assertEqual(
            servers_mod.ensure_servers(self.chat, self.emb, gpu=False, ctx=32768),
            (True, False))


if __name__ == "__main__":
    unittest.main()
