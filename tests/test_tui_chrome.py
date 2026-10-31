"""Hermes chrome unit tests — stdlib unittest only, no curses, no servers.

Run from the repo root (HUB_DATA_DIR is set to a temp dir per test):
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.tui import Theme, TUI, line_role, panel


class FakeIdent:
    fingerprint = "HUB-TEST-0000"
    node_id = "hubtest0000"
    hash = "00" * 20


class PanelTest(unittest.TestCase):
    def test_borders_and_uniform_width(self):
        rows = [("alpha", "green"), "sep", ("beta gamma", "dim")]
        lines = panel("TITLE", rows, 40)
        texts = [t for t, _ in lines]
        self.assertTrue(texts[0].startswith("┌") and texts[0].endswith("┐"))
        self.assertTrue(texts[-1].startswith("└") and texts[-1].endswith("┘"))
        self.assertIn("TITLE", texts[0])
        self.assertTrue(any(t.startswith("├") and t.endswith("┤")
                            for t in texts))
        for t in texts:
            self.assertEqual(len(t), len(texts[0]))
            if t.startswith("│"):
                self.assertTrue(t.endswith("│"))

    def test_long_row_wraps_within_width(self):
        long_text = "word " * 30
        lines = panel("T", [(long_text, "green")], 40)
        self.assertGreater(len(lines), 3)  # title + wrapped rows + bottom
        for t, _ in lines:
            self.assertLessEqual(len(t), 38)

    def test_narrow_panel(self):
        lines = panel("T", [("x", "green")], 24)
        for t, _ in lines:
            self.assertLessEqual(len(t), 24)


class RoleTest(unittest.TestCase):
    def test_roles_by_prefix(self):
        self.assertEqual(line_role("you> hello"), "cyan")
        self.assertEqual(line_role("hub> hello"), "green")
        self.assertEqual(line_role("hub story> once"), "green")
        self.assertEqual(line_role("[chat error: boom]"), "red")
        self.assertEqual(line_role("[answer cancelled]"), "amber")
        self.assertEqual(line_role("  ! violation"), "amber")
        self.assertEqual(line_role("--- block 1 done ---"), "dim")
        self.assertEqual(line_role("plain transcript"), "green")

    def test_theme_without_init_is_zero(self):
        th = Theme()
        for role in ("green", "cyan", "dim", "amber", "red", "title", "rev"):
            self.assertEqual(th.attr(role), 0)


class DoorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self.old
        self.tmp.cleanup()

    def test_menu_panel(self):
        tui = TUI(FakeIdent())
        lines = tui.menu_panel(80)
        text = "\n".join(t for t, _ in lines)
        self.assertIn("UNCENSORED HUB // MAIN MENU", text)
        self.assertIn("Chat", text)
        self.assertIn("Stories", text)
        self.assertTrue(text.split("\n")[0].startswith("┌"))

    def test_status_panels_structure(self):
        tui = TUI(FakeIdent())
        lines = tui.status_panels(80)
        text = "\n".join(t for t, _ in lines)
        for section in ("SESSION", "SERVERS", "MEMORY", "STORIES",
                        "NEXT ACTION"):
            self.assertIn(section, text)
        self.assertIn("HUB-TEST-0000", text)
        self.assertIn("hubtest0000", text)


if __name__ == "__main__":
    unittest.main()
