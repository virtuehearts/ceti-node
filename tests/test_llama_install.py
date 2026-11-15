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
        # Current release format: Linux packages are .tar.gz, not .zip.
        assets = [_asset("llama-b12192-bin-linux-cpu-x64.tar.gz"),
                  _asset("llama-b12192-bin-ubuntu-vulkan-x64.tar.gz")]
        self.assertEqual(llama_mod._pick_asset(assets, False, "x64")["name"],
                         "llama-b12192-bin-ubuntu-vulkan-x64.tar.gz")

    def test_linux_tarball_accepted_and_zip_still_works(self):
        # Old releases shipped Linux as .zip; both must be accepted.
        self.assertEqual(
            llama_mod._pick_asset([_asset("llama-b12192-bin-ubuntu-vulkan-x64.tar.gz")], False, "x64")["name"],
            "llama-b12192-bin-ubuntu-vulkan-x64.tar.gz")
        self.assertEqual(
            llama_mod._pick_asset([_asset("llama-b12012-bin-ubuntu-vulkan-x64.zip")], False, "x64")["name"],
            "llama-b12012-bin-ubuntu-vulkan-x64.zip")

    def test_linux_plain_ubuntu_is_the_cpu_build(self):
        # The plain Ubuntu package has no 'cpu' in its name
        # (llama-*-bin-ubuntu-x64.tar.gz) — it must still match the
        # cpu fallback, while accelerated builds must not.
        assets = [_asset("llama-b12192-bin-ubuntu-x64.tar.gz")]
        self.assertEqual(llama_mod._pick_asset(assets, False, "x64")["name"],
                         "llama-b12192-bin-ubuntu-x64.tar.gz")
        accel = [_asset("llama-b12192-bin-ubuntu-vulkan-x64.tar.gz")]
        self.assertEqual(llama_mod._pick_asset(accel, False, "x64")["name"],
                         "llama-b12192-bin-ubuntu-vulkan-x64.tar.gz")

    def test_linux_cpu_fallback_skips_accelerated_builds(self):
        # When only accelerated builds exist, the cpu fallback must
        # not grab one (it has an accelerator keyword) — the vulkan
        # preference above it wins instead.
        assets = [_asset("llama-b12192-bin-ubuntu-cuda-13.5-x64.tar.gz"),
                  _asset("llama-b12192-bin-ubuntu-rocm-10.0-x64.tar.gz")]
        self.assertEqual(llama_mod._pick_asset(assets, False, "x64")["name"],
                         "llama-b12192-bin-ubuntu-cuda-13.5-x64.tar.gz")


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

    def _fake_tar_download(self, url, dest):
        # A real .tar.gz in the current llama.cpp layout: the binary
        # (and its .so libs) live inside a llama-bXXXX/ subfolder,
        # not at the top level. install_server must flatten it.
        import tarfile
        import io
        self.downloaded.append(url)
        data = b"#!/bin/sh\necho fake\n"
        lib = b"fake lib\n"
        with tarfile.open(dest, "w:gz") as t:
            for name, blob in (("llama-b99999/llama-server", data),
                               ("llama-b99999/libllama.so", lib)):
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                info.mode = 0o755
                t.addfile(info, io.BytesIO(blob))

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

    def test_tarball_install_flattens_subfolder(self):
        # 0.0.1xa laptop crash: the Linux .tar.gz extracts to
        # bin/llama-bXXXX/ (binary + libs inside), not flat. The
        # binary must end up at bin/llama-server with its libs
        # beside it, or find_server() never sees it.
        import urllib.request
        llama_mod._github_json = lambda url: {"tag_name": "b12192", "assets": [
            _asset("llama-b12192-bin-ubuntu-vulkan-x64.tar.gz")]}
        old_retrieve = urllib.request.urlretrieve
        urllib.request.urlretrieve = self._fake_tar_download
        try:
            server = llama_mod.install_server()
        finally:
            urllib.request.urlretrieve = old_retrieve
        self.assertTrue(server.endswith("llama-server"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "llama-server")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "libllama.so")))
        self.assertFalse(os.path.isdir(os.path.join(self.tmp.name, "llama-b99999")))


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
