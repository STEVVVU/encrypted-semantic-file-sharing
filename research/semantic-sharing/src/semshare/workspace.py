"""Client-owned immutable epoch plus encrypted per-session ORAM deltas."""

from __future__ import annotations

import base64
import json
import math
import os
import re
import secrets
import shutil
import struct
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .encoder import DIMENSION, cosine
from .oram import BLOCK_BYTES, FileLock, ORAMError, PathORAM, SidecarPair

WORKSPACE_AAD = b"secushare-semshare-workspace-v1"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")


class Workspace:
    """One trusted-client corpus with independent encrypted base/delta per session namespace."""
    def __init__(self, root: Path, sidecars: SidecarPair):
        self.root = root
        self.sidecars = sidecars
        self.key_path = root / "workspace.key"
        self.state_path = root / "workspace.enc"
        self.lock_path = root / "workspace.lock"

    @classmethod
    def create(cls, root: Path, sidecars: SidecarPair, capacity: int = 64) -> "Workspace":
        if not 1 <= capacity <= 64:
            raise ValueError("workspace capacity must be between 1 and 64")
        if root.exists() and any(root.iterdir()):
            raise FileExistsError("workspace directory already contains state")
        root.mkdir(parents=True, exist_ok=True)
        workspace = cls(root, sidecars)
        key = secrets.token_bytes(32)
        workspace.key_path.write_bytes(key)
        try:
            os.chmod(workspace.key_path, 0o600)
        except OSError:
            pass
        sidecars.health()
        workspace._save({"schema": 1, "capacity": capacity, "active_epochs": {}, "deltas": {}})
        return workspace

    @classmethod
    def open(cls, root: Path, sidecars: SidecarPair) -> "Workspace":
        workspace = cls(root, sidecars)
        key = workspace.key_path.read_bytes()
        if len(key) != 32:
            raise ORAMError("workspace key has an invalid length")
        workspace._load()
        sidecars.health()
        return workspace

    def add(self, session_id: str, reference: str, record: bytes) -> str:
        self._validate_session(session_id)
        if not reference or len(reference.encode("utf-8")) > 160 or len(record) > BLOCK_BYTES:
            raise ValueError("reference or record exceeds the configured limit")
        with FileLock(self.lock_path):
            state = self._load()
            if session_id not in state["active_epochs"]:
                epoch = "e-" + uuid.uuid4().hex
                PathORAM.create(self.root / "epochs" / epoch, self.sidecars, [], state["capacity"])
                state["active_epochs"][session_id] = epoch
                self._save(state)
            index = state["deltas"].get(session_id)
            if index is None:
                delta = "d-" + uuid.uuid4().hex
                index_path = self.root / "deltas" / delta
                PathORAM.create(index_path, self.sidecars, [(reference, record)], state["capacity"])
                state["deltas"][session_id] = delta
                self._save(state)
                return delta
            index_path = self.root / "deltas" / index
            return PathORAM.open(index_path, self.sidecars).append(reference, record)

    def search(self, session_id: str, query_vector: list[float], limit: int = 5) -> list[dict]:
        self._validate_session(session_id)
        if len(query_vector) != DIMENSION or not 1 <= limit <= 20:
            raise ValueError("query vector dimension or result count is invalid")
        if any(not math.isfinite(value) for value in query_vector) or all(value == 0 for value in query_vector):
            raise ValueError("query vector must have finite, non-zero values")
        with FileLock(self.lock_path):
            state = self._load()
            epoch = state["active_epochs"].get(session_id)
            candidates = []
            if epoch is not None:
                base = PathORAM.open(self.root / "epochs" / epoch, self.sidecars)
                candidates.extend(base.records())
            delta_id = state["deltas"].get(session_id)
            if delta_id is not None:
                delta = PathORAM.open(self.root / "deltas" / delta_id, self.sidecars)
                candidates.extend(delta.records())
            ranked = []
            for reference, raw in candidates:
                item = json.loads(raw)
                vector = list(struct.unpack(f"<{DIMENSION}f", base64.b64decode(item["vector_b64"], validate=True)))
                score = sum(float(a) * float(b) for a, b in zip(query_vector, vector, strict=True))
                oracle = cosine(query_vector, vector)
                if abs(score - oracle) > 1e-6:
                    raise ORAMError("ranking score diverged from the independent float64 cosine oracle")
                ranked.append({"reference": reference, "file_id": item["file_id"], "share_id": item["share_id"], "score": oracle})
            ranked.sort(key=lambda item: (-item["score"], item["reference"]))
            return ranked[:limit]

    def merge(self, session_id: str) -> dict:
        """Publish a fully written immutable epoch by atomically replacing the local pointer."""
        self._validate_session(session_id)
        with FileLock(self.lock_path):
            state = self._load()
            delta_id = state["deltas"].get(session_id)
            if delta_id is None:
                raise ValueError("session has no delta to merge")
            old_epoch = state["active_epochs"].get(session_id)
            if old_epoch is None:
                raise ORAMError("session has a delta but no active base epoch")
            base = PathORAM.open(self.root / "epochs" / old_epoch, self.sidecars)
            delta = PathORAM.open(self.root / "deltas" / delta_id, self.sidecars)
            records = list(base.records()) + list(delta.records())
            refs = [reference for reference, _ in records]
            if len(refs) != len(set(refs)):
                raise ORAMError("duplicate file reference found during merge")

            epoch = "e-" + uuid.uuid4().hex
            target = self.root / "epochs" / epoch
            try:
                PathORAM.create(target, self.sidecars, records, state["capacity"])
            except Exception as failure:
                try:
                    if target.exists():
                        shutil.rmtree(target)
                except OSError as cleanup_error:
                    raise ExceptionGroup("epoch build failed and local staging cleanup failed", [failure, cleanup_error]) from failure
                raise

            state["active_epochs"][session_id] = epoch
            del state["deltas"][session_id]
            self._save(state)
            return {"previous_epoch": old_epoch, "active_epoch": epoch, "merged_records": len(records), "previous_epoch_retained": (self.root / "epochs" / old_epoch).is_dir()}

    def state_summary(self, session_id: str) -> dict:
        self._validate_session(session_id)
        state = self._load()
        return {
            "active_epoch": state["active_epochs"].get(session_id),
            "has_delta": session_id in state["deltas"],
            "capacity": state["capacity"],
        }

    def index_paths(self, session_id: str) -> tuple[Path, Path | None]:
        self._validate_session(session_id)
        state = self._load()
        delta_id = state["deltas"].get(session_id)
        epoch = state["active_epochs"].get(session_id)
        if epoch is None:
            raise ValueError("session has no active base epoch")
        return self.root / "epochs" / epoch, (self.root / "deltas" / delta_id if delta_id else None)

    def _save(self, state: dict) -> None:
        key = self.key_path.read_bytes()
        aes = AESGCM(key)
        nonce = secrets.token_bytes(12)
        encrypted = nonce + aes.encrypt(nonce, json.dumps(state, separators=(",", ":"), sort_keys=True).encode(), WORKSPACE_AAD)
        temp = self.state_path.with_suffix(".pending")
        with temp.open("wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.state_path)

    def _load(self) -> dict:
        data = self.state_path.read_bytes()
        if len(data) < 28 or len(data) > 16_384:
            raise ORAMError("workspace manifest length is invalid")
        try:
            raw = AESGCM(self.key_path.read_bytes()).decrypt(data[:12], data[12:], WORKSPACE_AAD)
            state = json.loads(raw)
        except Exception as exc:
            raise ORAMError("workspace manifest failed authentication or parsing") from exc
        if state.get("schema") != 1 or not 1 <= state.get("capacity", 0) <= 64:
            raise ORAMError("workspace manifest schema or capacity is unsupported")
        if not isinstance(state.get("active_epochs"), dict) or not isinstance(state.get("deltas"), dict):
            raise ORAMError("workspace manifest is missing its active indexes")
        epoch_id = re.compile(r"e-[0-9a-f]{32}\Z")
        delta_id = re.compile(r"d-[0-9a-f]{32}\Z")
        for collection, pattern in ((state["active_epochs"], epoch_id), (state["deltas"], delta_id)):
            if len(collection) > 64 or any(
                not isinstance(session, str)
                or not SESSION_ID.fullmatch(session)
                or not isinstance(index, str)
                or not pattern.fullmatch(index)
                for session, index in collection.items()
            ):
                raise ORAMError("workspace contains an invalid session or index identifier")
        if set(state["deltas"]) - set(state["active_epochs"]):
            raise ORAMError("workspace contains a session delta without a base epoch")
        return state

    @staticmethod
    def _validate_session(session_id: str) -> None:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("session ID must use 1..48 alphanumeric, underscore, or hyphen characters")
