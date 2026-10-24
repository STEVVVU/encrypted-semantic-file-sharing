"""Frozen synthetic corpus scaling against the real SecuShare app and sidecars."""
from __future__ import annotations

import argparse
import cProfile
import concurrent.futures
import hashlib
import json
import math
import os
import platform
import pstats
import io
import shutil
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

from . import lifecycle
from .app_client import SecuShareClient
from .cli import _model_path
from .encoder import DIMENSION, MODEL_REVISION, SentenceEncoder, cosine
from .oram import ORAMError, PathORAM, Sidecar, SidecarPair
from .workspace import Workspace
from .workflow import upload_share_and_embed

TOPICS = [
    ("refund", "The card issuer reimbursed a duplicate credit card charge after the customer requested a payment reversal.", "How can I get money back after being billed twice?"),
    ("garden", "The greenhouse gardener planted tomatoes and watered the vegetable seedlings every morning.", "Advice for raising edible plants under glass."),
    ("travel", "The passenger booked a train ticket and reserved a hotel near the railway station for the holiday.", "Planning accommodation and rail transport for a vacation."),
    ("backup", "The administrator restored the database from an encrypted backup after a disk failure.", "Recover stored records when the hard drive breaks."),
    ("fitness", "The runner trained for a marathon with endurance workouts and recovery days.", "Preparing to finish a long distance foot race."),
    ("baking", "The baker mixed flour and yeast, kneaded the dough and baked a loaf in the oven.", "Making homemade bread with a risen mixture."),
    ("astronomy", "The astronomer used a telescope to observe distant galaxies and measure starlight.", "Studying objects far beyond our solar system."),
    ("music", "The violinist rehearsed a concerto with the orchestra before the evening performance.", "Preparing a classical string solo with an ensemble."),
]


def corpus(size):
    return [{"id": f"doc-{i:02}", "label": TOPICS[i % 8][0], "text": TOPICS[i % 8][1] + f" This is synthetic case number {i // 8 + 1}."} for i in range(size)]


def quality(ranked_labels, relevant, k=5):
    observed = ranked_labels[:k]
    hits = [int(label == relevant) for label in observed]
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, ranked_labels.count(relevant))))
    return {"precision_at_5": sum(hits) / min(k, len(ranked_labels)),
            "recall_at_5": sum(hits) / ranked_labels.count(relevant),
            "mrr": next((1 / (i + 1) for i, label in enumerate(ranked_labels) if label == relevant), 0),
            "ndcg_at_5": sum(hit / math.log2(i + 2) for i, hit in enumerate(hits)) / ideal}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def latency_summary(values):
    return {"samples": len(values), "p50_seconds": percentile(values, .5) if values else None,
            "p95_seconds": percentile(values, .95) if values else None,
            "p99_seconds": percentile(values, .99) if len(values) >= 100 else None,
            "min_seconds": min(values) if values else None, "max_seconds": max(values) if values else None,
            "method": "nearest-rank empirical quantiles; p99 omitted below100 observations"}


def load_outcomes(requests):
    assert all(not (r["correct"] and r["rejected"]) for r in requests)
    groups = {"completed": [r for r in requests if r["correct"]],
              "rejected": [r for r in requests if r["rejected"]],
              "errors": [r for r in requests if not (r["correct"] or r["rejected"])]}
    return {"offered": len(requests), **{name: len(rows) for name, rows in groups.items()},
            "timeouts": sum(r.get("timed_out", False) for r in requests),
            "latency_by_outcome": {name: latency_summary([r["scheduled_latency_seconds"] for r in rows]) for name, rows in groups.items()}}


def counter(pair):
    return [sum(getattr(side, field) for side in (pair.first, pair.second)) for field in ("messages", "bytes_sent", "bytes_received")]


def resource_snapshot(state):
    import resource
    own = resource.getrusage(resource.RUSAGE_SELF)
    rss = {}
    for role, pid in state["pids"].items():
        status = Path(f"/proc/{pid}/status")
        if status.is_file():
            rss[role] = int(next(line.split()[1] for line in status.read_text().splitlines() if line.startswith("VmRSS:")))
    return {"client_peak_rss_kib": own.ru_maxrss, "client_cpu_seconds": own.ru_utime + own.ru_stime, "owned_process_rss_kib": rss}


