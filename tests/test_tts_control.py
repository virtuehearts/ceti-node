"""TTS process control, voice configuration, and launcher regression tests."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autorun
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
        with open(os.path.join(self.tmp.name, "tts", "start_tts.bat" if os.name == "nt" else "start_tts.sh"), "w") as f:
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
        pid = Path(self.tmp.name, "tts.pid").read_text().strip()
        self.assertEqual(pid, "4242")

    def test_launch_failure_is_logged_without_pid(self):
        with patch.object(servers_mod.subprocess, "Popen", side_effect=OSError("cannot spawn")):
            self.assertFalse(servers_mod.tts_start())
        self.assertIn("cannot spawn", Path(self.tmp.name, "tts.log").read_text())
        self.assertFalse(Path(self.tmp.name, "tts.pid").exists())

    def test_start_when_already_up_does_not_launch(self):
        servers_mod.tts_health = lambda: True
        self.assertTrue(servers_mod.tts_start())
        self.assertEqual(self.popen_calls, [])

    def test_start_without_tts_dir_fails(self):
        os.remove(os.path.join(self.tmp.name, "tts", "start_tts.bat" if os.name == "nt" else "start_tts.sh"))
        self.assertFalse(servers_mod.tts_start())
        self.assertEqual(self.popen_calls, [])

    def test_stop_kills_pid_and_removes_file(self):
        with open(os.path.join(self.tmp.name, "tts.pid"), "w") as f:
            f.write("4242")
        self.assertTrue(servers_mod.tts_stop())
        if os.name == "nt":
            self.assertEqual(self.run_calls, [["taskkill", "/PID", "4242", "/T", "/F"]])
        else:
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


class TtsLaunchCommandTest(unittest.TestCase):
    def test_windows_launch_is_hidden_and_uses_basename(self):
        with tempfile.TemporaryDirectory(prefix="ceti-node (1) ") as root:
            script = os.path.join(root, "start_tts.bat")
            with patch.object(servers_mod, "os") as windows_os:
                windows_os.name = "nt"
                windows_os.path = os.path
                with patch.object(servers_mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
                    with patch.object(servers_mod.subprocess, "Popen") as popen:
                        servers_mod.launch_tts(script, "test-log")
            popen.assert_called_once_with(
                ["cmd.exe", "/d", "/c", "call", "start_tts.bat"],
                cwd=root, stdin=servers_mod.subprocess.DEVNULL, stdout="test-log",
                stderr=servers_mod.subprocess.STDOUT, creationflags=0x08000000)

    def test_windows_command_uses_basename_for_windows_path(self):
        script = r"C:\Users\Jero\CETI beta (1)\tts\start_tts.bat"
        self.assertEqual(servers_mod._tts_cmd(script, is_windows=True),
                         ["cmd.exe", "/d", "/c", "call", "start_tts.bat"])

    def test_autorun_uses_shared_launcher_and_records_pid(self):
        with tempfile.TemporaryDirectory() as root:
            tts = Path(root, "tts")
            tts.mkdir()
            script = tts / ("start_tts.bat" if os.name == "nt" else "start_tts.sh")
            script.touch()
            log = Path(root, "tts.log")
            with patch.object(autorun, "ROOT", root), patch.object(servers_mod, "launch_tts") as launch:
                launch.return_value.pid = 42
                self.assertIs(autorun.start_tts(str(log)), launch.return_value)
                self.assertEqual(launch.call_args.args[0], str(script))
                self.assertTrue(launch.call_args.args[1].closed)
            self.assertEqual(Path(root, "tts.pid").read_text(), "42")

    @unittest.skipIf(os.name == "nt", "POSIX script launch")
    def test_posix_launch_captures_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory(prefix="ceti-node (1) ") as root:
            script = Path(root, "start_tts.sh")
            log = Path(root, "tts.log")
            script.write_text("echo stdout\necho stderr >&2\n")
            with log.open("wb") as output:
                proc = servers_mod.launch_tts(str(script), output)
            self.assertEqual(proc.wait(timeout=10), 0)
            self.assertEqual(log.read_text().splitlines(), ["stdout", "stderr"])


@unittest.skipUnless(os.name == "nt",
                     "Windows-only: runs the real .bat through cmd")
class TtsWindowsLaunchTest(unittest.TestCase):
    """Exercise the real hidden launcher and batch file on Windows."""

    def test_background_child_has_no_attached_console(self):
        with tempfile.TemporaryDirectory(prefix="ceti-node (1) ") as root:
            script = Path(root, "start_tts.bat")
            script.write_text(
                '@echo off\n'
                f'"{sys.executable}" -c "import ctypes; print(ctypes.windll.kernel32.GetConsoleCP())"\n',
                encoding="ascii",
            )
            log = Path(root, "tts.log")
            with log.open("wb") as output:
                proc = servers_mod.launch_tts(str(script), output)
            try:
                self.assertEqual(proc.wait(timeout=30), 0)
            finally:
                if proc.poll() is None:
                    servers_mod.stop_tts_process_tree(proc.pid)
            self.assertEqual(log.read_text().strip(), "0")

    def test_bat_runs_from_space_paren_path(self):
        import shutil
        import subprocess
        repo_root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        tmp = tempfile.TemporaryDirectory()
        try:
            # A duplicate-download path: space + parens.
            tts_dir = os.path.join(tmp.name, "ceti-node (1)", "tts")
            os.makedirs(tts_dir)
            shutil.copy(os.path.join(repo_root, "tts", "start_tts.bat"),
                        os.path.join(tts_dir, "start_tts.bat"))
            venv = Path(tts_dir, ".venv")
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                           check=True, timeout=120)
            (venv / ".tts-installed").touch()
            script = os.path.join(tts_dir, "start_tts.bat")
            log = Path(tts_dir, "tts.log")
            with log.open("wb") as output:
                proc = servers_mod.launch_tts(script, output)
            try:
                returncode = proc.wait(timeout=120)
            finally:
                if proc.poll() is None:
                    servers_mod.stop_tts_process_tree(proc.pid)
            out = log.read_text(errors="replace")
            self.assertNotIn("is not recognized", out)
            self.assertNotIn("was unexpected at this time", out)
            self.assertIn("dependencies incomplete", out)
            self.assertEqual(returncode, 1)
        finally:
            tmp.cleanup()


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
