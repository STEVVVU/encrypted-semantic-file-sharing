"""Compare historical point-read and current full-scan Workspace searches."""

from __future__ import annotations

import argparse
import base64
import cProfile
import hashlib
import importlib.util
import json
import os
import platform
import pstats
import shutil
import statistics
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import lifecycle
from .benchmark import TOPICS, corpus, counter, latency_summary, quality, save
from .cli import _model_path
from .encoder import DIMENSION, MODEL_ID, MODEL_REVISION, SentenceEncoder, cosine, verify_model_files
from .oram import BUCKET_BYTES, MAX_CAPACITY, PathORAM, Sidecar, SidecarPair
from .workspace import Workspace

BASELINE_REVISION = "72f07848a8133b36aece9a7e0816ce09f9b2e0f5"
BASELINE_SOURCE = "research/semantic-sharing/src/semshare/oram.py"
SIZES = (64, 512, 1024)
ROUNDS = 5
QUERIES = tuple({"label": label, "text": query} for label, _, query in TOPICS[:4])
MAX_SECONDS = 20 * 60
MAX_RSS_BYTES = 1024 ** 3


def round_order(round_number: int) -> tuple[str, str]:
    return ("point_read", "snapshot") if round_number % 2 == 0 else ("snapshot", "point_read")


def expected_snapshot_gets(capacity: int) -> int:
    height = max(1, (capacity - 1).bit_length())
    return 4 * ((1 << (height + 1)) - 1)


def summarize(samples: list[dict]) -> list[dict]:
    summary = []
    arms = {64: ("point_read", "snapshot"), 512: ("snapshot",), 1024: ("snapshot",)}
    for size, methods in arms.items():
        for method in methods:
            rows = [row for row in samples if row["size"] == size and row["method"] == method]
            if len(rows) != ROUNDS * len(QUERIES) or any(not row["correct_exact_top5"] for row in rows):
                raise RuntimeError(f"incomplete or incorrect sample cell: {size}/{method}")
            if any(
                {row["query_label"] for row in rows if row["round"] == round_number} != {query["label"] for query in QUERIES}
                for round_number in range(ROUNDS)
            ):
                raise RuntimeError(f"sample cell does not cover each held-out query: {size}/{method}")
            round_medians = [
                statistics.median(row["search_seconds"] for row in rows if row["round"] == round_number)
                for round_number in range(ROUNDS)
            ]
            times = [row["search_seconds"] for row in rows]
            latency = latency_summary(times)
            latency["quantile_method"] = latency.pop("method")
            summary.append({
                "size": size,
                "method": method,
                "rounds": ROUNDS,
                "queries_per_round": len(QUERIES),
                "round_medians_seconds": round_medians,
                "median_round_seconds": statistics.median(round_medians),
                "range_round_seconds": [min(round_medians), max(round_medians)],
                **latency,
            })
    return summary


def _source_hashes(evidence: Path | None = None) -> dict[str, str]:
    paths = (
        "research/semantic-sharing/src/semshare/oram.py",
        "research/semantic-sharing/src/semshare/workspace.py",
        "research/semantic-sharing/src/semshare/scan_benchmark.py",
        "research/semantic-sharing/src/semshare/benchmark.py",
        "research/semantic-sharing/src/semshare/encoder.py",
        "research/semantic-sharing/src/semshare/sidecar.py",
        "research/semantic-sharing/src/semshare/cli.py",
        "research/semantic-sharing/src/semshare/lifecycle.py",
        "research/semantic-sharing/pyproject.toml",
        "research/semantic-sharing/requirements.txt",
    )
    hashes = {}
    for path in paths:
        source = (lifecycle.REPO_ROOT / path).read_bytes()
        hashes[path] = hashlib.sha256(source).hexdigest()
        if evidence is not None:
            target = evidence / "source" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source)
    return hashes


def _read_receipt(documents: list[dict], queries: tuple[dict, ...], vectors: list[list[float]]) -> dict:
    inputs = {
        "documents": [{"id": row["id"], "label": row["label"], "text": row["text"]} for row in documents],
        "queries": [{"label": row["label"], "text": row["text"]} for row in queries],
    }
    return {
        "corpus_sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "document_count": len(documents),
        "query_hashes": [{"label": query["label"], "text_sha256": hashlib.sha256(query["text"].encode()).hexdigest()} for query in queries],
        "vector_hashes": [hashlib.sha256(struct.pack(f"<{DIMENSION}f", *vector)).hexdigest() for vector in vectors],
        "vector_representation": "little-endian float32; only SHA-256 hashes are retained",
    }


