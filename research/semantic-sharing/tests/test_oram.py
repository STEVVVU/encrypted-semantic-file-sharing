from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

from semshare.oram import (
    BLOCK_BYTES,
    BUCKET_BYTES,
    BUCKET_SLOTS,
    MAX_CAPACITY,
    MAX_MANIFEST_BYTES,
    MAX_STASH,
    ORAMError,
    PathORAM,
    Sidecar,
    SidecarPair,
    _b64,
    _unb64,
)
from semshare.sidecar import ObjectStore, handler_for


class PairServers:
    def __init__(self, root: Path):
        self.servers = []
        self.threads = []
        self.sidecars = []
        for name in ("a", "b"):
            base = handler_for(ObjectStore(root / name))
            handler = type("QuietHandler", (base,), {"log_message": lambda *_args: None})
            class OwnedServer(ThreadingHTTPServer):
                def get_request(self):
                    connection, address = super().get_request()
                    self.connections.append(connection)
                    return connection, address

            server = OwnedServer(("127.0.0.1", 0), handler)
            server.connections = []
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append(server)
            self.threads.append(thread)
            self.sidecars.append(Sidecar(f"http://127.0.0.1:{server.server_port}"))
        self.pair = SidecarPair(*self.sidecars)

    def close(self) -> None:
        self.pair.close()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)


class MemorySidecar:
    def __init__(self, objects=None):
        self.objects = {} if objects is None else objects
        self.messages = self.bytes_sent = self.bytes_received = 0
        self.get_ids = []

    def put(self, object_id, data):
        self.objects[object_id] = bytes(data)
        self.messages += 1
        self.bytes_sent += len(data)

    def get(self, object_id):
        if object_id not in self.objects:
            raise ORAMError("missing in-memory bucket")
        data = self.objects[object_id]
        if len(data) != BUCKET_BYTES:
            raise ORAMError("invalid in-memory bucket length")
        self.get_ids.append(object_id)
        self.messages += 1
        self.bytes_received += len(data)
        return data

    def delete(self, object_id):
        self.objects.pop(object_id, None)
        self.messages += 1

    def health(self):
        pass


class MemoryPair:
    def __init__(self):
        self.first = MemorySidecar()
        self.second = MemorySidecar()
        self.pair = SidecarPair(self.first, self.second)


