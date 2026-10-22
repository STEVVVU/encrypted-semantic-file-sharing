"""Small Path ORAM client using immutable encrypted buckets split across two sidecars."""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BLOCK_BYTES = 4096
BLOCK_ID_BYTES = 16
BLOCK_LENGTH_BYTES = 4
NONCE_BYTES = 12
TAG_BYTES = 16
BUCKET_SLOTS = 4
PLAIN_BYTES = BLOCK_ID_BYTES + BLOCK_LENGTH_BYTES + BLOCK_BYTES
SLOT_BYTES = NONCE_BYTES + PLAIN_BYTES + TAG_BYTES
BUCKET_BYTES = BUCKET_SLOTS * SLOT_BYTES
MANIFEST_AAD = b"secushare-semshare-manifest-v1"
BLOCK_AAD = b"secushare-semshare-block-v1"
MAX_CAPACITY = 64
MAX_STASH = 256
MAX_REVISION = 100_000


class ORAMError(RuntimeError):
    pass


class Sidecar:
    def __init__(self, base_url: str, timeout: float = 2.0, instance_id: str = ""):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("sidecar URL must use the IPv4 loopback address")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.instance_id = instance_id
        self.messages = 0
        self.bytes_sent = 0
        self.bytes_received = 0

    def request(self, method: str, object_id: str | None = None, data: bytes | None = None) -> bytes:
        path = "/health" if object_id is None else f"/objects/{object_id}"
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Length", str(len(data)))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                result = response.read(BUCKET_BYTES + 1)
                self.messages += 1
                self.bytes_sent += len(data or b"")
                self.bytes_received += len(result)
                if len(result) > BUCKET_BYTES:
                    raise ORAMError("sidecar response exceeded the configured bucket limit")
                return result
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ORAMError(f"sidecar request failed: {method} {self.base_url}") from exc

    def put(self, object_id: str, data: bytes) -> None:
        if len(data) != BUCKET_BYTES:
            raise ORAMError("bucket has an unexpected length")
        self.request("PUT", object_id, data)

    def get(self, object_id: str) -> bytes:
        data = self.request("GET", object_id)
        if len(data) != BUCKET_BYTES:
            raise ORAMError("sidecar returned a bucket with an unexpected length")
        return data

    def delete(self, object_id: str) -> None:
        self.request("DELETE", object_id)

    def health(self) -> None:
        expected = b"ok" if not self.instance_id else f"ok:{self.instance_id}".encode("ascii")
        if self.request("GET") != expected:
            raise ORAMError("sidecar health check failed")


@dataclass(frozen=True)
class SidecarPair:
    first: Sidecar
    second: Sidecar

    def put(self, object_id: str, bucket: bytes) -> None:
        mask = secrets.token_bytes(len(bucket))
        self.first.put(object_id, mask)
        try:
            self.second.put(object_id, bytes(a ^ b for a, b in zip(bucket, mask, strict=True)))
        except ORAMError as exc:
            try:
                self.first.delete(object_id)
            except ORAMError as cleanup_error:
                raise ORAMError("second-side write failed and first-side compensation failed") from cleanup_error
            raise exc

    def get(self, object_id: str) -> bytes:
        first = self.first.get(object_id)
        second = self.second.get(object_id)
        return bytes(a ^ b for a, b in zip(first, second, strict=True))

    def delete(self, object_id: str) -> None:
        self.first.delete(object_id)
        self.second.delete(object_id)

    def health(self) -> None:
        self.first.health()
        self.second.health()


class FileLock:
    """Process lock for the client-owned position map and stash."""

    _local_lock = threading.RLock()

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self.stream = None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        if not self._local_lock.acquire(timeout=self.timeout):
            raise ORAMError("timed out waiting for the client index lock")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = self.path.open("a+b")
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            while True:
                try:
                    self.stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ORAMError("timed out waiting for the client index lock")
                    time.sleep(0.02)
        except BaseException:
            try:
                if self.stream is not None:
                    self.stream.close()
            finally:
                self._local_lock.release()
            raise

    def __exit__(self, *_exc) -> None:
        try:
            assert self.stream is not None
            try:
                self.stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
        finally:
            self._local_lock.release()


