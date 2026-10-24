# Search the current session

The trusted client computes local sentence embeddings and searches the encrypted index through the two loopback storage sidecars.

## Sub-features

- `semantic-ranking`: rank a small synthetic corpus by normalized cosine similarity.
- `oram-read`: retrieve bounded index blocks via Path ORAM and validate ciphertext before use.
- `delta-visibility`: include a newly shared session document before compaction.
- `full-scan-benchmark`: compare the historical point-read method with the current read-only snapshot on a synthetic index.

## How to get to it (user POV)

- Run `python -m semshare drive --run-id <run-id>` after launching the isolated app and downloading the pinned model.

## Driving it with the verification CLI

Preconditions:

- The isolated app, sidecars, local pinned model, and synthetic corpus are ready.

- **Search:** submit the fixed synthetic query; compare ranked app share/file references with a fresh float64 cosine oracle in workflow evidence.
- **Point-read shape:** unit tests and the integration driver compare messages and byte counts for repeated same-size ORAM reads; this check does not describe full-tree snapshots.
- **Full-scan measurement:** run `python -m semshare.scan_benchmark --run-id <fresh-id> --cache-dir .agents/model-cache` for the sidecar-only comparison. The [retained campaign report](../../../../docs/evidence/scan-20261024/report.md) describes its 64, 512 and 1,024-record measurements and request traces. It does not start the Go/CGO app or exercise uploads.
- **Delta:** the driver proves the session's shared references are searchable before merge and hidden from a second local namespace before and after merge.

## Gotchas

- The client sees plaintext documents, vectors, and query text. The prototype does not implement MPC or hide data from its trusted client.
- A `records()` snapshot performs one read of every bucket in numeric tree order, independent of the query vector, and does not mutate the ORAM revision. Sidecars can distinguish this full-tree trace from a point read and observe stable bucket IDs, request timing, and tree capacity/object count. All remote buckets have fixed size, so sidecars cannot read per-bucket slot occupancy; a local observer of the encrypted client manifest can infer coarse index and stash size from its varying ciphertext length.
- The sidecar-only scan campaign found that the snapshot method makes 508 GET requests per 64-record query and 8,188 per 1,024-record query. Those measurements describe the synthetic local workload only. They do not establish a general ORAM security guarantee, retrieval quality or application upload performance.
- Path ORAM and workspace capacity now allow up to 1024 records per index; the default remains 64. A workspace can contain at most 64 session namespaces. The synthetic full-application benchmark's frozen corpus ceiling remains 64.
- Authenticated ORAM manifests are capped at 4 MiB. The 1024-position/reference/object bounds, up to 256 stashed 4096-byte payloads, and worst-case JSON escaping keep the maximum serialized state below 3 MiB before AEAD overhead.
- A bounded semantic fixture proves behavior only for its configured corpus and model.
