from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from semshare.app_client import SecuShareClient
from semshare import lifecycle
from semshare.lifecycle import _validate_run_id
from semshare.oram import FileLock, Sidecar


class GuardTests(unittest.TestCase):
    def assert_lock_available_to_another_thread(self):
        result = []
        def probe():
            acquired = FileLock._local_lock.acquire(timeout=0.1)
            result.append(acquired)
            if acquired:
                FileLock._local_lock.release()
        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(timeout=1)
        self.assertEqual(result, [True])

    def test_failed_lock_entry_releases_the_client_coordinator(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(FileLock, "_local_lock", threading.RLock()):
            parent = Path(raw) / "file"
            parent.write_text("owned fixture", encoding="utf-8")
            with self.assertRaises(OSError), FileLock(parent / "lock"):
                self.fail("a file cannot contain a lock file")
            self.assert_lock_available_to_another_thread()

    def test_failed_lock_exit_releases_the_client_coordinator(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(FileLock, "_local_lock", threading.RLock()):
            with self.assertRaises(ValueError), FileLock(Path(raw) / "lock") as lock:
                lock.stream.close()
            self.assert_lock_available_to_another_thread()

    def test_application_client_refuses_non_loopback_or_ambiguous_urls(self):
        allowed = SecuShareClient("http://127.0.0.1:8080/api/v1")
        self.assertEqual(allowed.api_base, "http://127.0.0.1:8080/api/v1")
        for url in (
            "https://127.0.0.1:8080/api/v1",
            "http://localhost:8080/api/v1",
            "http://127.0.0.1:8080/api/v1?next=https://example.org",
            "http://name@127.0.0.1:8080/api/v1",
            "http://127.0.0.1:8080/other",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                SecuShareClient(url)

    def test_sidecars_and_run_ids_are_strictly_bounded(self):
        with Sidecar("http://127.0.0.1:9000"):
            pass
        for url in ("http://localhost:9000", "http://127.0.0.1:9000/path", "http://user@127.0.0.1:9000"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Sidecar(url)
        _validate_run_id("a" * 32)
        for run_id in ("../" + "a" * 29, "A" * 32, "a" * 31, "a" * 33):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                _validate_run_id(run_id)

    def test_cleanup_preserves_process_logs_outside_deleted_scratch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_id = "b" * 32
            old_run_root, old_evidence_root = lifecycle.RUN_ROOT, lifecycle.EVIDENCE_ROOT
            lifecycle.RUN_ROOT = root / "runs"
            lifecycle.EVIDENCE_ROOT = root / "evidence"
            try:
                run_dir = lifecycle.RUN_ROOT / run_id
                run_dir.mkdir(parents=True)
                (run_dir / ".semshare-owned").write_text(run_id, encoding="ascii")
                app_log = run_dir / "app.log"
                app_log.write_text("synthetic run log\n", encoding="utf-8")
                (run_dir / "run.json").write_text(json.dumps({
                    "schema": 1,
                    "run_id": run_id,
                    "run_dir": str(run_dir.resolve()),
                    "pids": {},
                    "logs": [str(app_log)],
                }), encoding="utf-8")

                result = lifecycle.cleanup(run_id)

                saved = lifecycle.EVIDENCE_ROOT / run_id / "app.log"
                self.assertTrue(result["run_data_removed"])
                self.assertFalse(run_dir.exists())
                self.assertEqual(saved.read_text(encoding="utf-8"), "synthetic run log\n")
            finally:
                lifecycle.RUN_ROOT, lifecycle.EVIDENCE_ROOT = old_run_root, old_evidence_root


if __name__ == "__main__":
    unittest.main()
