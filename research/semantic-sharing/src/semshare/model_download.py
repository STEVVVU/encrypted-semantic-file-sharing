"""Fetch only the pinned public embedding model files and verify their hashes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from huggingface_hub import snapshot_download

from .encoder import verify_model_files

LOCK_PATH = Path(__file__).resolve().parents[2] / "models.lock.json"


def fetch(cache_dir: Path) -> Path:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    snapshot = Path(snapshot_download(
        repo_id=lock["id"],
        revision=lock["revision"],
        cache_dir=cache_dir,
        allow_patterns=list(lock["files"]),
    ))
    verify_model_files(snapshot)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Download the pinned public MiniLM sentence embedding model")
    parser.add_argument("--cache-dir", type=Path, default=Path(".agents/model-cache"))
    args = parser.parse_args()
    print(fetch(args.cache_dir))


if __name__ == "__main__":
    main()
