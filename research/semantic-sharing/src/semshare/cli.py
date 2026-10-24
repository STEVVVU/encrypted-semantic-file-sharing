"""Command line driver for the isolated SecuShare semantic-sharing workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import base64
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import lifecycle


def _emit(value: dict) -> None:
    print(json.dumps(value, sort_keys=True))


def _event(run_id: str, action: str, status: str) -> None:
    lifecycle._validate_run_id(run_id)
    evidence_dir = lifecycle.EVIDENCE_ROOT / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "action": action,
        "status": status,
    }, sort_keys=True)
    with (evidence_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def _model_path(cache: Path) -> Path:
    lock = json.loads((Path(__file__).resolve().parents[2] / "models.lock.json").read_text(encoding="utf-8"))
    return cache / f"models--{lock['id'].replace('/', '--')}" / "snapshots" / lock["revision"]


def drive(run_id: str, cache: Path) -> dict:
    """Run an end-to-end client workflow using only synthetic documents."""
    from .encoder import SentenceEncoder
    from .oram import Sidecar, SidecarPair

    state = lifecycle.load_state(run_id)
    lifecycle.doctor(run_id)
    encoder = SentenceEncoder(_model_path(cache))
    pair = SidecarPair(
        Sidecar(state["storage_urls"][0], instance_id=state["storage_instance_ids"][0]),
        Sidecar(state["storage_urls"][1], instance_id=state["storage_instance_ids"][1]),
    )
    with pair:
        return _drive(state, encoder, pair)


def _drive(state, encoder, pair) -> dict:
    from .app_client import SecuShareClient
    from .workspace import Workspace
    from .workflow import create_index_record, reference_url, upload_share_and_embed

    run_id = state["run_id"]
    workspace_root = Path(state["run_dir"]) / "client-workspace"
    workspace = Workspace.create(workspace_root, pair, capacity=8)

    # These small, synthetic documents exercise lexical and semantic distinctions.
    examples = [
        ("refund.txt", "A customer received reimbursement after the card issuer reversed a duplicate credit card payment.", "refund"),
        ("gardening.txt", "The gardening team planted tomatoes in a greenhouse and watered vegetables each morning.", "gardening"),
        ("security.txt", "A buyer reported an unauthorized card transaction and the issuing bank reversed the disputed charge.", "disputed-charge"),
    ]
    query = "refund for a card payment"
    uploaded = []
    file_evidence = []
    share_secrets = []
    oracle_inputs = []
    owner = SecuShareClient(state["api_url"])
    owner.create_guest()
    for filename, text, label in examples:
        reference, payload, details = upload_share_and_embed(owner, encoder, filename, text.encode("utf-8"))
        workspace.add("investigation", reference, payload)
        uploaded.append(reference)
        url = reference_url(state["app_url"], details["share_id"], details["secret"]["key"])
        share_id_from_link, fragment_key = SecuShareClient.parse_share_link(url, state["app_url"])
        if share_id_from_link != details["share_id"] or fragment_key != details["secret"]["key"]:
            raise RuntimeError("the locally parsed share fragment did not match the created share and client key")
        share_secrets.append({
            "share_id": details["share_id"],
            "secret": details["secret"],
            "plaintext": details["plaintext"].encode("utf-8"),
            "share_url": url,
            "fragment_key": fragment_key,
            "download_nonce": details["download_nonce"],
        })
        file_evidence.append({
            "file_id": details["file_id"],
            "share_id": details["share_id"],
            "synthetic_relevance_label": label,
            "synthetic_plaintext_reference": details["plaintext"],
            "plaintext_sha256": hashlib.sha256(details["plaintext"].encode("utf-8")).hexdigest(),
            "downloaded_ciphertext_sha256": details["secret"]["ciphertext_sha256"],
            "integrity_checks": details["tamper_checks"],
        })
        oracle_inputs.append((reference, label, details["vector"]))

    query_vector = encoder.encode([query])[0]
    delta_results = workspace.search("investigation", query_vector, limit=3)
    from .encoder import cosine

    oracle_results = sorted(
        ((reference, label, cosine(query_vector, vector)) for reference, label, vector in oracle_inputs),
        key=lambda item: (-item[2], item[0]),
    )
    rankings_agree = [item["reference"] for item in delta_results] == [item[0] for item in oracle_results]
    scores_agree = all(
        abs(result["score"] - oracle[2]) <= 1e-6
        for result, oracle in zip(delta_results, oracle_results, strict=True)
    )
    if len(delta_results) != len(examples) or delta_results[0]["reference"] != uploaded[0] or not rankings_agree or not scores_agree:
        raise RuntimeError("semantic search did not rank the refund paraphrase first")
    hidden_from_other_session = workspace.search("another-session", query_vector, limit=3)
    if hidden_from_other_session:
        raise RuntimeError("a session delta leaked into a different session's results")
    before = workspace.state_summary("investigation")["active_epoch"]
    merged = workspace.merge("investigation")
    if merged["previous_epoch"] != before or not merged["previous_epoch_retained"]:
        raise RuntimeError("epoch merge did not retain the old immutable base")
    base_results = workspace.search("investigation", query_vector, limit=3)
    if len(base_results) != len(examples) or base_results[0]["reference"] != uploaded[0]:
        raise RuntimeError("merged epoch did not expose the same semantic results")
    if workspace.search("another-session", query_vector, limit=3):
        raise RuntimeError("another session saw documents after the investigation session merged its epoch")

    # Prove real app authorization: a separate guest cannot fetch the owner's file.
    file_id = uploaded[0].split(":", 1)[0]
    recipient = SecuShareClient(state["api_url"])
    recipient.create_guest()
    recipient_root = Path(state["run_dir"]) / "recipient-workspace"
    recipient_workspace = Workspace.create(recipient_root, pair, capacity=8)
    if recipient_workspace.search("recipient", query_vector, limit=3):
        raise RuntimeError("recipient corpus became visible before its explicit share-link grant")
    share = share_secrets[0]
    share_id_from_link, fragment_key = SecuShareClient.parse_share_link(share["share_url"], state["app_url"])
    if share_id_from_link != share["share_id"]:
        raise RuntimeError("share-link path did not match the created share")
    public_copy, _public_ciphertext, recipient_nonce = recipient.download_share(share_id_from_link, {"key": fragment_key})
    if public_copy != share["plaintext"]:
        raise RuntimeError("a second guest could not decrypt the authorized public share with its client key")
    recipient_secret = {"key": fragment_key, "nonce": recipient_nonce}
    recipient_ciphertext = _public_ciphertext
    recipient_tamper_checks = recipient.verify_tamper_rejection(recipient_ciphertext, recipient_secret)
    recipient_vector = encoder.encode([public_copy.decode("utf-8")])[0]
    recipient_payload = create_index_record(
        file_id,
        share_id_from_link,
        "refund.txt",
        recipient_secret,
        recipient_vector,
        public_copy,
        recipient_ciphertext,
    )
    recipient_workspace.add("recipient", uploaded[0], recipient_payload)
    recipient_results = recipient_workspace.search("recipient", query_vector, limit=3)
    if [result["reference"] for result in recipient_results] != [uploaded[0]]:
        raise RuntimeError("explicit share-link grant did not create an isolated recipient search corpus")
    observer = SecuShareClient(state["api_url"])
    observer.create_guest()
    observer_status, observer_headers, observer_ciphertext = observer.request(
        f"/shares/{share_id_from_link}/file", "POST", b"{}"
    )
    normalized_observer_headers = {key.lower(): value for key, value in observer_headers.items()}
    changed_key = bytearray(fragment_key)
    changed_key[0] ^= 1
    if (
        observer_status != 200
        or hashlib.sha256(observer_ciphertext).hexdigest() != normalized_observer_headers.get("x-checksum-sha256")
        or base64.b64encode(fragment_key).decode("ascii") in observer_headers.values()
    ):
        raise RuntimeError("unauthorized share API response was not ciphertext-only or failed its checksum")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        AESGCM(bytes(changed_key)).decrypt(recipient_nonce, observer_ciphertext, None)
    except InvalidTag:
        observer_ciphertext_only = True
    else:
        raise RuntimeError("share ciphertext decrypted without the URL-fragment key")
    denied_status, _ = recipient.fetch_owned_ciphertext(file_id)
    if denied_status < 400:
        raise RuntimeError("SecuShare allowed a second guest to fetch the owner's private file")
    owner.deactivate_share(share["share_id"])
    revoked_status, _ = recipient.fetch_share_status(share["share_id"])
    if revoked_status != 410:
        raise RuntimeError(f"SecuShare did not return HTTP 410 after share revocation: {revoked_status}")

    pair.first.health()
    pair.second.health()
    base_path, delta_path = workspace.index_paths("investigation")
    from .oram import PathORAM

    index = PathORAM.open(base_path, pair)
    invariants = index.invariant_report()
    if not invariants["valid"]:
        raise RuntimeError(f"ORAM invariant check failed: {invariants}")
    block_ids = list(index._load_state()["positions"])
    if len(block_ids) < 2:
        raise RuntimeError("ORAM shape test requires at least two logical records")
    shape_before = [(item.messages, item.bytes_sent, item.bytes_received) for item in (pair.first, pair.second)]
    if index.read(block_ids[0]) is None:
        raise RuntimeError("ORAM could not read its first committed record")
    shape_middle = [(item.messages, item.bytes_sent, item.bytes_received) for item in (pair.first, pair.second)]
    if index.read(block_ids[-1]) is None:
        raise RuntimeError("ORAM could not read its last committed record")
    shape_after = [(item.messages, item.bytes_sent, item.bytes_received) for item in (pair.first, pair.second)]
    shape_deltas = [
        [(middle[i] - before[i]) for i in range(3)]
        for before, middle in zip(shape_before, shape_middle, strict=True)
    ]
    second_shape_deltas = [
        [(after[i] - middle[i]) for i in range(3)]
        for middle, after in zip(shape_middle, shape_after, strict=True)
    ]
    same_logical_read_shape = shape_deltas == second_shape_deltas
    if not same_logical_read_shape:
        raise RuntimeError("different same-size logical reads had different observed sidecar message shape")
    counts = [len(list(Path(path).glob("*.share"))) for path in state["storage_data_dirs"]]
    if counts[0] != counts[1] or counts[0] == 0:
        raise RuntimeError(f"the two storage sidecars do not contain matching objects: {counts}")
    return {
        "schema": 1,
        "run_id": run_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
        "api": {
            "guest_sessions": 3,
            "uploaded_shared_documents": len(examples),
            "public_share_download": "authorized recipient decrypted the synthetic document",
            "share_link_fragment_key_read_client_side": True,
            "share_link_fragment_key_sent_to_server": False,
            "share_api_without_fragment_returned_ciphertext_only": observer_ciphertext_only,
            "private_file_denial_http": denied_status,
            "future_revoked_share_http": revoked_status,
            "separate_recipient_corpus_records": len(recipient_results),
        },
        "documents": file_evidence,
        "model": {"id": encoder.model_revision, "dimension": 384, "device": "cpu"},
        "semantic_search": {
            "synthetic_query": query,
            "top_reference": delta_results[0]["reference"],
            "top_score": delta_results[0]["score"],
            "ranked_references": [result["reference"] for result in delta_results],
            "ranked_labels": [result[1] for result in oracle_results],
            "fresh_plaintext_cosine_oracle": [{"label": label, "score": score} for _reference, label, score in oracle_results],
            "ranking_agrees_with_oracle": rankings_agree,
            "scores_agree_with_oracle_within_1e-6": scores_agree,
            "recall_at_1_for_synthetic_refund_label": float(oracle_results[0][1] == "refund"),
            "synthetic_corpus_size": len(oracle_results),
        },
        "epoch_merge": merged,
        "session_visibility": {
            "unmerged_other_session_records": len(hidden_from_other_session),
            "other_session_records_after_investigation_merge": 0,
            "authorized_recipient_records": len(recipient_results),
            "recipient_ciphertext_integrity_checks": recipient_tamper_checks,
            "one_authorized_share_imported": len(recipient_results) == 1,
        },
        "oram": {
            "invariants": invariants,
            "shape": index.shape(),
            "same_size_reads_observed_equal_shape": same_logical_read_shape,
            "same_size_read_message_bytes_by_sidecar": shape_deltas,
            "bucket_objects_per_sidecar": counts,
            "delta_deactivated_after_merge": delta_path is None,
            "requests_per_sidecar": [pair.first.messages, pair.second.messages],
        },
        "storage": {"sidecars": 2, "same_shape": counts[0] == counts[1]},
        "data_handling": "All source documents and query are synthetic. Keys and embedding vectors are omitted from evidence.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="semshare")
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("launch", help="start an isolated SecuShare app and two storage sidecars")
    launch.add_argument("--run-id", default=None)
    doctor = commands.add_parser("doctor", help="check the exact processes and loopback endpoints for a run")
    doctor.add_argument("--run-id", required=True)
    drive_parser = commands.add_parser("drive", help="upload synthetic shared files, embed, search, and merge")
    drive_parser.add_argument("--run-id", required=True)
    drive_parser.add_argument("--cache-dir", type=Path, default=Path(".agents/model-cache"))
    model_parser = commands.add_parser("download-model", help="download and hash-check the pinned local model")
    model_parser.add_argument("--cache-dir", type=Path, default=Path(".agents/model-cache"))
    cleanup = commands.add_parser("cleanup", help="stop owned processes and remove only isolated run data")
    cleanup.add_argument("--run-id", required=True)
    inject_failure = commands.add_parser("inject-failure", help="inject a post-launch failure and preserve evidence after owned-process cleanup")
    inject_failure.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    run_id = getattr(args, "run_id", None)
    try:
        if args.command == "launch":
            run_id = args.run_id or lifecycle.new_run_id()
            result = lifecycle.start(run_id)
            _event(run_id, "launch", "passed")
            _emit(result)
        elif args.command == "doctor":
            result = lifecycle.doctor(args.run_id)
            _event(args.run_id, "doctor", "passed")
            _emit(result)
        elif args.command == "drive":
            report = drive(args.run_id, args.cache_dir)
            evidence_dir = lifecycle.EVIDENCE_ROOT / args.run_id
            evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence_file = evidence_dir / "workflow.json"
            temporary = evidence_file.with_suffix(".pending")
            temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temporary.replace(evidence_file)
            _event(args.run_id, "drive", "passed")
            _emit({"status": "passed", "run_id": args.run_id, "evidence": str(evidence_file)})
        elif args.command == "download-model":
            from .model_download import fetch

            _emit({"model_path": str(fetch(args.cache_dir))})
        elif args.command == "cleanup":
            result = lifecycle.cleanup(args.run_id)
            _event(args.run_id, "cleanup", "passed")
            _emit(result)
        elif args.command == "inject-failure":
            lifecycle.doctor(args.run_id)
            raise RuntimeError("injected post-launch failure for cleanup and evidence verification")
        return 0
    except Exception as exc:
        try:
            if run_id:
                _event(run_id, args.command, "failed")
                if args.command == "drive":
                    evidence_dir = lifecycle.EVIDENCE_ROOT / run_id
                    (evidence_dir / "failure.json").write_text(json.dumps({
                        "schema": 1,
                        "run_id": run_id,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                        "traceback": traceback.format_exc(limit=8),
                    }, indent=2) + "\n", encoding="utf-8")
                elif args.command == "inject-failure":
                    evidence_dir = lifecycle.EVIDENCE_ROOT / run_id
                    evidence_dir.mkdir(parents=True, exist_ok=True)
                    failure_record = {
                        "schema": 1,
                        "run_id": run_id,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                        "owned_processes_reaped": False,
                        "stopped_roles": [],
                        "run_data_removed": False,
                    }
                    failure_path = evidence_dir / "failure.json"
                    failure_path.write_text(json.dumps(failure_record, indent=2) + "\n", encoding="utf-8")
                    try:
                        cleanup_result = lifecycle.cleanup(run_id)
                        failure_record.update({
                            "owned_processes_reaped": len(cleanup_result["stopped"]) == 3,
                            "stopped_roles": cleanup_result["stopped"],
                            "run_data_removed": cleanup_result["run_data_removed"],
                        })
                    except Exception as cleanup_error:
                        failure_record["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"[:1000]
                    failure_path.write_text(json.dumps(failure_record, indent=2) + "\n", encoding="utf-8")
        except ValueError:
            pass
        print(f"semshare {args.command} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
