import unittest

from semshare.benchmark import corpus, quality, latency_summary, load_outcomes


class BenchmarkContractTests(unittest.TestCase):
    def test_load_counts_and_latency_separate_success_rejection_and_error(self):
        rows = [{"correct": True, "rejected": False, "scheduled_latency_seconds": 9},
                {"correct": False, "rejected": True, "scheduled_latency_seconds": .1, "timed_out": True},
                {"correct": False, "rejected": False, "scheduled_latency_seconds": 12, "timed_out": True}]
        result = load_outcomes(rows)
        self.assertEqual({k: result[k] for k in ("offered", "completed", "rejected", "errors", "timeouts")},
                         {"offered": 3, "completed": 1, "rejected": 1, "errors": 1, "timeouts": 2})
        self.assertEqual(result["latency_by_outcome"]["completed"]["p50_seconds"], 9)
        self.assertEqual(result["latency_by_outcome"]["rejected"]["p50_seconds"], .1)
        self.assertEqual(result["latency_by_outcome"]["errors"]["p50_seconds"], 12)
        self.assertIsNone(result["latency_by_outcome"]["completed"]["p99_seconds"])

    def test_empty_and_short_populations_do_not_invent_p99(self):
        self.assertIsNone(latency_summary([])["p50_seconds"])
        self.assertIsNone(latency_summary(list(range(99)))["p99_seconds"])
        self.assertEqual(latency_summary(list(range(100)))["p99_seconds"], 98)
        with self.assertRaises(AssertionError):
            load_outcomes([{"correct": True, "rejected": True, "scheduled_latency_seconds": 1}])

    def test_frozen_corpus_balances_labels_at_supported_sizes(self):
        for size in (8, 32, 64):
            documents = corpus(size)
            self.assertEqual(len({d["id"] for d in documents}), size)
            self.assertEqual({sum(d["label"] == label for d in documents) for label in {d["label"] for d in documents}}, {size // 8})

    def test_relevance_metrics_distinguish_order_and_missed_documents(self):
        result = quality(["other", "target", "other", "target", "other", "target"], "target")
        self.assertAlmostEqual(result["precision_at_5"], 2 / 5)
        self.assertAlmostEqual(result["recall_at_5"], 2 / 3)
        self.assertEqual(result["mrr"], .5)
        self.assertGreater(result["ndcg_at_5"], 0)
        self.assertLess(result["ndcg_at_5"], 1)