class PathORAMTests(unittest.TestCase):
    def test_read_append_missing_and_positions_preserve_records(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                entries = [(f"share-{i}", f"document {i}".encode()) for i in range(6)]
                oram = PathORAM.create(root / "index", servers.pair, entries, capacity=8)
                self.assertEqual(oram.invariant_report()["blocks"], 6)
                self.assertEqual(dict(oram.records()), dict(entries))
                new_id = oram.append("share-new", b"new session document")
                self.assertEqual(oram.read(new_id), b"new session document")
                before = oram.shape()["revision"]
                self.assertIsNone(oram.read("missing-block"))
                self.assertEqual(oram.shape()["revision"], before + 1)
                self.assertEqual(dict(oram.records()), {**dict(entries), "share-new": b"new session document"})
                self.assertEqual(oram.invariant_report()["blocks"], 7)
            finally:
                servers.close()

    def test_repeated_remapping_reads_and_writes_preserve_tree_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                initial = [(f"ref-{i}", f"payload-{i}".encode()) for i in range(6)]
                oram = PathORAM.create(root / "index", servers.pair, initial, capacity=16)
                identifiers = list(oram._load_state()["positions"])
                expected = dict(initial)
                for step in range(24):
                    identifier = identifiers[step % len(identifiers)]
                    self.assertEqual(oram.read(identifier), expected[oram._load_state()["refs"][identifier]])
                    self.assertTrue(oram.invariant_report()["valid"])
                    if step < 10:
                        ref = f"new-{step}"
                        value = f"payload-new-{step}".encode()
                        identifiers.append(oram.append(ref, value))
                        expected[ref] = value
                        self.assertTrue(oram.invariant_report()["valid"])
                self.assertEqual(dict(oram.records()), expected)
                self.assertTrue(oram.invariant_report()["valid"])
            finally:
                servers.close()

    def test_rejects_out_of_bounds_creation_and_record_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                with self.assertRaises(ValueError):
                    PathORAM.create(root / "too-many", servers.pair, [], capacity=MAX_CAPACITY + 1)
                with self.assertRaises(ValueError):
                    PathORAM.create(root / "oversized", servers.pair, [("large", b"x" * 4097)], capacity=1)
                oram = PathORAM.create(root / "valid", servers.pair, [("valid", b"x")], capacity=2)
                with self.assertRaises(ValueError):
                    oram.append("large", b"x" * 4097)
                self.assertTrue(oram.invariant_report()["valid"])
            finally:
                servers.close()

    def test_snapshot_scans_empty_partial_full_and_non_power_of_two_capacities(self) -> None:
        for capacity, count in ((1, 0), (3, 2), (512, 512), (MAX_CAPACITY, MAX_CAPACITY)):
            with self.subTest(capacity=capacity), tempfile.TemporaryDirectory() as raw:
                memory = MemoryPair()
                entries = [(f"ref-{i}", f"payload-{i}".encode()) for i in range(count)]
                oram = PathORAM.create(Path(raw) / "index", memory.pair, entries, capacity=capacity)
                before_revision = oram.shape()["revision"]
                before_messages = (memory.first.messages, memory.second.messages)
                self.assertEqual(dict(oram.records()), dict(entries))
                self.assertEqual(oram.shape()["revision"], before_revision)
                expected_buckets = (1 << (oram.shape()["height"] + 1)) - 1
                self.assertEqual(
                    (memory.first.messages - before_messages[0], memory.second.messages - before_messages[1]),
                    (expected_buckets, expected_buckets),
                )
                expected_order = [oram._load_state()["objects"][str(node)] for node in range(expected_buckets)]
                self.assertEqual(memory.first.get_ids[-expected_buckets:], expected_order)
                self.assertEqual(memory.second.get_ids[-expected_buckets:], expected_order)

    def test_records_uses_full_scan_trace_without_writes_and_iterator_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                entries = [(f"ref-{i}", f"payload-{i}".encode()) for i in range(6)]
                oram = PathORAM.create(root / "index", servers.pair, entries, capacity=8)
                start = [(item.messages, item.bytes_sent, item.bytes_received) for item in servers.sidecars]
                first = oram.records()
                after_first = [(item.messages, item.bytes_sent, item.bytes_received) for item in servers.sidecars]
                self.assertEqual(oram.read(oram.append("new", b"after snapshot")), b"after snapshot")
                self.assertEqual(dict(first), dict(entries))
                before_second = [(item.messages, item.bytes_sent, item.bytes_received) for item in servers.sidecars]
                oram.records()
                after_second = [(item.messages, item.bytes_sent, item.bytes_received) for item in servers.sidecars]
                first_delta = [(m - m0, s - s0, r - r0) for (m, s, r), (m0, s0, r0) in zip(after_first, start, strict=True)]
                second_delta = [(m - m0, s - s0, r - r0) for (m, s, r), (m0, s0, r0) in zip(after_second, before_second, strict=True)]
                self.assertEqual(first_delta, second_delta)
                self.assertEqual(first_delta, [(15, 0, 15 * BUCKET_BYTES)] * 2)
            finally:
                servers.close()

    def test_append_waits_for_complete_snapshot_scan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                entries = [(f"ref-{i}", f"payload-{i}".encode()) for i in range(4)]
                oram = PathORAM.create(root / "index", servers.pair, entries, capacity=8)
                scan_started = threading.Event()
                release_scan = threading.Event()
                first_get = True

                class BlockingSidecar:
                    def __init__(self, inner):
                        self.inner = inner

                    def get(self, object_id):
                        nonlocal first_get
                        if first_get:
                            first_get = False
                            scan_started.set()
                            if not release_scan.wait(timeout=5):
                                raise AssertionError("snapshot scan was not released")
                        return self.inner.get(object_id)

                    def __getattr__(self, name):
                        return getattr(self.inner, name)

                oram.sidecars = SidecarPair(BlockingSidecar(servers.pair.first), servers.pair.second)
                append_started = threading.Event()

                def append_after_scan_starts():
                    append_started.set()
                    return oram.append("after", b"snapshot")

                with ThreadPoolExecutor(max_workers=2) as pool:
                    scan = pool.submit(lambda: list(oram.records()))
                    self.assertTrue(scan_started.wait(timeout=3))
                    append = pool.submit(append_after_scan_starts)
                    self.assertTrue(append_started.wait(timeout=3))
                    self.assertFalse(append.done())
                    release_scan.set()
                    self.assertEqual(dict(scan.result(timeout=5)), dict(entries))
                    self.assertIsNotNone(append.result(timeout=5))
                self.assertEqual(dict(oram.records()), {**dict(entries), "after": b"snapshot"})
            finally:
                servers.close()

    def test_manifest_rejects_invalid_authenticated_structure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = MemoryPair()
            oram = PathORAM.create(Path(raw) / "index", memory.pair, [("one", b"1"), ("two", b"2")], capacity=4)
            original = oram._load_state()
            mutations = (
                lambda state: state.update(capacity=True),
                lambda state: state.update(height=state["height"] + 1),
                lambda state: state.update(revision=True),
                lambda state: state["positions"].update({"AAAAAAAAAAAAAAAAAAAAAA": 0}),
                lambda state: state["positions"].update({next(iter(state["positions"])): 1 << state["height"]}),
                lambda state: state["refs"].pop(next(iter(state["refs"]))),
                lambda state: state["refs"].update({list(state["refs"])[1]: list(state["refs"].values())[0]}),
                lambda state: state["refs"].update({next(iter(state["refs"])): "\ud800"}),
                lambda state: state["objects"].pop("0"),
                lambda state: state["objects"].update({"1": state["objects"]["0"]}),
            )
            for mutate in mutations:
                state = json.loads(json.dumps(original))
                mutate(state)
                oram._save_state(state)
                with self.assertRaises(ORAMError):
                    oram.records()
            oram._save_state(original)
            self.assertEqual(len(list(oram.records())), 2)

    def test_manifest_size_limit_applies_before_publish_and_before_load(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = MemoryPair()
            oram = PathORAM.create(Path(raw) / "index", memory.pair, [], capacity=1)
            original = oram.manifest_path.read_bytes()
            oversized = {**oram._load_state(), "unused": "x" * MAX_MANIFEST_BYTES}
            with self.assertRaisesRegex(ORAMError, "exceeds"):
                oram._save_state(oversized)
            self.assertEqual(oram.manifest_path.read_bytes(), original)
            oram.manifest_path.write_bytes(bytes(MAX_MANIFEST_BYTES + 1))
            with self.assertRaisesRegex(ORAMError, "length"):
                oram._load_state()

    def test_maximum_bounded_manifest_fits_under_three_mibibytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = MemoryPair()
            oram = PathORAM.create(Path(raw) / "index", memory.pair, [], capacity=1)
            block_ids = [_b64(number.to_bytes(16, "big")) for number in range(1, MAX_CAPACITY + 1)]
            state = {
                "schema": 1,
                "height": 10,
                "capacity": MAX_CAPACITY,
                "revision": 0,
                "positions": dict.fromkeys(block_ids, 1023),
                "objects": {str(node): f"{node:032x}" for node in range((1 << 11) - 1)},
                "stash": {key: _b64(b"x" * BLOCK_BYTES) for key in block_ids[:MAX_STASH]},
                "refs": {key: f"{index:04d}" + "\x01" * 156 for index, key in enumerate(block_ids)},
            }
            oram._save_state(state)
            size = oram.manifest_path.stat().st_size
            self.assertLess(size, 3 * 1024 * 1024)
            self.assertLessEqual(size, MAX_MANIFEST_BYTES)
            self.assertEqual(len(oram._load_state()["positions"]), MAX_CAPACITY)

    def test_invalid_initial_records_are_rejected_before_remote_objects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = MemoryPair()
            directory = Path(raw) / "index"
            for records in (
                [("duplicate", b"one"), ("duplicate", b"two")],
                [("", b"empty reference")],
                [("oversized", b"x" * (BLOCK_BYTES + 1))],
            ):
                with self.subTest(records=len(records)), self.assertRaises(ValueError):
                    PathORAM.create(directory, memory.pair, records, capacity=4)
                self.assertEqual((memory.first.messages, memory.second.messages), (0, 0))
                self.assertFalse(directory.exists())

    def test_snapshot_fails_closed_on_late_corruption_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            memory = MemoryPair()
            oram = PathORAM.create(Path(raw) / "index", memory.pair, [("valid", b"payload")], capacity=4)
            state = oram._load_state()
            last_node = max(map(int, state["objects"]))
            object_id = state["objects"][str(last_node)]
            original = memory.first.objects[object_id]
            memory.first.objects[object_id] = b"truncated"
            with self.assertRaises(ORAMError):
                oram.records()
            memory.first.objects[object_id] = bytes(BUCKET_BYTES + 1)
            with self.assertRaises(ORAMError):
                oram.records()
            corrupted = bytearray(original)
            corrupted[0] ^= 1
            memory.first.objects[object_id] = bytes(corrupted)
            with self.assertRaisesRegex(ORAMError, "authentication"):
                oram.records()
            memory.first.objects[object_id] = original
            self.assertEqual(dict(oram.records()), {"valid": b"payload"})

    def test_snapshot_rejects_missing_unknown_duplicate_wrong_path_and_bad_stash(self) -> None:
        def make_index(raw):
            memory = MemoryPair()
            oram = PathORAM.create(Path(raw) / "index", memory.pair, [("valid", b"payload")], capacity=4)
            return memory, oram

        with tempfile.TemporaryDirectory() as raw:
            memory, oram = make_index(raw)
            state = oram._load_state()
            state["objects"]["0"] = uuid.uuid4().hex
            oram._save_state(state)
            with self.assertRaisesRegex(ORAMError, "missing in-memory bucket"):
                oram.records()

        for kind in ("unknown", "duplicate", "wrong-path", "missing-block"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                memory, oram = make_index(raw)
                state = oram._load_state()
                block_key = next(iter(state["positions"]))
                block_id = _unb64(block_key)
                if kind == "unknown":
                    slots = [oram._encrypt_block(bytes.fromhex("01" * 16), b"unknown")]
                    node = 0
                elif kind == "duplicate":
                    slots = [oram._encrypt_block(block_id, b"payload")] * 2
                    node = 0
                elif kind == "missing-block":
                    slots = []
                    node = (1 << state["height"]) - 1 + state["positions"][block_key]
                else:
                    height = state["height"]
                    wrong_leaf = (state["positions"][block_key] + 1) % (1 << height)
                    slots = [oram._encrypt_block(block_id, b"payload")]
                    node = (1 << height) - 1 + wrong_leaf
                slots.extend(oram._dummy_slot() for _ in range(BUCKET_SLOTS - len(slots)))
                bucket_id = uuid.uuid4().hex
                memory.pair.put(bucket_id, b"".join(slots))
                state["objects"][str(node)] = bucket_id
                oram._save_state(state)
                with self.assertRaises(ORAMError):
                    oram.records()

        for kind, error in (
            ("unknown", "stash"),
            ("noncanonical", "stash"),
            ("oversized", "stash"),
            ("duplicate", "both"),
        ):
            with self.subTest(stash=kind), tempfile.TemporaryDirectory() as raw:
                _, oram = make_index(raw)
                state = oram._load_state()
                block_key = next(iter(state["positions"]))
                stash_key = _b64(bytes.fromhex("02" * 16)) if kind == "unknown" else block_key
                payload = {
                    "noncanonical": "!!!!",
                    "oversized": _b64(b"x" * (BLOCK_BYTES + 1)),
                }.get(kind, _b64(b"orphan"))
                state["stash"] = {stash_key: payload}
                oram._save_state(state)
                with self.assertRaisesRegex(ORAMError, error):
                    oram.records()

    def test_stash_pressure_aborts_without_publishing_partial_position_map(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                oram = PathORAM.create(root / "index", servers.pair, [("base", b"value")], capacity=4)
                old_manifest = oram.manifest_path.read_bytes()
                original_evict = oram._evict_path

                def force_stash_overflow(state, path, tree, stash):
                    for number in range(MAX_STASH + 100):
                        block_id = number.to_bytes(16, "big")
                        key = _b64(block_id)
                        state["positions"][key] = 0
                        stash[key] = b"pressure"
                    return original_evict(state, path, tree, stash)

                oram._evict_path = force_stash_overflow
                with self.assertRaisesRegex(ORAMError, "stash limit"):
                    oram.append("new", b"value")
                self.assertEqual(oram.manifest_path.read_bytes(), old_manifest)
                self.assertTrue(oram.invariant_report()["valid"])
            finally:
                servers.close()

    def test_same_size_logical_reads_have_same_path_message_shape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                oram = PathORAM.create(root / "index", servers.pair, [(str(i), b"x") for i in range(4)], capacity=8)
                before = [(item.messages, item.bytes_sent, item.bytes_received) for item in servers.sidecars]
                ids = list(oram._load_state()["positions"])
                oram.read(ids[0])
                first = [(item.messages, item.bytes_sent, item.bytes_received) for item in servers.sidecars]
                oram.read(ids[-1])
                second = [(item.messages, item.bytes_sent, item.bytes_received) for item in servers.sidecars]
                first_delta = [(m2 - m1, s2 - s1, r2 - r1) for (m1, s1, r1), (m2, s2, r2) in zip(before, first, strict=True)]
                second_delta = [(m2 - m1, s2 - s1, r2 - r1) for (m1, s1, r1), (m2, s2, r2) in zip(first, second, strict=True)]
                self.assertEqual(first_delta, second_delta)
                self.assertEqual(first_delta[0][0], 3 * oram.shape()["path_nodes"])
            finally:
                servers.close()

    def test_partial_sidecar_write_does_not_advance_client_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                oram = PathORAM.create(root / "index", servers.pair, [("base", b"original")], capacity=4)
                old_manifest = oram.manifest_path.read_bytes()

                class FailingSidecar:
                    def __init__(self, inner):
                        self.inner = inner

                    def put(self, *_args):
                        raise ORAMError("injected sidecar failure")

                    def __getattr__(self, name):
                        return getattr(self.inner, name)

                broken = SidecarPair(servers.pair.first, FailingSidecar(servers.pair.second))
                unsafe = PathORAM.open(root / "index", broken)
                with self.assertRaises(ORAMError):
                    unsafe.append("delta", b"uncommitted")
                self.assertEqual(oram.manifest_path.read_bytes(), old_manifest)
                self.assertEqual(dict(oram.records()), {"base": b"original"})
            finally:
                servers.close()

    def test_lost_sidecar_fails_closed_without_advancing_position_map(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            servers = PairServers(root / "sidecars")
            try:
                oram = PathORAM.create(root / "index", servers.pair, [("private", b"plaintext stays client-side")], capacity=4)
                block_id = next(iter(oram._load_state()["positions"]))
                old_manifest = oram.manifest_path.read_bytes()
                servers.servers[1].shutdown()
                servers.servers[1].server_close()
                # Listener shutdown leaves accepted HTTP/1.1 sockets alive.
                for connection in servers.servers[1].connections:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    connection.close()

                with self.assertRaisesRegex(ORAMError, "sidecar request failed"):
                    oram.read(block_id)

                self.assertEqual(oram.manifest_path.read_bytes(), old_manifest)
                self.assertEqual(oram.shape()["revision"], 0)
            finally:
                servers.close()


if __name__ == "__main__":
    unittest.main()
