from __future__ import annotations

import unittest
from unittest.mock import patch

from semshare.benchmark import TOPICS
from semshare.scan_benchmark import QUERIES, ROUNDS, SIZES, _read_receipt, expected_snapshot_gets, main, round_order, summarize
from semshare.encoder import DIMENSION


class ScanBenchmarkContractTests(unittest.TestCase):
    def test_profile_only_cli_does_not_change_the_frozen_campaign(self):
        run_id = "a" * 32
        with patch("sys.argv", ["scan_benchmark", "--run-id", run_id, "--profile-only"]), patch("semshare.scan_benchmark.run") as run:
            main()
        self.assertEqual(run.call_args.args[0], run_id)
        self.assertTrue(run.call_args.args[2])
        self.assertEqual((SIZES, ROUNDS, len(QUERIES)), ((64, 512, 1024), 5, 4))

    def test_rounds_alternate_and_snapshot_request_count_includes_both_workspace_indexes(self):
        self.assertEqual([round_order(number) for number in range(5)], [
            ("point_read", "snapshot"),
            ("snapshot", "point_read"),
            ("point_read", "snapshot"),
            ("snapshot", "point_read"),
            ("point_read", "snapshot"),
        ])
        self.assertEqual([expected_snapshot_gets(size) for size in (64, 512, 1024)], [508, 4092, 8188])

    def test_summary_requires_all_five_rounds_and_held_out_queries(self):
        samples = []
        for size, methods in ((64, ("point_read", "snapshot")), (512, ("snapshot",)), (1024, ("snapshot",))):
            for method in methods:
                for round_number in range(5):
                    for query_number, query in enumerate(QUERIES):
                        samples.append({
                            "size": size,
                            "method": method,
                            "round": round_number,
                            "query_label": query["label"],
                            "search_seconds": round_number + query_number / 10,
                            "correct_exact_top5": True,
                        })
        summary = summarize(samples)
        self.assertEqual(len(summary), 4)
        self.assertEqual([(row["size"], row["method"]) for row in summary], [
            (64, "point_read"), (64, "snapshot"), (512, "snapshot"), (1024, "snapshot"),
        ])
        self.assertTrue(all("quantile_method" in row for row in summary))
        self.assertAlmostEqual(summary[0]["median_round_seconds"], 2.15)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            summarize(samples[:-1])

    def test_input_receipt_keeps_hashes_without_plaintext_vectors_or_text(self):
        label, text, _ = TOPICS[0]
        document = {"id": "doc-000", "label": label, "text": "synthetic private document"}
        query = {"label": label, "text": "synthetic private query"}
        receipt = _read_receipt([document], (query,), [[0.0] * DIMENSION])
        serialized = str(receipt)
        self.assertNotIn(document["text"], serialized)
        self.assertNotIn(query["text"], serialized)
        self.assertNotIn("vectors", receipt)
        self.assertEqual(len(receipt["vector_hashes"]), 1)
        self.assertEqual(len(receipt["query_hashes"]), 1)


if __name__ == "__main__":
    unittest.main()
