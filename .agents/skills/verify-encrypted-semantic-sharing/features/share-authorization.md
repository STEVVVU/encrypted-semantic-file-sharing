# Authorize, import, and revoke a share

SecuShare controls file access through its existing share API. The research client imports an authorized document into a separate locally keyed recipient workspace only after parsing the share ID and key from the link; the key is in the fragment and is not part of the HTTP request path.

## How to drive it

Run `python -m semshare drive --run-id <run-id>` after launch and model setup. `workflow.json` records the locally parsed fragment, recipient's ciphertext-only response, checksum verification, separate recipient corpus, owner's private-file denial, and post-revocation HTTP status.

The deterministic integration checks use three distinct guest sessions: owner, authorized recipient, and an unprivileged observer. The observer receives share ciphertext and cannot decrypt it without the fragment key. The recipient can decrypt/import the active grant, then the owner revokes it and the recipient's next fetch receives HTTP 410. This demonstrates future request denial only; it cannot erase plaintext or keys learned before revocation.

Session IDs used for local index partitions are not authorization principals. They do not establish isolation from a holder of the shared workspace root key.
