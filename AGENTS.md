# Repository instructions

- Preserve the upstream SecuShare application, its API contracts, history, and MIT notices. Keep the original README in `README.upstream.md` when replacing the landing page.
- Keep new research code under `research/semantic-sharing/`; do not weaken existing authentication, sharing, or encryption checks.
- The research client must keep plaintext documents, embeddings, keys, and session state in ignored local run directories. Both storage sidecars bind to loopback and store only their assigned randomized share bytes.
- Describe the trusted-client and honest-writer assumptions and metadata leakage. Do not claim MPC, non-collusion, formal security, production readiness, or paper-scale performance.
- Use `frontend/package-lock.json` and isolated project environments. Do not mutate shared tool environments or install machine-wide software.
- Browser verification is CUA-only. Do not use Playwright, direct browser debugging protocols, or hidden browser-state APIs.
- Keep verification scripts and feature maps maintained; raw run data belongs in ignored local directories and curated evidence must survive cleanup.
