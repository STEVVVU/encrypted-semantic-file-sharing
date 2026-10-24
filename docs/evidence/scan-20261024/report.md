# Index-only scan benchmark results

The scan benchmark isolates the encrypted index search path. It starts two loopback storage sidecars and includes their HTTP overhead. It does not start the SecuShare app, upload documents, or measure WAN performance. The corpus has 1,024 synthetic documents and four held-out queries. The benchmark prepared all document and query vectors before timing. Each timed search used a 64, 512, or 1,024 record index and checked the exact float32 cosine ranking.

## Two complete runs compare the 64-record methods

Both campaigns completed 80 queries across five alternating rounds. Each round covered four queries per measured cell. The table reports the median of five round medians. The range is the minimum to maximum of those five round medians, not the range of individual query times.

| Run | 64 records, historical point read | 64 records, snapshot | 512 records, snapshot | 1,024 records, snapshot |
|---|---:|---:|---:|---:|
| A | 4.1747 s (3.9128 to 4.3674) | 0.4004 s (0.3931 to 0.4020) | 3.3072 s (3.2354 to 3.5949) | 6.5588 s (6.3945 to 6.5847) |
| B | 4.7976 s (4.2133 to 5.0591) | 0.4643 s (0.4188 to 0.4991) | 3.7216 s (3.3320 to 4.1786) | 7.4098 s (6.3894 to 7.5400) |

At 64 records, the snapshot method took less time in both campaigns. Both methods used the same pooled HTTPX transport inside each campaign, so this comparison isolates their different index-scan mechanisms. The 512- and 1,024-record cells measured the snapshot method only. Their timings show the cost of reading larger trees; they do not compare alternatives at those sizes or establish a scaling benefit.

After connection pooling, a separate profiled snapshot query spent 1.033 of 1.062 profiled seconds in the two `records()` calls that read 508 buckets. This profile identifies the full-tree reads as the remaining work at 64 records. It is diagnostic only; the reported medians come from unprofiled queries.

Run A's saved summary has a metadata defect. Its `method` field contains the quantile-description string for all four cells instead of the arm name. The original summary is retained unchanged. The checker derives A's cell labels and medians from its sample rows using the current `summarize` function. Run B's summary contains the arm names. Its 80 saved actual top-five rankings and scores match their saved oracle rankings and scores exactly. Run A saved correctness flags but not the actual and oracle rank pairs.

## Each method sends a different request trace

The point-read method made 2,688 sidecar requests per query at 64 records. It sent 896 `GET`, 896 `PUT`, and 896 `DELETE` requests. It sent and received 14,852,096 payload bytes per query. The snapshot method read each bucket in both index trees once. It made only `GET` requests and left manifests and revisions unchanged.

| Snapshot index size | `GET` requests per query | Received payload bytes per query |
|---:|---:|---:|
| 64 | 508 | 8,420,608 |
| 512 | 4,092 | 67,828,992 |
| 1,024 | 8,188 | 135,724,288 |

Each `GET` returned 16,576 payload bytes. The base tree was empty but the snapshot still read it. The counters exclude HTTP headers and framing. Sidecars can observe whether a query uses point reads or full-tree scans, request timing, stable bucket IDs, and tree capacity. The scan benchmark does not hide that trace or establish a general ORAM security guarantee.

## Runs and retained evidence

Both runs started from base HEAD `72f07848a8133b36aece9a7e0816ce09f9b2e0f5` with the new working-tree implementation bound by source hashes and retained source copies. Their historical `records()` baseline came from that exact commit. They used Python 3.11.9 and `sentence-transformers/all-MiniLM-L6-v2` revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. The source hash maps differ only for `scan_benchmark.py`. B fixes the summary's arm labels and saves actual and oracle rankings after the timed search; the timed method did not change. Both ran on Windows with 24 reported logical CPUs. Peak client plus sidecar RSS was 890,363,904 bytes in A and 890,310,656 bytes in B, below the 1 GiB limit. Both cleanup receipts say that the two owned sidecars stopped and run data was removed.

Run A is `7e2e2f5d4fce4ae2923ddddb4579b8df`. Run B is `e4f6cfc7611944fa91d583a398a5ab52`. The curated folder keeps the inputs, all samples, summaries, environment, resource and cleanup receipts, profiles, model file hashes, and B's ten retained source files. Text copies use LF line endings. `manifest.json` records each raw source hash, line-ending counts, canonical hash, and relative source path. The checker reconstructs and verifies original byte identities without following the original paths. The manifest also records SHA-256 hashes and ignored paths for each campaign's two 8,271,727-byte sidecar logs, four logs in total. The logs remain outside Git.

The initial campaign `4e45584e1e074178825b7ca30192c68f` stopped after 31 of 80 queries at its cooperative 20-minute deadline. It is retained as a failed run and contributes no result. A separate pre-pool profile attributed 4.362 of 5.448 seconds (80.1%) to socket connection setup. The pooled client was present for both complete campaigns. Profile runs are diagnostics, not additional timing samples or a transport speedup claim.

Run the receipt check from the repository root:

```sh
python scripts/check_scan_receipts.py
python -m unittest research/semantic-sharing/tests/test_scan_receipts.py -v
```

The checker reads only this curated folder. It verifies canonical hashes, run identity, sample coverage, request counts, payload totals, cleanup, limits, and B's saved rank pairs. It recomputes both summaries from the saved rows without changing the original summaries.

## Limits

These measurements cover a synthetic CPU-only index search with two local sidecars. They do not measure the Go/CGO app, upload or sharing APIs, WAN behavior, multiple clients, or production concurrency. The four synthetic queries do not establish general retrieval quality. The measurements do not establish security against a malicious client, colluding operators, or a local observer. The full-app benchmark remains separately frozen at 64 records.
