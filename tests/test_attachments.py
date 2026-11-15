"""Tests for chat attachments (hub/attachments.py): kind detection,
text extraction, and chunking. No servers, no network."""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

from hub import attachments


class KindTest(unittest.TestCase):
    def test_text_kinds(self):
        for name in ("notes.txt", "code.py", "page.html", "data.json",
                     "sheet.csv", "readme.md", "app.js", "style.css"):
            self.assertEqual(attachments.kind_for(name), "text", name)

    def test_pdf_and_image(self):
        self.assertEqual(attachments.kind_for("doc.pdf"), "pdf")
        self.assertEqual(attachments.kind_for("photo.png"), "image")
        self.assertEqual(attachments.kind_for("photo.JPG"), "image")

    def test_unsupported(self):
        for name in ("archive.zip", "song.mp3", "video.mp4", "noext",
                     "binary.exe"):
            self.assertIsNone(attachments.kind_for(name), name)


class ExtractTest(unittest.TestCase):
    def test_text_decode(self):
        kind, text = attachments.extract_text(
            "code.py", b"def hello():\n    return 42\n")
        self.assertEqual(kind, "text")
        self.assertIn("def hello", text)

    def test_image_has_no_text(self):
        kind, text = attachments.extract_text(
            "photo.png", b"\x89PNG\r\n\x1a\nxxxx")
        self.assertEqual(kind, "image")
        self.assertEqual(text, "")

    def test_pdf_needs_path(self):
        kind, text = attachments.extract_text("doc.pdf", b"%PDF-1.4")
        self.assertEqual(kind, "pdf")
        self.assertEqual(text, "")


class ChunkTest(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(attachments.chunk_text("hello"), ["hello"])

    def test_empty_is_no_chunks(self):
        self.assertEqual(attachments.chunk_text(""), [])
        self.assertEqual(attachments.chunk_text("   \n  "), [])

    def test_splits_under_cap(self):
        text = "\n".join("line %d" % i for i in range(200))
        chunks = attachments.chunk_text(text, size=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith("line 0"))
        self.assertTrue(chunks[-1].endswith("line 199"))
        joined = "\n".join(chunks)
        self.assertIn("line 0", joined)
        self.assertIn("line 199", joined)


if __name__ == "__main__":
    unittest.main()
