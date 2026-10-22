from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from semshare import cli, lifecycle


class FailureEvidenceTests(unittest.TestCase):
    def test_post_launch_injected_failure_reaps_owned_processes_and_keeps_failure_evidence(self):
        run_id = "a" * 32
        with tempfile.TemporaryDirectory() as raw:
            evidence_root = Path(raw) / "evidence"
            cleanup_result = {
                "run_id": run_id,
                "stopped": ["storage_b", "storage_a", "app"],
                "run_data_removed": True,
            }
            with (
                patch.object(lifecycle, "EVIDENCE_ROOT", evidence_root),
                patch.object(lifecycle, "doctor", return_value={"status": "ready"}),
                patch.object(lifecycle, "cleanup", return_value=cleanup_result) as cleanup,
            ):
                result = cli.main(["inject-failure", "--run-id", run_id])

            self.assertEqual(result, 1)
            cleanup.assert_called_once_with(run_id)
            evidence = json.loads((evidence_root / run_id / "failure.json").read_text())
            self.assertEqual(evidence["status"], "failed")
            self.assertTrue(evidence["owned_processes_reaped"])
            self.assertTrue(evidence["run_data_removed"])
            self.assertEqual(len(evidence["stopped_roles"]), 3)


if __name__ == "__main__":
    unittest.main()
