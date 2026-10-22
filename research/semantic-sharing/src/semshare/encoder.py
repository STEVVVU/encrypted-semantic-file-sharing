"""Local MiniLM sentence embedding with the model card's masked-mean pooling."""

from __future__ import annotations

import math
import hashlib
import json
from pathlib import Path

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
DIMENSION = 384
MAX_TEXT_CHARS = 16_000


class SentenceEncoder:
    def __init__(self, model_path: Path):
        from transformers import AutoModel, AutoTokenizer
        import torch

        self.torch = torch
        verify_model_files(model_path)
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        self.model = AutoModel.from_pretrained(model_path, local_files_only=True, use_safetensors=True)
        if self.model.config.hidden_size != DIMENSION:
            raise ValueError(f"expected embedding dimension {DIMENSION}, found {self.model.config.hidden_size}")
        self.model.eval()
        self.device = torch.device("cpu")
        self.model.to(self.device)
        self.model_revision = MODEL_REVISION

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > 64:
            raise ValueError("encode accepts 1..64 texts")
        if any(not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS for text in texts):
            raise ValueError("texts must be non-empty and within the configured character bound")
        encoded = self.tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=256,
            return_tensors="pt",
        )
        with self.torch.inference_mode():
            output = self.model(**encoded)
            hidden = output.last_hidden_state
            mask = encoded["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
            normalized = self.torch.nn.functional.normalize(pooled, p=2, dim=1)
        vectors = [[float(value) for value in row] for row in normalized.tolist()]
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise ValueError("embedding produced a non-finite component")
        return vectors


def cosine(left: list[float], right: list[float]) -> float:
    """Fresh float64 cosine oracle, independent of the vectorized ranking path."""
    if len(left) != DIMENSION or len(right) != DIMENSION:
        raise ValueError(f"vectors must have dimension {DIMENSION}")
    if any(not math.isfinite(value) for value in (*left, *right)):
        raise ValueError("cosine inputs must be finite")
    dot = math.fsum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(math.fsum(float(a) * float(a) for a in left))
    right_norm = math.sqrt(math.fsum(float(b) * float(b) for b in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("cosine is undefined for a zero vector")
    return dot / (left_norm * right_norm)


def verify_model_files(model_path: Path) -> None:
    lock_path = Path(__file__).resolve().parents[2] / "models.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("id") != MODEL_ID or lock.get("revision") != MODEL_REVISION:
        raise ValueError("model lock does not match the encoder's pinned identity")
    for name, expected in lock["files"].items():
        path = model_path / name
        if not path.is_file():
            raise ValueError(f"pinned model file is missing: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"pinned model file hash mismatch: {name}")
