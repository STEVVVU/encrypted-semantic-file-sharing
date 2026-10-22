from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from semshare.sidecar import MAX_OBJECT_BYTES, ObjectStore


class ObjectStoreTests(unittest.TestCase):
    def test_immutable_bounded_objects_and_missing_reads(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = ObjectStore(Path(root))
            store.put("a" * 32, b"share")
            self.assertEqual(store.get("a" * 32), b"share")
            with self.assertRaises(FileExistsError):
                store.put("a" * 32, b"replacement")
            with self.assertRaises(FileNotFoundError):
                store.get("b" * 32)
            with self.assertRaises(ValueError):
                store.put("../outside", b"share")
            with self.assertRaises(ValueError):
                store.put("c" * 32, b"x" * (MAX_OBJECT_BYTES + 1))
            store.delete("a" * 32)
            self.assertFalse((Path(root) / f"{'a' * 32}.share").exists())


if __name__ == "__main__":
    unittest.main()