class CountedClient(SecuShareClient):
    def __init__(self, *args):
        super().__init__(*args)
        self.traffic = {"requests": 0, "sent_body_bytes": 0, "received_body_bytes": 0, "http_errors": 0}

    def request(self, path, method="GET", body=None, content_type="application/json"):
        result = super().request(path, method, body, content_type)
        self.traffic["requests"] += 1
        self.traffic["sent_body_bytes"] += len(body or b"")
        self.traffic["received_body_bytes"] += len(result[2])
        self.traffic["http_errors"] += int(result[0] >= 400)
        return result


def drive(state, cache, smoke):
    encoder = SentenceEncoder(_model_path(cache))
    owner = CountedClient(state["api_url"])
    owner.create_guest()
    pair = SidecarPair(*(Sidecar(url, instance_id=identity) for url, identity in zip(state["storage_urls"], state["storage_instance_ids"], strict=True)))
    with pair:
        return _drive(state, encoder, owner, pair, smoke)


def _drive(state, encoder, owner, pair, smoke):
    evidence = lifecycle.EVIDENCE_ROOT / state["run_id"]
    sizes = [8] if smoke else [8, 32, 64]
    queries = [{"label": label, "text": query} for label, _, query in TOPICS[:4]]
    records, uploads, samples = [], [], []
    source_documents = corpus(max(sizes))
    save(evidence / "workload.json", {"documents": source_documents, "held_out_queries": queries, "relevance": "same topic label; all labels frozen before inference", "sha256": hashlib.sha256(json.dumps({"documents": source_documents, "queries": queries}, sort_keys=True).encode()).hexdigest()})
    encoder.encode([queries[0]["text"]])
    ingest_epoch = time.perf_counter()
    for document_index, doc in enumerate(source_documents):
        scheduled_ingest = ingest_epoch + document_index * 4.2
        time.sleep(max(0, scheduled_ingest - time.perf_counter()))
        started = time.perf_counter()
        reference, payload, detail = upload_share_and_embed(owner, encoder, doc["id"] + ".txt", doc["text"].encode())
        packed_vector = list(struct.unpack(f"<{DIMENSION}f", struct.pack(f"<{DIMENSION}f", *detail["vector"])))
        records.append((reference, payload, packed_vector, doc["label"]))
        uploads.append({"document": doc["id"], "seconds": time.perf_counter() - started, "scheduled_latency_seconds": time.perf_counter() - scheduled_ingest, "plaintext_sha256": hashlib.sha256(doc["text"].encode()).hexdigest(), "ciphertext_sha256": detail["secret"]["ciphertext_sha256"], "correct": all(detail["tamper_checks"].values())})
        assert uploads[-1]["correct"]
    save(evidence / "uploads.json", {"samples": uploads, "app_traffic": owner.traffic, "offered_documents_per_second": 1 / 4.2, "timing_scope": "one upload/share/download/decrypt/embed per distinct document; scheduled below shipped 30 file requests/minute limit; not a repeated ingest comparison"})
    for size in sizes:
        workspace = Workspace.create(Path(state["run_dir"]) / f"metrics-workspace-{size}", pair, capacity=size)
        started = time.perf_counter()
        for reference, payload, _, _ in records[:size]:
            workspace.add("metrics", reference, payload)
        samples.append({"kind": "index_build", "size": size, "seconds": time.perf_counter() - started, "records": size})
        for phase in ("delta", "merged"):
            if phase == "merged":
                started = time.perf_counter()
                merged = workspace.merge("metrics")
                assert merged["merged_records"] == size and merged["previous_epoch_retained"]
                samples.append({"kind": "merge", "size": size, "seconds": time.perf_counter() - started})
            for repeat in range(1 if smoke else 5):
                for query in queries[:1] if smoke else queries:
                    before_encoding = time.perf_counter()
                    vector = encoder.encode([query["text"]])[0]
                    encode_seconds = time.perf_counter() - before_encoding
                    oracle = sorted(((ref, cosine(vector, stored), label) for ref, _, stored, label in records[:size]), key=lambda row: (-row[1], row[0]))
                    results = {}
                    for side in (["plaintext_exact", "encrypted"] if repeat % 2 == 0 else ["encrypted", "plaintext_exact"]):
                        before = counter(pair)
                        started = time.perf_counter()
                        if side == "encrypted":
                            found = workspace.search("metrics", vector, limit=5)
                            actual = [(r["reference"], r["score"]) for r in found]
                        else:
                            plain = sorted(((ref, cosine(vector, stored)) for ref, _, stored, _ in records[:size]), key=lambda row: (-row[1], row[0]))[:5]
                            actual = plain
                        seconds = time.perf_counter() - started
                        assert [r[0] for r in actual] == [r[0] for r in oracle[:5]]
                        assert all(abs(a[1] - b[1]) <= 1e-6 for a, b in zip(actual, oracle[:5], strict=True))
                        delta = [after - prior for after, prior in zip(counter(pair), before)]
                        results[side] = actual
                        samples.append({"kind": "query", "size": size, "phase": phase, "repeat": repeat, "query_label": query["label"], "side": side,
                                        "search_seconds": seconds, "encoding_seconds": encode_seconds, "end_to_end_seconds": seconds + encode_seconds,
                                        "offered": 1, "completed": 1, "errors": 0, "rejected": 0, "timeouts": 0,
                                        "sidecar_messages": delta[0], "sidecar_sent_payload_bytes": delta[1], "sidecar_received_payload_bytes": delta[2],
                                        "network_amplification_over_embedding_bytes": (delta[1] + delta[2]) / (size * DIMENSION * 4),
                                        "quality": quality([r[2] for r in oracle], query["label"]), "correct_exact_top5": True, "resources": resource_snapshot(state)})
                    assert results["encrypted"] == results["plaintext_exact"]
                    save(evidence / "samples.json", samples)
                    assert shutil.disk_usage(lifecycle.REPO_ROOT).free >= 10 * 1024 ** 3
            base, delta = workspace.index_paths("metrics")
            for index_path in (base, delta):
                if index_path is not None:
                    assert PathORAM.open(index_path, pair).invariant_report()["valid"]
            assert workspace.search("other-session", vector, limit=5) == []
            stored = sum(p.stat().st_size for root in state["storage_data_dirs"] for p in Path(root).rglob("*.share"))
            samples.append({"kind": "storage", "size": size, "phase": phase, "cumulative_sidecar_bytes": stored,
                            "logical_plaintext_bytes_current_corpus": sum(len(d["text"].encode()) for d in source_documents[:size]),
                            "cumulative_sidecar_amplification_over_unique_uploaded_plaintext": stored / sum(len(d["text"].encode()) for d in source_documents),
                            "scope": "aggregate live sidecar files across all corpus workspaces and retained epoch/delta trees; superseded path buckets are deleted after durable commit; not active-corpus storage or cumulative write bytes"})
        profiler = cProfile.Profile()
        profiled = profiler.runcall(workspace.search, "metrics", vector, 5)
        profiler.dump_stats(str(evidence / f"search-{size}.prof"))
        profile_text = io.StringIO()
        pstats.Stats(profiler, stream=profile_text).sort_stats("cumulative").print_stats(40)
        (evidence / f"search-{size}-profile.txt").write_text(profile_text.getvalue(), encoding="utf-8")
        assert [r["reference"] for r in profiled] == [r[0] for r in oracle[:5]]
    if not smoke:
        expected_refs = [row[0] for row in oracle[:5]]
        for concurrency, rate in ((1, .25), (2, 1), (4, 4)):
            for repeat in range(5):
                epoch = time.perf_counter()

                def request(index):
                    scheduled = epoch + index / rate
                    time.sleep(max(0, scheduled - time.perf_counter()))
                    try:
                        actual = workspace.search("metrics", vector, limit=5)
                        assert [r["reference"] for r in actual] == expected_refs
                        return {"scheduled_latency_seconds": time.perf_counter() - scheduled, "correct": True, "rejected": False, "error": None, "timed_out": False}
                    except Exception as error:
                        rejected = isinstance(error, ORAMError) and str(error) in ("timed out waiting for the client index lock", "timed out waiting for the client coordinator")
                        return {"scheduled_latency_seconds": time.perf_counter() - scheduled, "correct": False, "rejected": rejected,
                                "error": str(error), "error_type": type(error).__name__, "timed_out": rejected or isinstance(error, TimeoutError)}

                before = counter(pair)
                offered = max(32, int(rate * 10))
                with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                    requests = list(pool.map(request, range(offered)))
                elapsed = time.perf_counter() - epoch
                outcomes = load_outcomes(requests)
                samples.append({"kind": "scheduled_load", "size": size, "phase": "merged", "concurrency": concurrency, "offered_rate": rate, "repeat": repeat,
                                **outcomes, "elapsed_seconds": elapsed,
                                "samples": requests, "sidecar_counters_delta": [after - prior for after, prior in zip(counter(pair), before)], "resources": resource_snapshot(state)})
                save(evidence / "samples.json", samples)
                assert outcomes["errors"] == 0
                assert shutil.disk_usage(lifecycle.REPO_ROOT).free >= 10 * 1024 ** 3
        started = time.perf_counter()
        stable = []
        while time.perf_counter() - started < 60:
            request_started = time.perf_counter()
            actual = workspace.search("metrics", vector, limit=5)
            assert [r["reference"] for r in actual] == expected_refs
            stable.append({"seconds": time.perf_counter() - request_started, "correct": True})
            assert shutil.disk_usage(lifecycle.REPO_ROOT).free >= 10 * 1024 ** 3
        samples.append({"kind": "stability", "policy": "closed-loop one outstanding trusted-client query", "duration_seconds": time.perf_counter() - started, "offered": len(stable), "completed": len(stable), "errors": 0, "samples": stable, "resources": resource_snapshot(state)})
    save(evidence / "samples.json", samples)
    summary = []
    for size in sizes:
        for phase in ("delta", "merged"):
            for side in ("plaintext_exact", "encrypted"):
                rows = [s for s in samples if s["kind"] == "query" and s["size"] == size and s["phase"] == phase and s["side"] == side]
                times = [s["search_seconds"] for s in rows]
                repeat_medians = [statistics.median(r["search_seconds"] for r in rows if r["repeat"] == repeat) for repeat in range(1 if smoke else 5)]
                summary.append({"size": size, "phase": phase, "side": side, "repeats": len(repeat_medians), "per_repeat_medians_seconds": repeat_medians, "median_repeat_seconds": statistics.median(repeat_medians), "range_repeat_seconds": [min(repeat_medians), max(repeat_medians)], "median_seconds": statistics.median(times), "range_seconds": [min(times), max(times)], **latency_summary(times), "quality_mean": {key: statistics.mean(r["quality"][key] for r in rows) for key in rows[0]["quality"]}})
    save(evidence / "summary.json", {"status": "passed", "smoke_only": smoke, "comparisons": summary, "errors": owner.traffic["http_errors"] + sum(s.get("errors", 0) for s in samples), "rejected": sum(s.get("rejected", 0) for s in samples), "model_revision": MODEL_REVISION})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(".agents/model-cache"))
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("full native CGO app measurement requires Ubuntu")
    assert shutil.disk_usage(lifecycle.REPO_ROOT).free >= 10 * 1024 ** 3
    state = lifecycle.start(args.run_id)
    evidence = lifecycle.EVIDENCE_ROOT / args.run_id
    try:
        lifecycle.doctor(args.run_id)
        save(evidence / "environment.json", {"source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=lifecycle.REPO_ROOT, text=True).strip(), "python": sys.version, "packages": subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True).splitlines(), "platform": platform.platform(), "cpu_count": os.cpu_count(), "cpu": platform.processor(), "load_average": os.getloadavg(), "thread_environment": {key: os.environ.get(key) for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "TOKENIZERS_PARALLELISM")}, "model_revision": MODEL_REVISION, "cache": "hash-checked pinned CPU encoder; one unreported warmup; warm in-memory plaintext vectors and warm sidecar OS cache", "baseline_scope": "same float32 vectors and exact cosine ranking in trusted client; excludes encryption/ORAM/network/durability", "command": sys.argv})
        drive(state, args.cache_dir, args.smoke)
    except BaseException as error:
        save(evidence / "benchmark-failure.json", {"status": "failed", "error_type": type(error).__name__, "error": str(error)[:1000]})
        raise
    finally:
        save(evidence / "benchmark-cleanup.json", lifecycle.cleanup(args.run_id))


if __name__ == "__main__":
    main()
