"""Exercise SecuShare's file-sharing boundary before indexing client-side vectors."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

from .app_client import SecuShareClient
from .encoder import DIMENSION, MAX_TEXT_CHARS, SentenceEncoder
from .oram import BLOCK_BYTES
from .workspace import Workspace


def load_text(path: Path) -> bytes:
    data = path.read_bytes()
    if not data or len(data) > 1_000_000:
        raise ValueError("document must contain 1..1,000,000 bytes")
    text = data.decode("utf-8")
    if not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"document text must contain 1..{MAX_TEXT_CHARS} characters")
    return data


def upload_share_and_embed(
    client: SecuShareClient,
    encoder: SentenceEncoder,
    filename: str,
    plaintext: bytes,
) -> tuple[str, bytes, dict]:
    cleartext = plaintext.decode("utf-8")
    uploaded, secret = client.upload_encrypted(filename, plaintext)
    shared = client.create_share(uploaded["id"])
    share_id = shared.get("id")
    if not isinstance(share_id, str) or not share_id:
        raise RuntimeError("SecuShare share response omitted its identifier")
    decrypted, ciphertext, download_nonce = client.download_share(share_id, secret)
    if decrypted != plaintext:
        raise RuntimeError("shared ciphertext did not decrypt to the uploaded source bytes")
    tamper_checks = client.verify_tamper_rejection(ciphertext, {"key": secret["key"], "nonce": download_nonce})
    vector = encoder.encode([decrypted.decode("utf-8")])[0]
    encoded = create_index_record(uploaded["id"], share_id, filename, secret, vector, decrypted, ciphertext)
    return f"{uploaded['id']}:{share_id}", encoded, {
        "file_id": uploaded["id"],
        "share_id": share_id,
        "plaintext": cleartext,
        "vector": vector,
        "secret": secret,
        "download_nonce": download_nonce,
        "tamper_checks": tamper_checks,
    }


def create_index_record(file_id: str, share_id: str, filename: str, secret: dict, vector: list[float], plaintext: bytes, ciphertext: bytes) -> bytes:
    """Pack one verified app share into the bounded encrypted-client index block."""
    packed = struct.pack(f"<{DIMENSION}f", *vector)
    record = {
        "schema": 1,
        "file_id": file_id,
        "share_id": share_id,
        "filename": filename,
        "key_b64": base64.b64encode(secret["key"]).decode("ascii"),
        "nonce_b64": base64.b64encode(secret.get("nonce", b"")).decode("ascii"),
        "vector_b64": base64.b64encode(packed).decode("ascii"),
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
    }
    encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > BLOCK_BYTES:
        raise ValueError("document reference and embedding exceed one fixed ORAM block")
    return encoded


def reference_url(web_base: str, share_id: str, key: bytes) -> str:
    if not web_base.startswith("http://127.0.0.1:") and not web_base.startswith("https://"):
        raise ValueError("web base must be HTTPS or a local loopback URL")
    return f"{web_base.rstrip('/')}/s/{share_id}#{base64.b64encode(key).decode('ascii')}"
