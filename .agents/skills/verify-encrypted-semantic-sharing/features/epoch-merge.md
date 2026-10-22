# Merge a session into a new epoch

The trusted client compacts session additions into a fresh immutable encrypted index epoch, activating it only after both storage sidecars acknowledge their objects.

## Sub-features

- `merge`: build a new base epoch from the old base and session delta.
- `atomic-activation`: switch the client manifest only after both sides are ready.
- `recovery`: retain the previous usable snapshot if a sidecar fails during preparation.

## How to get to it (user POV)

- Run `python -m semshare drive --run-id <run-id>` after launch and model setup.

## Driving it with the verification CLI

Preconditions:

- A working base snapshot, at least one session delta, and both isolated sidecars.

- **Merge:** inspect the new epoch ID, retained old epoch, and session visibility after compaction in `workflow.json`.
- **Search:** query after activation and observe results from the merged snapshot without reading the former delta.
- **Interrupted merge:** an injected sidecar write failure is covered in `test_workspace.py`; the integration driver separately proves the real app's successful merge path.

## Gotchas

- Activation is coordinated by the trusted client and does not provide a distributed atomic commit against malicious clients or colluding servers.
- Revoking future access cannot erase plaintext or keys a recipient already learned.
