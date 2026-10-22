# Search the current session

The trusted client computes local sentence embeddings and searches the encrypted index through the two loopback storage sidecars.

## Sub-features

- `semantic-ranking`: rank a small synthetic corpus by normalized cosine similarity.
- `oram-read`: retrieve bounded index blocks via Path ORAM and validate ciphertext before use.
- `delta-visibility`: include a newly shared session document before compaction.

## How to get to it (user POV)

- Run `python -m semshare drive --run-id <run-id>` after launching the isolated app and downloading the pinned model.

## Driving it with the verification CLI

Preconditions:

- The isolated app, sidecars, local pinned model, and synthetic corpus are ready.

- **Search:** submit the fixed synthetic query; compare ranked app share/file references with a fresh float64 cosine oracle in workflow evidence.
- **Access shape:** unit tests and the integration driver compare messages and byte counts for repeated same-size ORAM reads; this is an observed shape check, not a privacy proof.
- **Delta:** the driver proves the session's shared references are searchable before merge and hidden from a second local namespace before and after merge.

## Gotchas

- The client sees plaintext documents, vectors, and query text. The prototype does not implement MPC or hide data from its trusted client.
- A bounded semantic fixture proves behavior only for its configured corpus and model.
