# Encrypted semantic file sharing

Local semantic search over encrypted shared documents, built on the complete [SecuShare application](https://github.com/SecuShare/SecuShare). The existing React/TypeScript frontend and Go/SQLite backend remain intact. A Python research client uses the app's real upload, share, download and revocation APIs, with a local sentence encoder and two ciphertext storage sidecars.

The added workflow combines client-side AES-GCM, a pinned MiniLM encoder, bounded Path ORAM indexes, private session deltas and immutable epoch merges. A recipient imports an explicit share link into a separate search corpus. File keys stay in the link fragment and trusted client state. Revocation prevents future server downloads; it cannot erase copies or keys a recipient already obtained.

## Run and verify

See [research client setup and commands](research/semantic-sharing/README.md) and the [maintained verification skill](.agents/skills/verify-encrypted-semantic-sharing/SKILL.md). Native app verification needs Python 3.11, Go with CGO, and a C compiler. The driver launches only synthetic documents and loopback services, then preserves evidence while cleaning its own processes and scratch data.

```sh
python -m pip install -r research/semantic-sharing/requirements.txt
python -m pip install -e research/semantic-sharing --no-deps
python -m semshare download-model
python -m semshare launch
python -m semshare doctor --run-id <printed-run-id>
python -m semshare drive --run-id <printed-run-id>
python -m semshare cleanup --run-id <printed-run-id>
```

The deterministic storage and session tests run with `python -m unittest discover -s research/semantic-sharing/tests -v` after installation. The [native Ubuntu workflow](https://github.com/STEVVVU/encrypted-semantic-file-sharing/actions/runs/39721059173) passed frontend lint/tests/build, backend native race tests, 22 research tests, and the complete app/sidecar journey. [Retained synthetic evidence](docs/evidence/linux-39721059173/) records three encrypted uploads, recipient isolation, HTTP 403 private-file denial, HTTP 410 after revocation, cosine-ranking agreement, ORAM invariants and owned-process cleanup. The original workflow evidence uses `model.id` for the pinned revision hash; the model identity is the locked MiniLM model described above.

## Current index-only scan results

Two complete CPU-only campaigns compared the historical 64-record point-read search with the current snapshot scan. Both used the same pooled HTTPX transport and alternated the two methods over five rounds. At 64 records, the median of five round medians was 4.1747 s for point reads and 0.4004 s for snapshots in run A. Run B measured 4.7976 s and 0.4643 s. At 512 and 1,024 records, snapshot medians were 3.3072 s and 6.5588 s in A, and 3.7216 s and 7.4098 s in B. Each cell had 20 queries. The linked [campaign report and receipts](docs/evidence/scan-20261024/report.md) include ranges, request counts, bytes, source and model hashes, resource limits, and cleanup evidence.

This benchmark uses two loopback sidecars and does not start the SecuShare app or upload documents. A snapshot query reads 508 buckets at 64 records and 8,188 at 1,024 records, including the empty base tree. It receives 8,420,608 and 135,724,288 payload bytes per query at those sizes. Sidecars see the full-tree trace. The result does not establish general ORAM security, retrieval quality, WAN performance, production capacity, or a full-app upload result. The complete application integration still uses its separate 64-record workflow.

Run A's original summary has incorrect method labels. Its saved sample rows retain the actual labels, and the checker recomputes its summary without rewriting the receipt. Run B stores arm labels and all actual/oracle top-five pairs. Both complete runs passed all 80 queries with zero errors or rejections.

## Historical full-application metrics

The [hosted metrics run](https://github.com/STEVVVU/encrypted-semantic-file-sharing/actions/runs/39733590445) completed the [frozen corpus benchmark](.agents/skills/verify-encrypted-semantic-sharing/features/metrics.md) at source `e49209b`, using a four-vCPU Ubuntu runner, Python 3.11 and pinned CPU MiniLM revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. Four manually labeled paraphrase queries were repeated five times per cell, alternating encrypted/plaintext order. Every top-five reference and score matched exact float32-vector ranking. Search medians below are medians of the five per-repeat medians, with their range; query embedding is excluded.

This retained table is the historical point-read scan result from source `e49209b`. Its encrypted search performed one remapping ORAM read per record; it predates the current read-only full-tree snapshot and is not a measurement of the current search path.

| Records | Plain exact, delta / merged (ms) | Encrypted delta (s) | Encrypted merged (s) |
|---:|---:|---:|---:|
| 8 | 1.138 / 1.144 | 0.216 [0.212–0.217] | 0.212 [0.212–0.215] |
| 32 | 4.475 / 4.448 | 1.279 [1.270–1.302] | 1.287 [1.277–1.305] |
| 64 | 8.884 / 8.795 | 2.976 [2.955–3.016] | 3.001 [2.936–3.228] |

The plaintext path is an overhead floor that omits encryption, ORAM, network and durability. Warm query embedding took about 10 ms; at 64 records merged-search empirical p95 was 3.361 s across 20 queries. Four-query synthetic relevance had MRR/nDCG@5 1.0; precision@5 was 0.2/0.8/1.0 and recall@5 1.0/1.0/0.625 at 8/32/64 records. At 64 records there are eight same-topic documents, so top five can recover at most 5/8. These labels do not establish general document-search quality.

Scheduled rates 0.25/1/4 queries/s with worker limits 1/2/4 completed all 520 requests, with zero errors, rejections or timeouts. Median completed rates were **0.252/0.336/0.340 queries/s**. The overloaded 1/s and 4/s batches had per-run p95 ranges **61.4–65.5 s** and **102.0–105.4 s**, including queued time. Finite backlog completion is not sustainable 4/s capacity. A separate 61.4-second closed-loop stability check completed 21 correct searches. Per-cell p99 is omitted because there were fewer than 100 observations.

The separate historical point-read profile attributes the 64-record path to 64 serial ORAM reads/path commits and 2,688 sidecar requests under the trusted-client lock. Each query sent and received 14,852,096 payload bytes; headers/framing are excluded. Client peak RSS was 478,284 KiB. Before load testing, aggregate retained-index sidecar storage was 20,388,480 bytes across all three corpus workspaces and their retained epoch/delta trees, versus 7,744 unique uploaded plaintext bytes. This is not active-corpus storage amplification: superseded path buckets are deleted after durable commit, while whole old index generations remain.

[Curated cells, counts, hashes and limits](docs/evidence/metrics-39733590445/measurement.json) bind these results to the raw workflow artifact; cleanup stopped all three owned processes and removed scratch data. Reproduce with `python -m semshare.benchmark --run-id <fresh-id> --cache-dir <isolated-model-cache>` or the manual [metrics workflow](.github/workflows/metrics.yml). Ingest was one unreplicated 64-document pass at one document per 4.2 seconds, below the shipped file-request limiter; its success is not an upload-capacity measurement or a formal privacy result.

## Trust and current scope

This is a trusted-client research prototype. It implements a position map, stash, random leaf remapping, padded encrypted buckets and path eviction. Each index supports up to 1024 records, with a default workspace capacity of 64; a workspace can contain at most 64 session namespaces. Current `records()` snapshots read every bucket once in numeric tree order and do not remap records. Searches then rank the locally returned records; this is not a scalable private approximate-nearest-neighbor service. Session names separate views inside one trusted client, while separate workspace keys define separate client corpora.

The two sidecars are loopback processes on one machine. They do not establish independent non-colluding operators, MPC, malicious-client security or formal privacy. The app still stores filename, type and size metadata. Sidecars can distinguish full-tree scans from point reads and observe request timing, stable bucket object IDs, and tree capacity/object count. Fixed-size encrypted buckets hide per-bucket occupancy from sidecars; a local observer of the encrypted client manifest can infer coarse index and stash size from ciphertext length. See the [detailed trust boundary](research/semantic-sharing/README.md#trust-and-privacy-boundary).

The [index-only scan benchmark](research/semantic-sharing/README.md#index-only-scan-benchmark) compares the current path against the historical 64-record point-read implementation and measures snapshot scans at 512 and 1,024 records. It uses two loopback sidecars and does not start the Go/CGO app. See the [measured results and scope limits](docs/evidence/scan-20261024/report.md).

## Source and research

The full MIT-licensed SecuShare source and history are retained at upstream base `42590a03dbd1a8812ab4ad7b96e924969eea8bc0`. The original landing page is preserved as [README.upstream.md](README.upstream.md), and its original CI is retained under [archive/upstream-workflows](archive/upstream-workflows/). New code is under `research/semantic-sharing/`.

The coursework reference is Berkeley CS262a's [Constellation report](https://people.eecs.berkeley.edu/~kubitron/courses/cs262a-F25/projects/reports/project1006_paper_85262889807149618246.pdf). The ORAM adaptation follows [Path ORAM](https://pages.cs.wisc.edu/~yxy/pubs/PathORam.pdf). This project does not reproduce Constellation's distributed MPC protocol or either paper's scale and performance results.
