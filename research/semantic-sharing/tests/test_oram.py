from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from semshare.oram import MAX_STASH, ORAMError, PathORAM, Sidecar, SidecarPair, _b64
from semshare.sidecar import ObjectStore, handler_for


class PairServers:
    def __init__(self, root: Path):
        self.servers = []
        self.threads = []
        self.sidecars = []
        for name in ("a", "b"):
            base = handler_for(ObjectStore(root / name))
            handler = type("QuietHandler", (base,), {"log_message": lambda *_args: None})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append(server)
            self.threads.append(thread)
            self.sidecars.append(Sidecar(f"http://127.0.0.1:{server.server_port}"))
        self.pair = SidecarPair(*self.sidecars)

    def close(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)


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
                    PathORAM.create(root / "too-many", servers.pair, [], capacity=65)
                with self.assertRaises(ValueError):
                    PathORAM.create(root / "oversized", servers.pair, [("large", b"x" * 4097)], capacity=1)
                oram = PathORAM.create(root / "valid", servers.pair, [("valid", b"x")], capacity=2)
                with self.assertRaises(ValueError):
                    oram.append("large", b"x" * 4097)
                self.assertTrue(oram.invariant_report()["valid"])
            finally:
                servers.close()

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

                with self.assertRaisesRegex(ORAMError, "sidecar request failed"):
                    oram.read(block_id)

                self.assertEqual(oram.manifest_path.read_bytes(), old_manifest)
                self.assertEqual(oram.shape()["revision"], 0)
            finally:
                servers.close()


if __name__ == "__main__":
    unittest.main()
