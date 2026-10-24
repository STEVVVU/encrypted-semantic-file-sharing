"""Validate the committed, canonicalized scan benchmark receipts."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "research" / "semantic-sharing" / "src"))

from semshare.scan_benchmark import summarize


EXPECTED_CELLS = ((64, "point_read"), (64, "snapshot"), (512, "snapshot"), (1024, "snapshot"))
EXPECTED_GETS = {64: 508, 512: 4092, 1024: 8188}
EXPECTED_RECEIVED_BYTES = {64: 8420608, 512: 67828992, 1024: 135724288}
RUNS = {
    "a": "7e2e2f5d4fce4ae2923ddddb4579b8df",
    "b": "e4f6cfc7611944fa91d583a398a5ab52",
}
RAW_IDENTITIES_SHA256 = "cb9b134c126ac6548646240e6236e575ad071fe876552e0596b6aeee0fb97adc"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_bundle(bundle: Path) -> dict:
    bundle = bundle.resolve()
    manifest = _load(bundle / "manifest.json")
    entries = {entry["path"]: entry for entry in manifest["files"]}
    identities = {name: entry["raw_sha256"] for name, entry in entries.items() if name != "report.md"}
    if (len(entries) != len(manifest["files"])
            or _sha256(json.dumps(identities, sort_keys=True).encode()) != RAW_IDENTITIES_SHA256):
        raise ValueError("original raw receipt identities changed")
    actual_files = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", ".gitattributes"}
    }
    if actual_files != set(entries):
        raise ValueError("receipt inventory does not match manifest")

    for relative, entry in entries.items():
        if (PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_absolute()
                or ".." in PurePosixPath(relative).parts or ".." in PureWindowsPath(relative).parts):
            raise ValueError(f"receipt path escapes the curated folder: {relative}")
        source = entry["source"]
        if Path(source).is_absolute() or PureWindowsPath(source).is_absolute() or ".." in PurePosixPath(source).parts:
            raise ValueError(f"receipt source is not repository-relative: {source}")
        data = (bundle / relative).read_bytes()
        if _sha256(data) != entry["canonical_sha256"]:
            raise ValueError(f"canonical receipt hash mismatch: {relative}")
        if entry["text"] and (b"\r" in data or not data.endswith(b"\n")):
            raise ValueError(f"receipt is not canonical LF text: {relative}")
        endings = entry["raw_line_endings"]
        if entry["text"] and (endings["lone_cr"] or (endings["crlf"] and endings["lone_lf"])):
            raise ValueError(f"unsupported mixed raw line endings: {relative}")
        raw = data.replace(b"\n", b"\r\n") if entry["text"] and endings["crlf"] else data
        if _sha256(raw) != entry["raw_sha256"] or len(raw) != entry["raw_bytes"]:
            raise ValueError(f"original raw receipt identity mismatch: {relative}")

    derived = {}
    loaded = {}
    for label, run_id in RUNS.items():
        run_root = bundle / label
        run = _load(run_root / "run.json")
        env = _load(run_root / "environment.json")
        samples = _load(run_root / "samples.json")
        summary = _load(run_root / "summary.json")
        cleanup = _load(run_root / "benchmark-cleanup.json")
        resources = _load(run_root / "resources.json")
        inputs = _load(run_root / "inputs.json")
        if run["run_id"] != run_id or cleanup["run_id"] != run_id:
            raise ValueError(f"run identity mismatch for {label}")
        if run["mode"] != "two sidecar processes only" or env["application_server"]:
            raise ValueError(f"unexpected benchmark scope for {label}")
        if not cleanup["run_data_removed"] or set(cleanup["stopped"]) != {"storage_a", "storage_b"}:
            raise ValueError(f"cleanup receipt failed for {label}")
        if summary["status"] != "passed" or summary["total_queries"] != 80:
            raise ValueError(f"campaign did not complete for {label}")
        expected_order = [["point_read", "snapshot"] if round_number % 2 == 0 else ["snapshot", "point_read"] for round_number in range(5)]
        if summary["round_order"] != expected_order:
            raise ValueError(f"methods did not alternate for {label}")
        if summary["errors"] or summary["rejected"] or resources["peak_client_plus_current_sidecars_rss_bytes"] > env["limits"]["max_client_plus_sidecar_rss_bytes"]:
            raise ValueError(f"errors or resource limit violation for {label}")
        if resources["free_disk_bytes"] < env["limits"]["minimum_free_disk_bytes"]:
            raise ValueError(f"free disk limit violation for {label}")
        if {row["label"] for row in inputs["query_hashes"]} != {"refund", "garden", "travel", "backup"}:
            raise ValueError(f"query inputs are incomplete for {label}")

        comparisons = summarize(samples)
        if [(row["size"], row["method"]) for row in comparisons] != list(EXPECTED_CELLS):
            raise ValueError(f"sample cells are incomplete for {label}")
        if any(not row["correct_exact_top5"] or row["errors"] or row["rejected"] for row in samples):
            raise ValueError(f"sample correctness or outcome failed for {label}")
        for cell in comparisons:
            rows = [row for row in samples if (row["size"], row["method"]) == (cell["size"], cell["method"])]
            if len(rows) != 20 or {row["query_label"] for row in rows} != {"refund", "garden", "travel", "backup"}:
                raise ValueError(f"query coverage failed for {label}/{cell['size']}/{cell['method']}")
            for row in rows:
                if row["method"] == "point_read":
                    if row["http_methods"] != {"GET": 896, "PUT": 896, "DELETE": 896}:
                        raise ValueError(f"historical trace mismatch for {label}")
                    if row["sidecar_messages"] != 2688 or row["sidecar_sent_payload_bytes"] != 14852096 or row["sidecar_received_payload_bytes"] != 14852096:
                        raise ValueError(f"historical payload mismatch for {label}")
                else:
                    size = row["size"]
                    if row["http_methods"] != {"GET": EXPECTED_GETS[size]} or row["sidecar_sent_payload_bytes"] != 0 or row["sidecar_received_payload_bytes"] != EXPECTED_RECEIVED_BYTES[size]:
                        raise ValueError(f"snapshot trace or payload mismatch for {label}/{size}")
                    if row["before_indexes"] != row["after_indexes"] or row["index_bucket_gets"] != {"base": EXPECTED_GETS[size] // 2, "delta": EXPECTED_GETS[size] // 2}:
                        raise ValueError(f"snapshot changed index state or scanned the wrong trees for {label}/{size}")
                if label == "b" and row["actual_top5"] != row["oracle_top5"]:
                    raise ValueError(f"actual ranking differs from oracle for {row['size']}/{row['query_label']}")

        if label == "b":
            source_entries = {
                path: entry["raw_sha256"]
                for path, entry in entries.items()
                if path.startswith("b/source/")
            }
            expected_sources = env["source_hashes"]
            if len(source_entries) != len(expected_sources):
                raise ValueError("retained B source inventory differs from its environment receipt")
            for source_path, source_hash in expected_sources.items():
                bundled_path = "b/source/" + source_path.replace("\\", "/")
                if source_entries.get(bundled_path) != source_hash:
                    raise ValueError(f"retained source does not match run binding: {source_path}")
            live_source = REPO_ROOT / "research" / "semantic-sharing" / "src" / "semshare" / "scan_benchmark.py"
            bundled_source = bundle / "b/source/research/semantic-sharing/src/semshare/scan_benchmark.py"
            if live_source.read_bytes().replace(b"\r\n", b"\n") != bundled_source.read_bytes():
                raise ValueError("live summary function differs from retained B source")
            for computed, retained in zip(comparisons, summary["comparisons"], strict=True):
                if (computed["size"], computed["method"], computed["median_round_seconds"], computed["range_round_seconds"]) != (
                    retained["size"], retained["method"], retained["median_round_seconds"], retained["range_round_seconds"]
                ):
                    raise ValueError("run B summary differs from its sample rows")

        loaded[label] = {"inputs": inputs, "samples": samples, "summary": summary}
        loaded[label]["environment"] = env
        derived[label] = [
            {
                "size": cell["size"],
                "method": cell["method"],
                "median_of_round_medians_seconds": cell["median_round_seconds"],
                "round_median_range_seconds": cell["range_round_seconds"],
                "samples": cell["samples"],
            }
            for cell in comparisons
        ]

    if loaded["a"]["inputs"] != loaded["b"]["inputs"]:
        raise ValueError("A and B used different corpus, query, or vector inputs")
    for field in ("source_commit", "point_read_baseline", "model_id", "model_revision", "model_file_hashes", "platform", "python", "cpu_count", "torch_version", "torch_threads"):
        if loaded["a"]["environment"].get(field) != loaded["b"]["environment"].get(field):
            raise ValueError(f"A and B environment differs for {field}")
    source_hashes_a = loaded["a"]["environment"]["source_hashes"]
    source_hashes_b = loaded["b"]["environment"]["source_hashes"]
    changed_sources = {path for path in source_hashes_a if source_hashes_a[path] != source_hashes_b.get(path)}
    if changed_sources != {"research/semantic-sharing/src/semshare/scan_benchmark.py"}:
        raise ValueError("A and B source revisions differ outside the benchmark reporting fix")
    return {"runs": RUNS, "derived_comparisons": derived, "validated": True}


if __name__ == "__main__":
    print(json.dumps(verify_bundle(REPO_ROOT / "docs" / "evidence" / "scan-20261024"), indent=2))
