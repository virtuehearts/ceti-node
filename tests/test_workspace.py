"""Code workspace: full-file read for the editor, write + syntax
check through the work folder. No network, no key, no spend.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import codeagent


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self._tmp.name
        self.wf = codeagent.WorkFolder("ws-session")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self._old
        self._tmp.cleanup()

    def test_read_full_returns_whole_file(self):
        body = "\n".join("line %d" % i for i in range(200))
        self.wf.write_file("a/b.py", body)
        self.assertEqual(self.wf.read_full("a/b.py"), body)

    def test_read_full_big_file(self):
        # hub/web_ui.html is ~230KB — the editor must load it.
        big = ("x = 1  # " + "y" * 100 + "\n") * 3000
        self.assertGreater(len(big.encode("utf-8")), 200_000)
        self.wf.write_file("big.py", big)
        self.assertEqual(self.wf.read_full("big.py"), big)

    def test_read_full_missing(self):
        with self.assertRaises(ValueError):
            self.wf.read_full("nope.py")

    def test_read_full_rejects_traversal(self):
        with self.assertRaises(ValueError):
            self.wf.read_full("../escape.py")
        with self.assertRaises(ValueError):
            self.wf.read_full("/etc/passwd")

    def test_write_then_check_py(self):
        self.wf.write_file("ok.py", "x = 1\n")
        self.assertIn("OK", self.wf.run_check("ok.py"))
        self.wf.write_file("bad.py", "def broken(:\n")
        self.assertIn("FAIL", self.wf.run_check("bad.py"))

    def test_list_files_after_write(self):
        self.wf.write_file("x.py", "x=1\n")
        self.assertIn("x.py", self.wf.list_files())


if __name__ == "__main__":
    unittest.main()