def _precompute(encoder: SentenceEncoder, documents: list[dict], queries: tuple[dict, ...], deadline: float) -> list[list[float]]:
    texts = [row["text"] for row in documents] + [row["text"] for row in queries]
    vectors = []
    for start in range(0, len(texts), 64):
        _check_client_limits(deadline)
        vectors.extend(encoder.encode(texts[start : start + 64]))
    return [list(struct.unpack(f"<{DIMENSION}f", struct.pack(f"<{DIMENSION}f", *vector))) for vector in vectors]


def _create_workspace(root: Path, pair: SidecarPair, size: int, documents: list[dict], vectors: list[list[float]]) -> Workspace:
    workspace = Workspace.create(root, pair, capacity=size)
    epoch_id = "e-" + uuid.uuid4().hex
    delta_id = "d-" + uuid.uuid4().hex
    PathORAM.create(root / "epochs" / epoch_id, pair, [], size)
    records = []
    for document, vector in zip(documents[:size], vectors[:size], strict=True):
        share_id = "scan-" + document["id"]
        reference = f"{document['id']}:{share_id}"
        packed = struct.pack(f"<{DIMENSION}f", *vector)
        payload = json.dumps({
            "file_id": document["id"],
            "share_id": share_id,
            "vector_b64": base64.b64encode(packed).decode("ascii"),
        }, separators=(",", ":")).encode()
        records.append((reference, payload))
    PathORAM.create(root / "deltas" / delta_id, pair, records, size)
    state = workspace._load()
    state["active_epochs"]["scan"] = epoch_id
    state["deltas"]["scan"] = delta_id
    workspace._save(state)
    return workspace


def _index_state(workspace: Workspace, pair: SidecarPair) -> dict[str, dict]:
    base, delta = workspace.index_paths("scan")
    paths = {"base": base, "delta": delta}
    state = {}
    for name, path in paths.items():
        if path is None:
            continue
        index = PathORAM.open(path, pair)
        state[name] = {
            "revision": index.shape()["revision"],
            "manifest_sha256": hashlib.sha256(index.manifest_path.read_bytes()).hexdigest(),
        }
    return state


def _index_object_roles(workspace: Workspace, pair: SidecarPair) -> dict[str, str]:
    base, delta = workspace.index_paths("scan")
    roles = {}
    for name, path in (("base", base), ("delta", delta)):
        if path is not None:
            roles.update({object_id: name for object_id in PathORAM.open(path, pair)._load_state()["objects"].values()})
    return roles


def _instrument_methods(pair: SidecarPair, counts: dict[str, int], index_gets: dict[str, int], object_roles: dict[str, str]):
    saved = []
    for side in (pair.first, pair.second):
        previous = side.__dict__.get("request")
        original = side.request

        def request(method, *args, _original=original, **kwargs):
            counts[method] = counts.get(method, 0) + 1
            if method == "GET" and args:
                role = object_roles.get(args[0])
                if role is not None:
                    index_gets[role] = index_gets.get(role, 0) + 1
            return _original(method, *args, **kwargs)

        side.request = request
        saved.append((side, previous))
    return saved


def _restore_instrumentation(saved) -> None:
    for side, previous in saved:
        if previous is None:
            del side.request
        else:
            side.request = previous


