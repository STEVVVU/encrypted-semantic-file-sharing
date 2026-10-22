# Upload and share a document

The client uses SecuShare's existing guest session, encrypted file upload, share creation, and share download flows, then decrypts the downloaded bytes locally.

For state-changing API requests, the client sends the validated guest CSRF value in both the `X-CSRF-Token` header and matching `csrf_token` cookie. It accepts only the fixed hexadecimal token format returned by the app and refuses redirects to another origin, keeping the cookie inside the local app boundary.

## Sub-features

- `upload`: encrypt and upload synthetic plaintext to the actual app API.
- `share`: create a real share and fetch its ciphertext through the share API.
- `decrypt`: verify authenticated decryption returns the original bytes.

## How to get to it (user POV)

- Start `python -m semshare launch` from the repository root and retain its fresh `run_id`.

## Driving it with the verification CLI

Preconditions:

- A fresh run directory and no preexisting process on the selected loopback ports.

- **Upload:** run `python -m semshare drive --run-id <run-id>`; observe the uploaded file and share references in `.agents/evidence/runs/<run-id>/workflow.json`.
- **Download:** observe the share API return ciphertext whose SHA-256 is recorded in the same evidence file.
- **Decrypt:** the driver fails unless authenticated decryption equals each synthetic source byte-for-byte.

## Gotchas

- The upstream API stores the original filename, MIME type, and sizes as metadata; this workflow does not hide those fields.
- Share access is still governed by the upstream app and its guest session. Do not bypass authentication or authorization.
- Local proof requires Linux/CGO for the upstream SQLite driver. Keep this map in draft until the Ubuntu workflow passes and its evidence artifact is inspected.
