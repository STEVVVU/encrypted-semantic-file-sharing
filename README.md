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

## Trust and scope

The manual [metrics workflow](.github/workflows/metrics.yml) runs the [frozen synthetic corpus benchmark](.agents/skills/verify-encrypted-semantic-sharing/features/metrics.md). Reproduce it with `python -m semshare.benchmark --run-id <fresh-id> --cache-dir <isolated-model-cache>`. It compares exact plaintext ranking with encrypted delta and merged searches at 8, 32 and 64 records, retains held-out relevance labels, and checks every result. Full hosted measurements are pending; functional checks above are not performance results.

This is a trusted-client research prototype. It implements a position map, stash, random leaf remapping, padded encrypted buckets and path eviction, with bounded indexes of 64 records. Searches scan those bounded indexes; this is not a scalable private approximate-nearest-neighbor service. Session names separate views inside one trusted client, while separate workspace keys define separate client corpora.

The two sidecars are loopback processes on one machine. They do not establish independent non-colluding operators, MPC, malicious-client security or formal privacy. The app still stores filename, type and size metadata; timing and storage object identifiers remain visible. See the [detailed trust boundary](research/semantic-sharing/README.md#trust-and-privacy-boundary).

## Source and research

The full MIT-licensed SecuShare source and history are retained at upstream base `42590a03dbd1a8812ab4ad7b96e924969eea8bc0`. The original landing page is preserved as [README.upstream.md](README.upstream.md), and its original CI is retained under [archive/upstream-workflows](archive/upstream-workflows/). New code is under `research/semantic-sharing/`.

The coursework reference is Berkeley CS262a's [Constellation report](https://people.eecs.berkeley.edu/~kubitron/courses/cs262a-F25/projects/reports/project1006_paper_85262889807149618246.pdf). The ORAM adaptation follows [Path ORAM](https://pages.cs.wisc.edu/~yxy/pubs/PathORam.pdf). This project does not reproduce Constellation's distributed MPC protocol or either paper's scale and performance results.