def _search(workspace: Workspace, pair: SidecarPair, query: list[float], expected: list[tuple[str, float]], method: str, baseline_records) -> tuple[list[dict], dict]:
    methods: dict[str, int] = {}
    index_gets: dict[str, int] = {}
    before = counter(pair)
    before_indexes = _index_state(workspace, pair)
    object_roles = _index_object_roles(workspace, pair)
    saved = _instrument_methods(pair, methods, index_gets, object_roles)
    started = time.perf_counter()
    try:
        if method == "point_read":
            previous = PathORAM.records
            PathORAM.records = baseline_records
            try:
                actual = workspace.search("scan", query, limit=5)
            finally:
                PathORAM.records = previous
        else:
            actual = workspace.search("scan", query, limit=5)
    finally:
        seconds = time.perf_counter() - started
        _restore_instrumentation(saved)
    after = counter(pair)
    after_indexes = _index_state(workspace, pair)
    if len(actual) != min(5, len(expected)) or any(
        row["reference"] != expected[index][0] or abs(row["score"] - expected[index][1]) > 1e-6
        for index, row in enumerate(actual)
    ):
        raise RuntimeError("Workspace search did not match the exact float32-vector top-five oracle")
    delta = [after_value - before_value for after_value, before_value in zip(after, before, strict=True)]
    if method == "snapshot":
        capacity = workspace._load()["capacity"]
        expected_gets = expected_snapshot_gets(capacity)
        per_index_gets = 2 * (expected_gets // 4)
        if methods != {"GET": expected_gets} or delta[1] != 0 or any(
            before_indexes[name] != after_indexes[name] for name in before_indexes
        ) or index_gets != {"base": per_index_gets, "delta": per_index_gets}:
            raise RuntimeError("snapshot search performed writes or changed its manifests/revisions")
        if delta[2] != expected_gets * BUCKET_BYTES:
            raise RuntimeError("snapshot search received an unexpected bucket payload total")
    elif methods.get("GET", 0) == 0 or methods.get("PUT", 0) == 0 or methods.get("DELETE", 0) == 0:
        raise RuntimeError("point-read baseline did not perform its expected remapping writes")
    elif workspace._load()["capacity"] == 64 and delta != [2688, 14_852_096, 14_852_096]:
        raise RuntimeError("64-record point-read baseline did not match its historical 2688-request trace")
    if method == "point_read":
        index_gets = {"base": 0, "delta": methods.get("GET", 0)}
    return actual, {
        "search_seconds": seconds,
        "sidecar_messages": delta[0],
        "sidecar_sent_payload_bytes": delta[1],
        "sidecar_received_payload_bytes": delta[2],
        "http_methods": methods,
        "index_bucket_gets": index_gets,
        "before_indexes": before_indexes,
        "after_indexes": after_indexes,
    }


def _legacy_records(source_path: Path):
    name = "semshare._p28_point_read_baseline"
    spec = importlib.util.spec_from_file_location(name, source_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the retained historical ORAM source")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.PathORAM.records


def _start_sidecars(run_id: str) -> dict:
    lifecycle._validate_run_id(run_id)
    run_dir = lifecycle.RUN_ROOT / run_id
    evidence = lifecycle.EVIDENCE_ROOT / run_id
    if run_dir.exists() or (evidence.exists() and any(evidence.iterdir())):
        raise lifecycle.LifecycleError("run or evidence directory already exists; refusing to reuse it")
    ports = []
    while len(ports) < 2:
        candidate = lifecycle._free_port()
        if candidate not in ports:
            ports.append(candidate)
    lifecycle.RUN_ROOT.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir()
    (run_dir / ".semshare-owned").write_text(run_id, encoding="ascii")
    data_dirs = [str((run_dir / "storage-a").resolve()), str((run_dir / "storage-b").resolve())]
    logs = [str((run_dir / "storage-a.log").resolve()), str((run_dir / "storage-b.log").resolve())]
    state = {
        "schema": 1,
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "storage_urls": [f"http://127.0.0.1:{port}" for port in ports],
        "storage_data_dirs": data_dirs,
        "storage_instance_ids": [run_id + "-a", run_id + "-b"],
        "pids": {},
        "logs": logs,
    }
    lifecycle._write_state(run_dir, state)
    module_env = os.environ.copy()
    module_env["PYTHONPATH"] = str(lifecycle.SOURCE_ROOT) + (os.pathsep + module_env["PYTHONPATH"] if module_env.get("PYTHONPATH") else "")
    try:
        for role, port, data_dir, instance_id, log in zip(
            ("storage_a", "storage_b"), ports, data_dirs, state["storage_instance_ids"], logs, strict=True
        ):
            process = lifecycle._spawn(
                [sys.executable, "-m", "semshare.sidecar", "--host", "127.0.0.1", "--port", str(port), "--data-dir", data_dir, "--instance-id", instance_id],
                lifecycle.REPO_ROOT,
                module_env,
                Path(log),
            )
            state["pids"][role] = process.pid
            lifecycle._write_state(run_dir, state)
        deadline = time.monotonic() + 20
        while True:
            if any(process.poll() is not None for process in (lifecycle._OWNED[pid] for pid in state["pids"].values())):
                raise lifecycle.LifecycleError("an owned sidecar exited before becoming ready")
            try:
                for url, instance in zip(state["storage_urls"], state["storage_instance_ids"], strict=True):
                    with Sidecar(url, instance_id=instance) as sidecar:
                        sidecar.health()
                return state
            except Exception:
                if time.monotonic() >= deadline:
                    raise lifecycle.LifecycleError("owned loopback sidecars did not become ready")
                time.sleep(0.05)
    except BaseException:
        lifecycle.cleanup(run_id)
        raise


def _process_rss_bytes(pid: int, peak: bool = False) -> int | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (field, ctypes.c_size_t) for field in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                )
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return counters.PeakWorkingSetSize if peak else counters.WorkingSetSize
        finally:
            kernel32.CloseHandle(handle)
    status = Path(f"/proc/{pid}/status")
    if not status.is_file():
        return None
    field = "VmHWM" if peak else "VmRSS"
    for line in status.read_text(encoding="ascii").splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1]) * 1024
    return None