class PathORAM:
    def __init__(self, directory: Path, sidecars: SidecarPair, key: bytes):
        self.directory = directory
        self.manifest_path = directory / "manifest.enc"
        self.lock_path = directory / "manifest.lock"
        self.sidecars = sidecars
        self.key = key
        self.aes = AESGCM(key)
        self.gc_failures = 0

    @classmethod
    def create(cls, directory: Path, sidecars: SidecarPair, records: list[tuple[str, bytes]], capacity: int) -> "PathORAM":
        if not 1 <= capacity <= MAX_CAPACITY or len(records) > capacity:
            raise ValueError(f"capacity must be 1..{MAX_CAPACITY} and hold all records")
        if directory.exists() and any(directory.iterdir()):
            raise FileExistsError("ORAM directory already contains state")
        directory.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        key_path = directory / "client.key"
        key_path.write_bytes(key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        result = cls(directory, sidecars, key)
        height = max(1, math.ceil(math.log2(capacity)))
        leaf_count = 1 << height
        node_count = (1 << (height + 1)) - 1
        tree: dict[int, list[bytes]] = {node: [] for node in range(node_count)}
        positions: dict[str, int] = {}
        for ref, record in records:
            if len(record) > BLOCK_BYTES:
                raise ValueError("record exceeds the fixed ORAM block size")
            block_id = secrets.token_bytes(BLOCK_ID_BYTES)
            leaf = secrets.randbelow(leaf_count)
            positions[_b64(block_id)] = leaf
            slot = result._encrypt_block(block_id, record)
            path = _path_nodes(leaf, height)
            for node in reversed(path):
                if len(tree[node]) < BUCKET_SLOTS:
                    tree[node].append(slot)
                    break
            else:
                raise ORAMError("initial Path ORAM placement exceeded its bucket capacity")
        object_ids: dict[str, str] = {}
        for node, slots in tree.items():
            slots.extend(result._dummy_slot() for _ in range(BUCKET_SLOTS - len(slots)))
            object_id = uuid.uuid4().hex
            sidecars.put(object_id, b"".join(slots))
            object_ids[str(node)] = object_id
        state = {
            "schema": 1,
            "height": height,
            "capacity": capacity,
            "revision": 0,
            "positions": positions,
            "objects": object_ids,
            "stash": {},
            "refs": {key: ref for (ref, _), key in zip(records, positions, strict=True)},
        }
        result._save_state(state)
        return result

    @classmethod
    def open(cls, directory: Path, sidecars: SidecarPair) -> "PathORAM":
        key = (directory / "client.key").read_bytes()
        if len(key) != 32:
            raise ORAMError("client key has an invalid length")
        result = cls(directory, sidecars, key)
        result._load_state()
        return result

    def append(self, ref: str, record: bytes) -> str:
        if not ref or len(ref.encode("utf-8")) > 160 or len(record) > BLOCK_BYTES:
            raise ValueError("reference or record exceeds the configured bound")
        with FileLock(self.lock_path):
            state = self._load_state()
            if state["revision"] >= MAX_REVISION or len(state["positions"]) >= state["capacity"]:
                raise ORAMError("ORAM capacity or revision limit reached")
            if ref in state["refs"].values():
                raise ValueError("reference already exists in this index")
            access_leaf = secrets.randbelow(1 << state["height"])
            path = _path_nodes(access_leaf, state["height"])
            tree, stash = self._read_path(state, path)
            block_id = secrets.token_bytes(BLOCK_ID_BYTES)
            new_leaf = secrets.randbelow(1 << state["height"])
            state["positions"][_b64(block_id)] = new_leaf
            state["refs"][_b64(block_id)] = ref
            stash[_b64(block_id)] = record
            object_updates = self._evict_path(state, path, tree, stash)
            self._commit_path(state, path, object_updates, stash)
            return _b64(block_id)

    def read(self, block_id_text: str) -> bytes | None:
        try:
            if not isinstance(block_id_text, str) or len(block_id_text) != 22:
                raise ValueError("block identifier length is invalid")
            block_id = _unb64(block_id_text)
        except (ValueError, TypeError):
            block_id = b""
        if len(block_id) != BLOCK_ID_BYTES:
            block_id = b""
        with FileLock(self.lock_path):
            state = self._load_state()
            if state["revision"] >= MAX_REVISION:
                raise ORAMError("ORAM revision limit reached")
            old_leaf = state["positions"].get(_b64(block_id)) if block_id else None
            access_leaf = old_leaf if old_leaf is not None else secrets.randbelow(1 << state["height"])
            path = _path_nodes(access_leaf, state["height"])
            tree, stash = self._read_path(state, path)
            wanted = _b64(block_id)
            result = stash.get(wanted) if block_id else None
            if block_id and old_leaf is not None:
                state["positions"][wanted] = secrets.randbelow(1 << state["height"])
            object_updates = self._evict_path(state, path, tree, stash)
            self._commit_path(state, path, object_updates, stash)
            return result

    def records(self) -> Iterator[tuple[str, bytes]]:
        state = self._load_state()
        for block_id in tuple(state["positions"]):
            record = self.read(block_id)
            if record is None:
                raise ORAMError("position map references a missing ORAM block")
            yield state["refs"][block_id], record

    def invariant_report(self) -> dict[str, int | bool]:
        state = self._load_state()
        self.sidecars.health()
        all_blocks: dict[str, int] = {}
        for node_text, object_id in state["objects"].items():
            node = int(node_text)
            bucket = self.sidecars.get(object_id)
            for offset in range(0, BUCKET_BYTES, SLOT_BYTES):
                raw = bucket[offset : offset + SLOT_BYTES]
                block_id, payload = self._decrypt_block(raw)
                if block_id == bytes(BLOCK_ID_BYTES):
                    continue
                block_key = _b64(block_id)
                if block_key in all_blocks:
                    raise ORAMError("duplicate block ID found in remote tree")
                all_blocks[block_key] = node
                if block_key not in state["positions"] or not _node_on_path(node, state["positions"][block_key], state["height"]):
                    raise ORAMError("block placement violates the Path ORAM invariant")
        for block_key in state["stash"]:
            if block_key in all_blocks:
                raise ORAMError("block exists in both the stash and remote tree")
            all_blocks[block_key] = -1
        complete = set(all_blocks) == set(state["positions"])
        if len(state["stash"]) > MAX_STASH or not complete:
            raise ORAMError("stash or block-count invariant failed")
        return {"blocks": len(all_blocks), "stash": len(state["stash"]), "capacity": state["capacity"], "valid": True}

    def shape(self) -> dict[str, int]:
        state = self._load_state()
        return {"height": state["height"], "path_nodes": state["height"] + 1, "bucket_slots": BUCKET_SLOTS, "bucket_bytes": BUCKET_BYTES, "revision": state["revision"]}

    def _read_path(self, state: dict, path: list[int]) -> tuple[dict[int, list[bytes]], dict[str, bytes]]:
        tree: dict[int, list[bytes]] = {}
        stash = {_b64(_unb64(key)): _unb64(value) for key, value in state["stash"].items()}
        for node in path:
            object_id = state["objects"].get(str(node))
            if object_id is None:
                raise ORAMError("manifest is missing a path bucket")
            bucket = self.sidecars.get(object_id)
            for offset in range(0, BUCKET_BYTES, SLOT_BYTES):
                raw = bucket[offset : offset + SLOT_BYTES]
                block_id, payload = self._decrypt_block(raw)
                if block_id != bytes(BLOCK_ID_BYTES):
                    block_key = _b64(block_id)
                    if block_key not in state["positions"]:
                        raise ORAMError("remote bucket contains an unknown block ID")
                    if not _node_on_path(node, state["positions"][block_key], state["height"]):
                        raise ORAMError("remote block is stored outside its assigned path")
                    if block_key in stash:
                        raise ORAMError("duplicate block exists in remote path and client stash")
                    stash[block_key] = payload
                # A fetched path enters the client stash; eviction rebuilds each bucket.
            tree[node] = []
        return tree, stash

    def _evict_path(self, state: dict, path: list[int], tree: dict[int, list[bytes]], stash: dict[str, bytes]) -> dict[int, bytes]:
        objects: dict[int, bytes] = {}
        for depth, node in reversed(list(enumerate(path))):
            eligible = [key for key in stash if _node_on_path(node, state["positions"][key], state["height"])]
            eligible.sort(key=lambda key: state["positions"][key])
            while len(tree[node]) < BUCKET_SLOTS and eligible:
                key = eligible.pop(0)
                tree[node].append(self._encrypt_block(_unb64(key), stash.pop(key)))
            while len(tree[node]) < BUCKET_SLOTS:
                tree[node].append(self._dummy_slot())
            if len(tree[node]) != BUCKET_SLOTS:
                raise ORAMError("bucket capacity invariant failed")
            objects[node] = b"".join(tree[node])
        if len(stash) > MAX_STASH:
            raise ORAMError("client stash limit reached; operation aborted")
        return objects

    def _commit_path(self, state: dict, path: list[int], buckets: dict[int, bytes], stash: dict[str, bytes]) -> None:
        new_ids = {node: uuid.uuid4().hex for node in path}
        for node in path:
            self.sidecars.put(new_ids[node], buckets[node])
        previous = {node: state["objects"][str(node)] for node in path}
        state["objects"].update({str(node): object_id for node, object_id in new_ids.items()})
        state["stash"] = {_b64(_unb64(key)): _b64(value) for key, value in stash.items()}
        state["revision"] += 1
        self._save_state(state)
        for object_id in previous.values():
            try:
                self.sidecars.delete(object_id)
            except ORAMError:
                self.gc_failures += 1

    def _encrypt_block(self, block_id: bytes, record: bytes) -> bytes:
        if len(block_id) != BLOCK_ID_BYTES or len(record) > BLOCK_BYTES:
            raise ValueError("invalid block ID or payload length")
        plain = block_id + len(record).to_bytes(BLOCK_LENGTH_BYTES, "big") + record.ljust(BLOCK_BYTES, b"\0")
        nonce = secrets.token_bytes(NONCE_BYTES)
        return nonce + self.aes.encrypt(nonce, plain, BLOCK_AAD)

    def _dummy_slot(self) -> bytes:
        return self._encrypt_block(bytes(BLOCK_ID_BYTES), b"")

    def _decrypt_block(self, slot: bytes) -> tuple[bytes, bytes]:
        if len(slot) != SLOT_BYTES:
            raise ORAMError("encrypted block slot has an invalid length")
        nonce, ciphertext = slot[:NONCE_BYTES], slot[NONCE_BYTES:]
        try:
            plain = self.aes.decrypt(nonce, ciphertext, BLOCK_AAD)
        except Exception as exc:
            raise ORAMError("block authentication failed") from exc
        if len(plain) != PLAIN_BYTES:
            raise ORAMError("decrypted block has an invalid length")
        block_id = plain[:BLOCK_ID_BYTES]
        size = int.from_bytes(plain[BLOCK_ID_BYTES : BLOCK_ID_BYTES + BLOCK_LENGTH_BYTES], "big")
        if size > BLOCK_BYTES:
            raise ORAMError("decrypted record length exceeds the fixed block")
        return block_id, plain[BLOCK_ID_BYTES + BLOCK_LENGTH_BYTES :][:size]

    def _save_state(self, state: dict) -> None:
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = self.aes.encrypt(nonce, json.dumps(state, separators=(",", ":"), sort_keys=True).encode(), MANIFEST_AAD)
        data = nonce + ciphertext
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.manifest_path.with_suffix(".pending")
        with temp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.manifest_path)

    def _load_state(self) -> dict:
        try:
            data = self.manifest_path.read_bytes()
            if len(data) < NONCE_BYTES + TAG_BYTES or len(data) > 1024 * 1024:
                raise ORAMError("encrypted manifest length is invalid")
            raw = self.aes.decrypt(data[:NONCE_BYTES], data[NONCE_BYTES:], MANIFEST_AAD)
            state = json.loads(raw)
        except ORAMError:
            raise
        except Exception as exc:
            raise ORAMError("encrypted manifest failed authentication or parsing") from exc
        if state.get("schema") != 1 or not 1 <= state.get("capacity", 0) <= MAX_CAPACITY:
            raise ORAMError("manifest schema or capacity is unsupported")
        if state.get("revision", MAX_REVISION + 1) > MAX_REVISION:
            raise ORAMError("manifest revision exceeds the configured bound")
        if len(state.get("positions", {})) > state["capacity"] or len(state.get("stash", {})) > MAX_STASH:
            raise ORAMError("manifest exceeds the configured index bounds")
        return state


def _path_nodes(leaf: int, height: int) -> list[int]:
    if not 0 <= leaf < (1 << height):
        raise ValueError("leaf is outside the tree")
    node = (1 << height) - 1 + leaf
    path = [node]
    while node:
        node = (node - 1) // 2
        path.append(node)
    return list(reversed(path))


def _node_on_path(node: int, leaf: int, height: int) -> bool:
    return node in _path_nodes(leaf, height)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
