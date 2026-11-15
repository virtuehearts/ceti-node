"""TTS server control + voice panel tests (0.0.1k). Stdlib unittest
only, no real server, no real bridge: subprocess and the bridge
health check are stubbed. Covers the System view's TTS SERVER row
(start/stop) and voice panel (list voices, set default voice):
- tts_voices() reads the bridge's voices.db, falls back to the two
  built-ins when the DB is missing/unreadable.
- tts_start() launches the start script and records the PID;
  tts_stop() kills it (and removes the PID file).
- set_default_voice() changes ONLY tts_voice, preserving the
  serving backend/model/endpoint (set_serving would clobber them).
- (0.0.1-beta) _tts_cmd() routes Windows through cmd /c — Popen on
  a bare .bat raises WinError 193, which silently broke the Hub's
  START button on the laptop. tts_log_tail() / tts_starting() feed
  the System view's STARTING state + bridge log, so a failed
  first-run install no longer looks like "still building".

Run from the repo root:
    python3 -m unittest tests.test_tts_control -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hub.servers as servers_mod
import hub.nodeapi as nodeapi_mod


class TtsVoicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_root = servers_mod.REPO_ROOT
        servers_mod.REPO_ROOT = self.tmp.name

    def tearDown(self):
        servers_mod.REPO_ROOT = self._old_root
        self.tmp.cleanup()

    def _write_db(self, rows):
        dbdir = os.path.join(self.tmp.name, "tts", "data")
        os.makedirs(dbdir)
        db = os.path.join(dbdir, "voices.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE voices (id TEXT PRIMARY KEY, name TEXT, type TEXT)")
        for r in rows:
            conn.execute("INSERT INTO voices VALUES (?,?,?)", r)
        conn.commit()
        conn.close()
        return db

    def test_reads_voices_db(self):
        self._write_db([("mya-03", "Mya 03", "builtin"),
                        ("mya-02", "Mya 02", "builtin"),
                        ("custom-1", "Custom One", "cloned")])
        got = servers_mod.tts_voices()
        self.assertEqual([v["id"] for v in got], ["custom-1", "mya-02", "mya-03"])
        self.assertEqual(got[0]["name"], "Custom One")

    def test_missing_db_falls_back_to_builtins(self):
        got = servers_mod.tts_voices()
        self.assertEqual([v["id"] for v in got], ["mya-03", "mya-02"])

    def test_empty_db_falls_back_to_builtins(self):
        self._write_db([])
        self.assertEqual([v["id"] for v in servers_mod.tts_voices()],
                         ["mya-03", "mya-02"])

    def test_corrupt_db_falls_back_to_builtins(self):
        dbdir = os.path.join(self.tmp.name, "tts", "data")
        os.makedirs(dbdir)
        with open(os.path.join(dbdir, "voices.db"), "w") as f:
            f.write("not a database")
        self.assertEqual([v["id"] for v in servers_mod.tts_voices()],
                         ["mya-03", "mya-02"])


class TtsStartStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_root = servers_mod.REPO_ROOT
        self._old_data = servers_mod.data_dir
        self._old_popen = servers_mod.subprocess.Popen
        self._old_run = servers_mod.subprocess.run
        servers_mod.REPO_ROOT = self.tmp.name
        servers_mod.data_dir = lambda: self.tmp.name
        # A fake tts/ dir with a start script so tts_start() proceeds.
        os.makedirs(os.path.join(self.tmp.name, "tts"))
        with open(os.path.join(self.tmp.name, "tts", "start_tts.sh"), "w") as f:
            f.write("#!/bin/sh\n")
        self.popen_calls = []
        self.run_calls = []
        self.killed = []

        class FakeProc:
            def __init__(self, pid):
                self.pid = pid

        def fake_popen(cmd, **kw):
            self.popen_calls.append(cmd)
            return FakeProc(4242)

        def fake_run(cmd, **kw):
            self.run_calls.append(cmd)
            return None

        servers_mod.subprocess.Popen = fake_popen
        servers_mod.subprocess.run = fake_run
        # Health: down unless the test says otherwise.
        self._old_health = servers_mod.tts_health
        servers_mod.tts_health = lambda: False
        self._old_kill = os.kill
        os.kill = lambda pid, sig: self.killed.append((pid, sig))

    def tearDown(self):
        servers_mod.REPO_ROOT = self._old_root
        servers_mod.data_dir = self._old_data
        servers_mod.subprocess.Popen = self._old_popen
        servers_mod.subprocess.run = self._old_run
        servers_mod.tts_health = self._old_health
        os.kill = self._old_kill
        self.tmp.cleanup()

    def test_start_launches_and_records_pid(self):
        self.assertTrue(servers_mod.tts_start())
        self.assertEqual(len(self.popen_calls), 1)
        pid = open(os.path.join(self.tmp.name, "tts.pid")).read().strip()
        self.assertEqual(pid, "4242")

    def test_start_when_already_up_does_not_launch(self):
        servers_mod.tts_health = lambda: True
        self.assertTrue(servers_mod.tts_start())
        self.assertEqual(self.popen_calls, [])

    def test_start_without_tts_dir_fails(self):
        os.remove(os.path.join(self.tmp.name, "tts", "start_tts.sh"))
        self.assertFalse(servers_mod.tts_start())
        self.assertEqual(self.popen_calls, [])

    def test_stop_kills_pid_and_removes_file(self):
        with open(os.path.join(self.tmp.name, "tts.pid"), "w") as f:
            f.write("4242")
        self.assertTrue(servers_mod.tts_stop())
        self.assertEqual(self.killed, [(4242, 15)])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "tts.pid")))

    def test_stop_without_pid_file_reports_false(self):
        self.assertFalse(servers_mod.tts_stop())


class DefaultVoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dd = nodeapi_mod._dd
        nodeapi_mod._dd = lambda data_dir=None: self.tmp.name

    def tearDown(self):
        nodeapi_mod._dd = self._old_dd
        self.tmp.cleanup()

    def _serving(self):
        with open(os.path.join(self.tmp.name, "serving.json")) as f:
            return json.load(f)

    def test_set_default_voice_preserves_serving(self):
        # A serving config with a cloud backend + model + endpoint.
        with open(os.path.join(self.tmp.name, "serving.json"), "w") as f:
            json.dump({"text_backend": "openrouter", "text_model": "x",
                       "tts_voice": "mya-03",
                       "public_endpoint": "https://example.invalid",
                       "cloud_key": "secret"}, f)
        r = nodeapi_mod.set_default_voice("mya-02")
        self.assertEqual(r, {"ok": True, "tts_voice": "mya-02"})
        cfg = self._serving()
        self.assertEqual(cfg["tts_voice"], "mya-02")
        self.assertEqual(cfg["text_backend"], "openrouter")
        self.assertEqual(cfg["text_model"], "x")
        self.assertEqual(cfg["public_endpoint"], "https://example.invalid")
        self.assertEqual(cfg["cloud_key"], "secret")

    def test_set_default_voice_validates_name(self):
        self.assertIn("error", nodeapi_mod.set_default_voice(""))
        self.assertIn("error", nodeapi_mod.set_default_voice("bad voice!"))

    def test_set_default_voice_creates_config_when_missing(self):
        r = nodeapi_mod.set_default_voice("mya-02")
        self.assertEqual(r, {"ok": True, "tts_voice": "mya-02"})
        self.assertEqual(self._serving()["tts_voice"], "mya-02")


class TtsCmdTest(unittest.TestCase):
    """The 2026-11-14 START-button bug: on Windows Popen([.bat])
    raises WinError 193 — a .bat is not an executable — so the Hub's
    START button silently did nothing while the UI said "Starting…".
    The launch must go through cmd /c (same as autorun.py)."""

    def test_windows_goes_through_cmd(self):
        self.assertEqual(
            servers_mod._tts_cmd("C:\\node\\tts\\start_tts.bat",
                                 is_windows=True),
            ["cmd", "/c", "C:\\node\\tts\\start_tts.bat"])

    def test_posix_runs_script_directly(self):
        self.assertEqual(
            servers_mod._tts_cmd("/node/tts/start_tts.sh",
                                 is_windows=False),
            ["/node/tts/start_tts.sh"])

    def test_start_launches_through_tts_cmd(self):
        # Wiring: tts_start() must launch via _tts_cmd, not the bare
        # script path — prove it by patching _tts_cmd to the Windows
        # form even on this Linux box.
        tmp = tempfile.TemporaryDirectory()
        old_root = servers_mod.REPO_ROOT
        old_data = servers_mod.data_dir
        old_popen = servers_mod.subprocess.Popen
        old_health = servers_mod.tts_health
        old_cmd = servers_mod._tts_cmd
        servers_mod.REPO_ROOT = tmp.name
        servers_mod.data_dir = lambda: tmp.name
        servers_mod.tts_health = lambda: False
        os.makedirs(os.path.join(tmp.name, "tts"))
        with open(os.path.join(tmp.name, "tts", "start_tts.sh"), "w") as f:
            f.write("#!/bin/sh\n")
        calls = []

        class FakeProc:
            pid = 4242

        def fake_cmd(script, is_windows=None):
            calls.append(script)
            return ["cmd", "/c", script]

        def fake_popen(cmd, **kw):
            calls.append(cmd)
            return FakeProc()

        servers_mod._tts_cmd = fake_cmd
        servers_mod.subprocess.Popen = fake_popen
        try:
            self.assertTrue(servers_mod.tts_start())
        finally:
            servers_mod.REPO_ROOT = old_root
            servers_mod.data_dir = old_data
            servers_mod.subprocess.Popen = old_popen
            servers_mod.tts_health = old_health
            servers_mod._tts_cmd = old_cmd
            tmp.cleanup()
        self.assertEqual(calls[1], ["cmd", "/c", calls[0]])


class TtsLogTailTest(unittest.TestCase):
    """The UI now shows the bridge log while TTS is down/starting,
    so a failed first-run install no longer looks like "still
    building"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_data = servers_mod.data_dir
        self._old_health = servers_mod.tts_health
        servers_mod.data_dir = lambda: self.tmp.name
        servers_mod.tts_health = lambda: False

    def tearDown(self):
        servers_mod.data_dir = self._old_data
        servers_mod.tts_health = self._old_health
        self.tmp.cleanup()

    def _write_log(self, lines):
        with open(os.path.join(self.tmp.name, "tts.log"), "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_returns_last_n_lines(self):
        self._write_log(["line %d" % i for i in range(30)])
        self.assertEqual(servers_mod.tts_log_tail(5),
                         "\n".join("line %d" % i for i in range(25, 30)))

    def test_missing_log_returns_empty(self):
        self.assertEqual(servers_mod.tts_log_tail(), "")

    def test_starting_true_when_pid_alive(self):
        with open(os.path.join(self.tmp.name, "tts.pid"), "w") as f:
            f.write(str(os.getpid()))
        self.assertTrue(servers_mod.tts_starting())

    def test_starting_false_without_pid_file(self):
        self.assertFalse(servers_mod.tts_starting())

    def test_starting_false_when_pid_dead(self):
        # A PID that can never be alive (far outside the live range).
        with open(os.path.join(self.tmp.name, "tts.pid"), "w") as f:
            f.write("999999")
        self.assertFalse(servers_mod.tts_starting())


if __name__ == "__main__":
    unittest.main()
