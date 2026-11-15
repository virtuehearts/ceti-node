"""llama-server version gate tests (0.0.1e fail-closed fix). Stdlib
unittest only, no servers, no model, no network. find_server() is
tested with stubbed candidates/builds: the gate must skip binaries
that are too old AND binaries whose build cannot be determined —
the 0.0.1d laptop failure was a stale binary with an unparseable
--version sailing through on trust, after which autorun never
installed a fresh build because it thought a server existed.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub.llama as llama_mod


class FindServerTest(unittest.TestCase):
    def setUp(self):
        self._old_candidates = llama_mod._candidates
        self._old_build = llama_mod.server_build

    def tearDown(self):
        llama_mod._candidates = self._old_candidates
        llama_mod.server_build = self._old_build

    def _stub(self, builds):
        # builds: {path: build-or-None}; candidates yield in dict order.
        llama_mod._candidates = lambda: iter(builds.keys())
        llama_mod.server_build = lambda p: builds[p]

    def test_unknown_build_skipped(self):
        # The 0.0.1e fix: unknown build is fail-closed, not accepted.
        self._stub({"stale-server": None})
        self.assertIsNone(llama_mod.find_server())

    def test_old_build_skipped(self):
        self._stub({"old-server": 11200})
        self.assertIsNone(llama_mod.find_server())

    def test_new_build_accepted(self):
        self._stub({"new-server": 11941})
        self.assertEqual(llama_mod.find_server(), "new-server")

    def test_unknown_then_new_falls_through(self):
        self._stub({"stale-server": None, "new-server": 11941})
        self.assertEqual(llama_mod.find_server(), "new-server")

    def test_min_build_none_accepts_anything(self):
        # Chat-only escape hatch preserved: unknown build accepted
        # when the caller explicitly passes min_build=None.
        self._stub({"stale-server": None})
        self.assertEqual(llama_mod.find_server(min_build=None), "stale-server")

    def test_boundary_build_accepted(self):
        self._stub({"edge-server": 11630})
        self.assertEqual(llama_mod.find_server(), "edge-server")


class ServerBuildTest(unittest.TestCase):
    def test_version_on_stderr_is_read(self):
        # 0.0.1xa laptop failure (second half): llama-server prints
        # its version to stderr, not stdout. A stdout-only read
        # reports 'unknown build' and the fail-closed gate skips a
        # perfectly good binary, so the server is never found.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "llama-server")
            with open(p, "w") as f:
                f.write("#!/bin/sh\necho 'version: 0.9.0-dev (build 12192, commit 9f0e5098a)' >&2\n")
            os.chmod(p, 0o755)
            self.assertEqual(llama_mod.server_build(p), 12192)

    def test_version_on_stdout_still_works(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "llama-server")
            with open(p, "w") as f:
                f.write("#!/bin/sh\necho 'version: 0.7.0-dev (build 11941, commit x)'\n")
            os.chmod(p, 0o755)
            self.assertEqual(llama_mod.server_build(p), 11941)

    def test_no_version_output_is_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "llama-server")
            with open(p, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(p, 0o755)
            self.assertIsNone(llama_mod.server_build(p))


if __name__ == "__main__":
    unittest.main()
