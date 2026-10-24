# Encrypted semantic sharing

This research client adds local semantic search to the MIT-licensed SecuShare application in this repository. It uses SecuShare's existing guest authentication, client-side encrypted upload, and share-download API, then stores document references and locally computed embeddings through a bounded Path ORAM client and two loopback object sidecars.

The application remains a usable, self-hosted encrypted file-sharing product. This directory contains an opt-in research workflow; it does not change the application's web interface or add a required model service. The pinned sentence encoder runs locally on CPU and can be downloaded ahead of an offline run.

## Trust and privacy boundary

The trusted party is the client process and its local filesystem. That process handles plaintext documents, file keys, embedding vectors, the ORAM position map, stash, and encryption keys. SecuShare continues to receive the encrypted file bytes and the metadata required by its existing upload and share APIs. Storage sidecars receive fixed-length XOR shares of encrypted, padded ORAM buckets. Each sidecar is constrained to IPv4 loopback by this prototype.

The two sidecars are separate processes, but this implementation does not provide MPC, remote non-colluding operators, malicious-server security, or a proof that metadata is hidden from colluding sidecars. Sidecars can distinguish a full-tree snapshot from a point read and observe request timing, stable bucket object identifiers, and tree capacity/object count. Fixed-size buckets hide per-bucket slot occupancy from the sidecars; a local observer of the encrypted client manifest can infer coarse index and stash size from its ciphertext length. A host administrator can inspect both sidecars and the client. The client-side model is not a private inference service. These limits make this a trusted-client prototype inspired by Path ORAM, not the paper's stronger multi-party setting.

Each ORAM index supports up to 1024 records; the default workspace capacity remains 64 records per index. A workspace can contain at most 64 session namespaces, each with an independent base epoch and delta index. The ORAM keeps four encrypted slots per bucket, 4 KiB records, and at most 256 client-stash entries. Session IDs partition local indexes; they are not authentication identities or a security boundary. The workspace root key and trusted client control all local namespaces. Sharing to another user requires SecuShare's actual share grant and its key-bearing URL fragment; the recipient can import only that granted file into a separate locally keyed workspace. A merge builds a fresh immutable base on both sidecars before atomically switching that session's encrypted local workspace pointer. A failed merge leaves the old base active and its delta available. Previous epochs and merged delta objects remain on the sidecars for recovery; this research client does not implement remote garbage collection.

## Run the isolated proof

Use Python 3.11+, Go 1.27.3 with CGO and a C compiler enabled, and enough disk for PyTorch and the pinned model. The verifier creates a disposable SQLite database, upload directory, Go caches, and two local sidecar stores under `.agents/run-data/<run-id>`. It refuses to reuse a run directory and stops only processes whose identity matches that run. Evidence is preserved under `.agents/evidence/runs/<run-id>` after cleanup.

From the repository root:

```sh
python -m pip install -r research/semantic-sharing/requirements.txt
python -m pip install -e research/semantic-sharing --no-deps
python -m semshare download-model
```

Then run the driver commands in sequence, keeping the printed `run_id`:

```sh
python -m semshare launch
python -m semshare doctor --run-id <run-id>
python -m semshare drive --run-id <run-id>
python -m semshare cleanup --run-id <run-id>
```

The CI workflow also injects a deliberate failure after launch, verifies all three owned processes are reaped, and checks that the failed result survives cleanup as evidence.

`drive` uses synthetic payment and gardening text. It uploads and shares each text through the real application API, downloads and decrypts the share, embeds the recovered document with the pinned model, searches the session delta, confirms another session cannot see it, merges a new epoch, checks the real app's private-file authorization boundary, and validates the remote ORAM invariants. The evidence JSON contains only synthetic inputs and summary metadata; it omits API tokens, file keys, and vectors.

Run deterministic storage/workspace tests from the repository root:

```sh
python -m unittest discover -s research/semantic-sharing/tests -v
```

## Index-only scan benchmark

The index-only scan benchmark uses only the two loopback sidecars and the locally cached pinned model; it does not start the Go/CGO app or upload documents. It precomputes the fixed corpus and query vectors outside search timing, compares the historical 64-record point-read method with the current full-tree snapshot over five alternating rounds, and measures snapshot-only indexes at 512 and 1024 records. It refuses to download the model, keeps only input/vector hashes in retained evidence, and enforces 10 GiB free disk, 1 GiB client-plus-sidecar RSS, and a cooperative 20-minute run limit. Run it with a fresh run ID:

```sh
python -m semshare.scan_benchmark --run-id <fresh-32-character-hex-id> --cache-dir .agents/model-cache
```

It retains the samples, profile and source-line annotations under `.agents/evidence/runs/<run-id>` and copies the owned sidecar logs there during cleanup. This storage-only check is not the full application journey or a production performance claim.

Two complete campaigns each ran 80 queries and passed with zero errors or rejections. At 64 records, the median of five round medians was 4.1747 s for point reads and 0.4004 s for snapshots in run A. Run B measured 4.7976 s and 0.4643 s. At 512 and 1,024 records, snapshot medians were 3.3072 s and 6.5588 s in A, and 3.7216 s and 7.4098 s in B. Each cell contains 20 queries. The [campaign report and curated receipts](../../docs/evidence/scan-20261024/report.md) include the run ranges, traces, payload bytes, hashes, resource limits and cleanup.

Each 64-record snapshot query made 508 `GET` requests and received 8,420,608 payload bytes. At 1,024 records it made 8,188 `GET` requests and received 135,724,288 payload bytes. Each `GET` returned 16,576 bytes. Snapshot reads left manifests and revisions unchanged, but sidecars can observe the full-tree request trace. The measurements do not establish general ORAM security, retrieval quality, WAN behavior, production capacity or upload performance. The full application workflow remains a separate 64-record check.

Run A's raw summary labels are incorrect, so the receipt checker derives its cell medians from the sample rows. The original summary remains unchanged. Run B records arm labels and every actual/oracle top-five pair. Use `python scripts/check_scan_receipts.py` to validate the committed receipts.

Add `--profile-only` to create a 64-record fixture and profile one real snapshot query without running the timing campaign. Source copies, hashes, profile annotations and cleanup receipts survive cleanup. Sidecar clients reuse HTTPX connections with a two-second I/O timeout, disabled environment proxies and redirects, and bounded unencoded response bodies. Owners must use `with Sidecar(...)`, `with SidecarPair(...)`, or close the clients explicitly after their last request.

The runtime commands require outbound access only for the initial Go module and model downloads. The embedding model is pinned by repository revision and all six model files are checked against SHA-256 hashes in `models.lock.json`; encoder inference itself is local-only.

## Embedding model

The client uses `sentence-transformers/all-MiniLM-L6-v2`, pinned to revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, with Apache-2.0 model-card licensing. It computes attention-mask-weighted mean pooling over the final hidden state, L2 normalizes the 384-dimensional vector, and truncates to 256 tokens. See the [model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/tree/1110a243fdf4706b3f48f1d95db1a4f5529b4d41) and [Path ORAM paper](https://pages.cs.wisc.edu/~yxy/pubs/PathORam.pdf).
