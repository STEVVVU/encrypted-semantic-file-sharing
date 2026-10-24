import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_scan_receipts import verify_bundle


ROOT = Path(__file__).resolve().parents[3]
BUNDLE = ROOT / "docs" / "evidence" / "scan-20261024"


class ScanReceiptTests(unittest.TestCase):
    def test_curated_receipts_recompute_and_validate(self):
        result = verify_bundle(BUNDLE)
        self.assertTrue(result["validated"])
        self.assertEqual(result["derived_comparisons"]["a"][0]["median_of_round_medians_seconds"], 4.174683199998981)
        self.assertEqual(result["derived_comparisons"]["a"][1]["median_of_round_medians_seconds"], 0.40039105000323616)
        self.assertEqual(result["derived_comparisons"]["b"][3]["median_of_round_medians_seconds"], 7.409772799990606)

    def test_changed_ranking_fails_even_with_updated_canonical_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "bundle"
            shutil.copytree(BUNDLE, copy)
            samples_path = copy / "b" / "samples.json"
            samples = json.loads(samples_path.read_text(encoding="utf-8"))
            ranked = next(row for row in samples if "actual_top5" in row)
            self.assertEqual(ranked["actual_top5"], ranked["oracle_top5"])
            ranked["actual_top5"].reverse()
            data = (json.dumps(samples, indent=2) + "\n").encode("utf-8")
            samples_path.write_bytes(data)
            manifest_path = copy / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = next(row for row in manifest["files"] if row["path"] == "b/samples.json")
            entry["canonical_sha256"] = hashlib.sha256(data).hexdigest()
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ValueError, "original raw receipt identity mismatch"):
                verify_bundle(copy)


if __name__ == "__main__":
    unittest.main()
