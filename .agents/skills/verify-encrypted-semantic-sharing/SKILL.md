---
name: verify-encrypted-semantic-sharing
description: Verify SecuShare's encrypted semantic-sharing CLI against an isolated local app and two loopback storage processes.
---

# Verify encrypted semantic sharing

Status: draft until native Linux CI proves the real app workflow on a host with CGO. Windows unit tests and encoder inference are not substitutes for that proof.

## Launch

Install `research/semantic-sharing/requirements.txt` and the local package (`python -m pip install -e research/semantic-sharing --no-deps`), then fetch the hash-checked model with `python -m semshare download-model`. Start from the repository root with `python -m semshare launch`. It returns a fresh `run_id` and owns the disposable app server, two sidecars, and their run directories. It waits for `/health/ready` and both identified sidecar `/health` endpoints. Do not reuse a run directory or terminate an unrelated listener.

## Doctor

Run `python -m semshare doctor --run-id <run-id>`. It checks that the recorded child PIDs and executable/command identities are alive, the app readiness and loopback endpoints respond, and each sidecar instance ID matches. It does not mutate index data.

## Drive

Run `python -m semshare drive --run-id <run-id>`. It creates guest sessions through `/api/v1/auth/guest`, uploads and shares synthetic text through SecuShare's real APIs, fetches the encrypted shares, decrypts them on the client, embeds them with the pinned local model, and searches the session delta through both sidecars. It checks that another guest cannot fetch an owner's file, a separate locally keyed recipient corpus imports only a real share grant, the share key is parsed from the local URL fragment, revocation denies future fetches, and another local session namespace cannot see the owner's records before or after merge. It also checks ciphertext/nonce mutation rejection and equal fixed-shape sidecar messages for two same-size logical reads.

## Evidence

The driver writes its JSON result or failure record under `.agents/evidence/runs/<run-id>`; cleanup copies the owned app and sidecar logs there before removing scratch data. The result includes exact synthetic document/query references, uploaded/downloaded ciphertext hashes, model revision/dimension, sidecar object counts, ORAM path/bucket counts and invariant results, semantic ranking/cosine oracle, and old/new epoch IDs. Never record bearer/CSRF tokens, keys, plaintext embeddings, or user documents. The CI job uploads this evidence as a workflow artifact. Its post-launch injected-failure command verifies it can stop only the three owned processes and preserve a failed result after deleting that run's scratch directory.

## Cleanup

Run `python -m semshare cleanup --run-id <run-id>`. It stops only child PIDs recorded by this run after verifying their identity, waits for exit, removes the scratch database and sidecar data, and leaves evidence intact. CI runs this during normal cleanup and proves the same boundary after its deliberate failure injection.

## Current block

This driver has not yet run end-to-end locally: the Windows host's upstream SQLite backend requires CGO, which is unavailable in this task environment. `.github/workflows/semantic-sharing.yml` supplies the native Ubuntu proof; keep this skill marked draft until its workflow artifact records a successful run.