def _check_client_limits(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("index scan benchmark exceeded its 20-minute limit")
    if shutil.disk_usage(lifecycle.REPO_ROOT).free < 10 * 1024 ** 3:
        raise RuntimeError("free disk space fell below 10 GiB")
    rss = _process_rss_bytes(os.getpid(), peak=True)
    if rss is None:
        raise RuntimeError("could not measure client RSS")
    if rss >= MAX_RSS_BYTES:
        raise MemoryError("client RSS reached the 1 GiB cap")


def _check_limits(state: dict, deadline: float) -> dict:
    if time.monotonic() >= deadline:
        raise TimeoutError("index scan benchmark exceeded its 20-minute limit")
    free_bytes = shutil.disk_usage(lifecycle.REPO_ROOT).free
    if free_bytes < 10 * 1024 ** 3:
        raise RuntimeError("free disk space fell below 10 GiB")
    pids = {"client": os.getpid(), **state["pids"]}
    rss = {name: _process_rss_bytes(pid, peak=(name == "client")) for name, pid in pids.items()}
    if any(value is None for value in rss.values()):
        raise RuntimeError("could not measure all owned process RSS values")
    total = sum(rss.values())
    if total >= MAX_RSS_BYTES:
        raise MemoryError("client and sidecar RSS reached the 1 GiB cap")
    return {"free_disk_bytes": free_bytes, "process_rss_bytes": rss, "peak_client_plus_current_sidecars_rss_bytes": total}


def _profile(workspace: Workspace, pair: SidecarPair, query: list[float], expected: list[tuple[str, float]], evidence: Path, state: dict, deadline: float) -> None:
    _check_limits(state, deadline)
    before = counter(pair)
    before_indexes = _index_state(workspace, pair)
    methods: dict[str, int] = {}
    index_gets: dict[str, int] = {}
    saved = _instrument_methods(pair, methods, index_gets, _index_object_roles(workspace, pair))
    profiler = cProfile.Profile()
    try:
        actual = profiler.runcall(workspace.search, "scan", query, 5)
    finally:
        _restore_instrumentation(saved)
        profiler.dump_stats(str(evidence / "snapshot-query-64.prof"))
    if [(row["reference"], row["score"]) for row in actual] != expected:
        raise RuntimeError("profiled snapshot query did not match its exact ranking oracle")
    if (
        methods != {"GET": expected_snapshot_gets(64)}
        or index_gets != {"base": 254, "delta": 254}
        or methods.get("PUT", 0)
        or methods.get("DELETE", 0)
        or counter(pair)[1] != before[1]
        or _index_state(workspace, pair) != before_indexes
    ):
        raise RuntimeError("profiled snapshot query changed remote or local index state")
    profile_text = pstats.Stats(profiler).sort_stats("cumulative")
    with (evidence / "snapshot-query-64-profile.txt").open("w", encoding="utf-8") as stream:
        profile_text.stream = stream
        profile_text.print_stats(40)
    rows = sorted(
        ((filename, line, name, calls, self_seconds, cumulative) for (filename, line, name), (primitive, calls, self_seconds, cumulative, _) in profile_text.stats.items()),
        key=lambda row: row[5],
        reverse=True,
    )[:40]
    save(evidence / "snapshot-query-64-profile-source.json", [
        {"file": filename, "line": line, "function": name, "calls": calls, "self_seconds": self_seconds, "cumulative_seconds": cumulative}
        for filename, line, name, calls, self_seconds, cumulative in rows
    ])


def run(run_id: str, cache: Path, profile_only: bool = False) -> None:
    lifecycle._validate_run_id(run_id)
    if (lifecycle.RUN_ROOT / run_id).exists() or (
        (lifecycle.EVIDENCE_ROOT / run_id).exists() and any((lifecycle.EVIDENCE_ROOT / run_id).iterdir())
    ):
        raise lifecycle.LifecycleError("run or evidence directory already exists; refusing to reuse it")
    deadline = time.monotonic() + MAX_SECONDS
    if shutil.disk_usage(lifecycle.REPO_ROOT).free < 10 * 1024 ** 3:
        raise RuntimeError("free disk space is below the 10 GiB benchmark minimum")
    model_path = _model_path(cache)
    verify_model_files(model_path)
    with (Path(__file__).resolve().parents[2] / "models.lock.json").open(encoding="utf-8") as stream:
        model_lock = json.load(stream)
    os.environ.update({"OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "TOKENIZERS_PARALLELISM": "false"})
    import torch

    torch.set_num_threads(2)
    torch.set_num_interop_threads(2)
    encoder = SentenceEncoder(model_path)
    _check_client_limits(deadline)
    document_count = 64 if profile_only else MAX_CAPACITY
    documents = corpus(document_count)
    vectors = _precompute(encoder, documents, QUERIES, deadline)
    document_vectors = vectors[:document_count]
    query_vectors = vectors[document_count:]
    evidence = lifecycle.EVIDENCE_ROOT / run_id
    state = None
    pair = None
    samples = []
    try:
        state = _start_sidecars(run_id)
        pair = SidecarPair(*(Sidecar(url, instance_id=instance) for url, instance in zip(state["storage_urls"], state["storage_instance_ids"], strict=True)))
        source = subprocess.check_output(["git", "show", f"{BASELINE_REVISION}:{BASELINE_SOURCE}"], cwd=lifecycle.REPO_ROOT)
        baseline_path = Path(state["run_dir"]) / "point-read-baseline-oram.py"
        baseline_path.write_bytes(source)
        baseline_hash = hashlib.sha256(source).hexdigest()
        baseline_records = _legacy_records(baseline_path)
        save(evidence / "inputs.json", _read_receipt(documents, QUERIES, vectors))
        save(evidence / "environment.json", {
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=lifecycle.REPO_ROOT, text=True).strip(),
            "source_hashes": _source_hashes(evidence),
            "point_read_baseline": {"commit": BASELINE_REVISION, "path": BASELINE_SOURCE, "source_sha256": baseline_hash, "scope": "only the historical records method is invoked; current PathORAM open/read/load validation and cryptography remain active"},
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "model_file_hashes": model_lock["files"],
            "python": sys.version,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "torch_version": torch.__version__,
            "torch_threads": torch.get_num_threads(),
            "sidecar_only": True,
            "application_server": False,
            "workload": {"diagnostic_only": profile_only, "sizes": [64] if profile_only else SIZES, "rounds": 0 if profile_only else ROUNDS, "profile_queries": 1, "queries": [query["label"] for query in QUERIES], "encoding": "all document and query vectors precomputed in batches of at most 64 outside search timing"},
            "limits": {"wall_seconds": MAX_SECONDS, "max_client_plus_sidecar_rss_bytes": MAX_RSS_BYTES, "minimum_free_disk_bytes": 10 * 1024 ** 3},
        })
        save(evidence / "run.json", {"run_id": run_id, "mode": "two sidecar processes only", "storage_urls": state["storage_urls"], "storage_instance_ids": state["storage_instance_ids"]})
        budget = _check_limits(state, deadline)
        if profile_only:
            workspace = _create_workspace(Path(state["run_dir"]) / "workspace-64-profile", pair, 64, documents, document_vectors)
            oracle = sorted(
                ((f"doc-{index:02}:scan-doc-{index:02}", cosine(query_vectors[0], document_vectors[index])) for index in range(64)),
                key=lambda row: (-row[1], row[0]),
            )[:5]
            _profile(workspace, pair, query_vectors[0], oracle, evidence, state, deadline)
            save(evidence / "profile-diagnostic.json", {
                "status": "passed", "diagnostic_only": True, "timed_campaign": False,
                "fixture_records": 64, "profile_queries": 1, "errors": 0,
                "resources": _check_limits(state, deadline),
            })
            return
        workspaces = {}
        for size in SIZES:
            methods = ("point_read", "snapshot") if size == 64 else ("snapshot",)
            for method in methods:
                budget = _check_limits(state, deadline)
                workspaces[(size, method)] = _create_workspace(
                    Path(state["run_dir"]) / f"workspace-{size}-{method}", pair, size, documents[:size], document_vectors[:size]
                )
                _check_limits(state, deadline)
        for round_number in range(ROUNDS):
            for size in SIZES:
                methods = round_order(round_number) if size == 64 else ("snapshot",)
                results_this_round = {}
                for method in methods:
                    workspace = workspaces[(size, method)]
                    current = []
                    for query_index, query in enumerate(QUERIES):
                        budget = _check_limits(state, deadline)
                        query_vector = query_vectors[query_index]
                        oracle = sorted(
                            ((f"doc-{index:02}:scan-doc-{index:02}", cosine(query_vector, document_vectors[index]), document["label"])
                             for index, document in enumerate(documents[:size])),
                            key=lambda row: (-row[1], row[0]),
                        )
                        actual, trace = _search(workspace, pair, query_vector, [(row[0], row[1]) for row in oracle[:5]], method, baseline_records)
                        current.append((query["label"], [(row["reference"], row["score"]) for row in actual]))
                        sample = {
                            "round": round_number,
                            "size": size,
                            "method": method,
                            "query_label": query["label"],
                            "query_text_sha256": hashlib.sha256(query["text"].encode()).hexdigest(),
                            "query_vector_sha256": hashlib.sha256(struct.pack(f"<{DIMENSION}f", *query_vector)).hexdigest(),
                            "actual_top5": [{"reference": row["reference"], "score": row["score"]} for row in actual],
                            "oracle_top5": [{"reference": row[0], "score": row[1]} for row in oracle[:5]],
                            **trace,
                            "quality": quality([row[2] for row in oracle], query["label"]),
                            "correct_exact_top5": True,
                            "errors": 0,
                            "rejected": 0,
                        }
                        samples.append(sample)
                        if sample["search_seconds"] < 0:
                            raise RuntimeError("monotonic clock returned a negative search duration")
                        budget = _check_limits(state, deadline)
                        save(evidence / "samples.json", samples)
                    results_this_round[method] = current
                if size == 64:
                    if results_this_round["point_read"] != results_this_round["snapshot"]:
                        raise RuntimeError("point-read and snapshot arms returned different exact query results")
        summary = summarize(samples)
        profile_oracle = sorted(
            ((f"doc-{index:02}:scan-doc-{index:02}", cosine(query_vectors[0], document_vectors[index])) for index in range(64)),
            key=lambda row: (-row[1], row[0]),
        )[:5]
        _profile(workspaces[(64, "snapshot")], pair, query_vectors[0], profile_oracle, evidence, state, deadline)
        save(evidence / "resources.json", _check_limits(state, deadline))
        save(evidence / "summary.json", {
            "status": "passed",
            "scope": "CPU-only semantic index and two local sidecars; no SecuShare application server or upload path",
            "round_order": [list(round_order(round_number)) for round_number in range(ROUNDS)],
            "comparison": "64-record historical point-read method versus snapshot on fresh equivalent indexes; 512 and 1024 snapshot costs only",
            "comparisons": summary,
            "total_queries": len(samples),
            "errors": sum(sample["errors"] for sample in samples),
            "rejected": sum(sample["rejected"] for sample in samples),
        })
    except BaseException as error:
        if evidence.exists():
            save(evidence / "benchmark-failure.json", {"status": "failed", "error_type": type(error).__name__, "error": str(error)[:1000], "samples_written": len(samples)})
        raise
    finally:
        try:
            if pair is not None:
                pair.close()
        finally:
            if state is not None:
                save(evidence / "benchmark-cleanup.json", lifecycle.cleanup(run_id))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=lifecycle.new_run_id())
    parser.add_argument("--cache-dir", type=Path, default=Path(".agents/model-cache"))
    parser.add_argument("--profile-only", action="store_true", help="profile one real 64-record snapshot query; no timing campaign")
    args = parser.parse_args()
    try:
        run(args.run_id, args.cache_dir, args.profile_only)
    except BaseException as error:
        print(f"scan benchmark failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
