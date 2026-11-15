"""Code agent: work-folder sandboxing, tool execution, and the
model loop (model stubbed — no network, no key, no spend).
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import codeagent


class WorkFolderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self._tmp.name
        self.wf = codeagent.WorkFolder("test-session")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self._old
        self._tmp.cleanup()

    def test_write_read_roundtrip(self):
        self.assertIn("wrote", self.wf.write_file("a.py", "x = 1\n"))
        self.assertEqual(self.wf.read_file("a.py"), "x = 1\n")

    def test_traversal_rejected(self):
        for bad in ("../escape.py", "/etc/passwd", "sub/../../x.py"):
            with self.assertRaises(ValueError, msg=bad):
                self.wf.write_file(bad, "x")
            with self.assertRaises(ValueError, msg=bad):
                self.wf.read_file(bad)

    def test_seed_allowlist(self):
        self.assertEqual(self.wf.seed("README.md"), "README.md")
        self.assertTrue(os.path.isfile(
            os.path.join(self.wf.root, "README.md")))
        for bad in ("data/identity.dat", "../secret", "models/x.gguf",
                    "/etc/passwd"):
            with self.assertRaises((ValueError, FileNotFoundError), msg=bad):
                self.wf.seed(bad)

    def test_edit_file_exact_once(self):
        self.wf.write_file("f.txt", "hello world\nhello world\n")
        with self.assertRaises(ValueError):
            self.wf.edit_file("f.txt", "hello", "bye")
        self.wf.write_file("g.txt", "alpha\nbeta\n")
        self.wf.edit_file("g.txt", "beta", "BETA")
        self.assertEqual(self.wf.read_file("g.txt"), "alpha\nBETA\n")
        with self.assertRaises(ValueError):
            self.wf.edit_file("g.txt", "missing", "x")

    def test_run_check_py(self):
        self.wf.write_file("good.py", "x = 1\n")
        self.assertTrue(self.wf.run_check("good.py").startswith("OK"))
        self.wf.write_file("bad.py", "def broken(:\n")
        self.assertTrue(self.wf.run_check("bad.py").startswith("FAIL"))

    def test_run_check_js(self):
        self.wf.write_file("good.js", "const x = 1;\n")
        self.assertTrue(self.wf.run_check("good.js").startswith("OK"))
        self.wf.write_file("bad.js", "const = ;\n")
        self.assertTrue(self.wf.run_check("bad.js").startswith("FAIL"))

    def test_run_check_html(self):
        self.wf.write_file("page.html",
                           "<html><script>const x = 1;</script></html>")
        self.assertTrue(self.wf.run_check("page.html").startswith("OK"))
        self.wf.write_file("bad.html",
                           "<html><script>const = ;</script></html>")
        self.assertTrue(self.wf.run_check("bad.html").startswith("FAIL"))


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self._tmp.name
        self._orig = codeagent.chat_cloud_tools

    def tearDown(self):
        codeagent.chat_cloud_tools = self._orig
        if self._old is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self._old
        self._tmp.cleanup()

    def test_loop_runs_tools_then_summarizes(self):
        calls = {"n": 0}

        def fake(provider, api_key, model, messages, tools,
                 max_tokens=2048, temperature=0.2):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"content": "", "tool_calls": [{
                    "id": "c1", "name": "write_file",
                    "arguments": {"path": "hello.py",
                                  "content": "print('hi')\n"},
                    "raw": {"id": "c1", "type": "function",
                            "function": {"name": "write_file",
                                         "arguments": '{"path": "hello.py", "content": "print(\'hi\')\\n"}'}}}]}
            if calls["n"] == 2:
                return {"content": "", "tool_calls": [{
                    "id": "c2", "name": "run_check",
                    "arguments": {"path": "hello.py"},
                    "raw": {"id": "c2", "type": "function",
                            "function": {"name": "run_check",
                                         "arguments": '{"path": "hello.py"}'}}}]}
            return {"content": "Wrote hello.py and it passes.",
                    "tool_calls": []}

        codeagent.chat_cloud_tools = fake
        log = []
        res = codeagent.run_agent("write hello.py", "openrouter",
                                  "test-model", "test-key",
                                  session="loop-test", log=log)
        self.assertEqual(res["files_changed"], ["hello.py"])
        self.assertIn("Wrote hello.py", res["summary"])
        self.assertTrue(any("write_file" in line for line in log))
        self.assertTrue(any("run_check" in line for line in log))
        log_path = os.path.join(self._tmp.name, "code-work", "loop-test",
                                "agent-log.json")
        self.assertTrue(os.path.isfile(log_path))
        saved = json.load(open(log_path))
        self.assertEqual(saved["model"], "test-model")

    def test_loop_stops_at_step_limit(self):
        def fake(provider, api_key, model, messages, tools,
                 max_tokens=2048, temperature=0.2):
            return {"content": "", "tool_calls": [{
                "id": "c1", "name": "list_files", "arguments": {},
                "raw": {"id": "c1", "type": "function",
                        "function": {"name": "list_files",
                                     "arguments": "{}"}}}]}
        codeagent.chat_cloud_tools = fake
        res = codeagent.run_agent("loop forever", "openrouter",
                                  "test-model", "test-key",
                                  session="limit-test", max_steps=3)
        self.assertIn("step limit", res["summary"])


if __name__ == "__main__":
    unittest.main()
