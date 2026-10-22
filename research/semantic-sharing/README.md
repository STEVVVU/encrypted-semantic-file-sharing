# Encrypted semantic sharing

This research client adds local semantic search to the MIT-licensed SecuShare application in this repository. It uses SecuShare's existing guest authentication, client-side encrypted upload, and share-download API, then stores document references and locally computed embeddings through a bounded Path ORAM client and two loopback object sidecars.

The application remains a usable, self-hosted encrypted file-sharing product. This directory contains an opt-in research workflow; it does not change the application's web interface or add a required model service. The pinned sentence encoder runs locally on CPU and can be downloaded ahead of an offline run.

## Trust and privacy boundary

The trusted party is the client process and its local filesystem. That process handles plaintext documents, file keys, embedding vectors, the ORAM position map, stash, and encryption keys. SecuShare continues to receive the encrypted file bytes and the metadata required by its existing upload and share APIs. Storage sidecars receive fixed-length XOR shares of encrypted, padded ORAM buckets. Each sidecar is constrained to IPv4 loopback by this prototype.

The two sidecars are separate processes, but this implementation does not provide MPC, remote non-colluding operators, malicious-server security, or a proof that metadata is hidden from colluding sidecars. Sidecars can observe request timing, object identifiers, and access traces. A host administrator can inspect both sidecars and the client. The client-side model is not a private inference service. These limits make this a trusted-client prototype inspired by Path ORAM, not the paper's stronger multi-party setting.

The ORAM is deliberately bounded to 64 records per index, four encrypted slots per bucket, 4 KiB records, and 256 stash entries. Each client session namespace has an independent base epoch and delta index. Session IDs partition local indexes; they are not authentication identities or a security boundary. The workspace root key and trusted client control all local namespaces. Sharing to another user requires SecuShare's actual share grant and its key-bearing URL fragment; the recipient can import only that granted file into a separate locally keyed workspace. A merge builds a fresh immutable base on both sidecars before atomically switching that session's encrypted local workspace pointer. A failed merge leaves the old base active and its delta available. Previous epochs and merged delta objects remain on the sidecars for recovery; this research client does not implement remote garbage collection.

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

The runtime commands require outbound access only for the initial Go module and model downloads. The embedding model is pinned by repository revision and all six model files are checked against SHA-256 hashes in `models.lock.json`; encoder inference itself is local-only.

## Embedding model

The client uses `sentence-transformers/all-MiniLM-L6-v2`, pinned to revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, with Apache-2.0 model-card licensing. It computes attention-mask-weighted mean pooling over the final hidden state, L2 normalizes the 384-dimensional vector, and truncates to 256 tokens. See the [model card](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/tree/1110a243fdf4706b3f48f1d95db1a4f5529b4d41) and [Path ORAM paper](https://pages.cs.wisc.edu/~yxy/pubs/PathORam.pdf).
