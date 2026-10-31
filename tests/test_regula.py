"""Regula chunked block store tests — stdlib unittest only, temp dirs only.

Covers: 50MB chunking (with a small CHUNK_SIZE override for boundary
tests), content-addressed dedup, manifest + Merkle root, ledger block
+ manifest events, chain verification, chunk verify + fetch, and
record_file for small files. Never touches the real data/ directory.
"""
import hashlib
import json
import os
import tempfile
import unittest

from hub import regula


class ChunkStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_chunk = regula.CHUNK_SIZE
        regula.CHUNK_SIZE = 10  # tiny chunks so boundaries are testable

    def tearDown(self):
        regula.CHUNK_SIZE = self.orig_chunk
        self.tmp.cleanup()

    def _write(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_chunk_boundaries_and_dedup(self):
        src = self._write("data.bin", b"0123456789" * 3)  # 30 bytes -> 3 chunks
        file_sha, size, chunks, root = regula.chunk_file(self.tmp.name, src)
        self.assertEqual(size, 30)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([c["size"] for c in chunks], [10, 10, 10])
        self.assertEqual(file_sha, hashlib.sha256(b"0123456789" * 3).hexdigest())
        # All three chunks are identical bytes -> one chunk file (dedup).
        self.assertEqual(regula.blocks_count(self.tmp.name), 1)
        self.assertEqual(regula.get_chunk(self.tmp.name, chunks[0]["sha256"]), b"0123456789")

    def test_store_chunked_events_manifest_and_verify(self):
        src = self._write("notes.txt", b"abcdefghij" + b"KLMNOPQRST" + b"uv")  # 22 -> 3 chunks
        manifest, event = regula.store_chunked(self.tmp.name, "model", src, "notes.txt", "test-repo")
        self.assertEqual(len(manifest["files"][0]["chunks"]), 3)
        self.assertEqual(event["kind"], "model")
        ok, height, err = regula.verify(self.tmp.name)
        self.assertTrue(ok, err)
        # genesis + 3 blocks + 1 manifest = 5
        self.assertEqual(height, 5)
        ok, checked, err = regula.verify_blocks(self.tmp.name)
        self.assertTrue(ok, err)
        self.assertEqual(checked, 3)
        # Manifest on disk verifies against the node pubkey.
        file_sha = manifest["files"][0]["sha256"]
        with open(regula.manifest_path(self.tmp.name, file_sha)) as f:
            saved = json.load(f)
        self.assertTrue(regula.verify_manifest(saved, saved["pubkey"]))
        # Tamper a chunk -> verify_blocks fails.
        with open(regula.chunk_path(self.tmp.name, manifest["files"][0]["chunks"][0]["sha256"]), "wb") as f:
            f.write(b"tampered!!")
        ok, _c, err = regula.verify_blocks(self.tmp.name)
        self.assertFalse(ok)

    def test_record_file_single_event_no_copy(self):
        src = self._write("session.json", b'{"id": "s1"}')
        ev = regula.record_file(self.tmp.name, "session", src, "CHAT")
        self.assertEqual(ev["kind"], "session")
        self.assertEqual(ev["detail"], "CHAT")
        self.assertEqual(regula.blocks_count(self.tmp.name), 0)
        ok, height, err = regula.verify(self.tmp.name)
        self.assertTrue(ok, err)
        self.assertEqual(height, 2)  # genesis + record
        events = regula.list_events(self.tmp.name, 10)
        self.assertEqual(events[0]["kind"], "session")

    def test_get_chunk_rejects_bad_sha(self):
        self.assertIsNone(regula.get_chunk(self.tmp.name, "not-a-sha"))
        self.assertIsNone(regula.get_chunk(self.tmp.name, "0" * 64))


if __name__ == "__main__":
    unittest.main()
