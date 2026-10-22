from __future__ import annotations

import base64
import json
import struct
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

from semshare.encoder import DIMENSION
from semshare.oram import ORAMError, Sidecar, SidecarPair
from semshare.sidecar import ObjectStore, handler_for
from semshare.workspace import Workspace


def record(file_id: str, share_id: str, vector: list[float]) -> bytes:
    raw = struct.pack(f"<{DIMENSION}f", *vector)
    return json.dumps({"file_id": file_id, "share_id": share_id, "vector_b64": base64.b64encode(raw).decode()}, separators=(",", ":")).encode()


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

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)


class WorkspaceTests(unittest.TestCase):
    def test_session_delta_is_private_until_search_and_merge_is_atomic(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pair = PairServers(root / "stores")
            try:
                workspace = Workspace.create(root / "client", pair.pair, capacity=8)
                vector_a = [1.0] + [0.0] * (DIMENSION - 1)
                vector_b = [0.0, 1.0] + [0.0] * (DIMENSION - 2)
                workspace.add("alice", "file-a:share-a", record("file-a", "share-a", vector_a))
                workspace.add("bob", "file-b:share-b", record("file-b", "share-b", vector_b))
                self.assertEqual([x["file_id"] for x in workspace.search("alice", vector_a)], ["file-a"])
                self.assertEqual([x["file_id"] for x in workspace.search("bob", vector_b)], ["file-b"])

                old = workspace.state_summary("alice")["active_epoch"]
                original_pair = workspace.sidecars

                class FailingSidecar:
                    def __init__(self, inner):
                        self.inner = inner

                    def put(self, *_args):
                        raise ORAMError("injected second-side write failure")

                    def __getattr__(self, name):
                        return getattr(self.inner, name)

                workspace.sidecars = SidecarPair(original_pair.first, FailingSidecar(original_pair.second))
                with self.assertRaises(ORAMError):
                    workspace.merge("alice")
                workspace.sidecars = original_pair
                self.assertEqual(workspace.state_summary("alice")["active_epoch"], old)
                self.assertEqual([x["file_id"] for x in workspace.search("alice", vector_a)], ["file-a"])

                merged = workspace.merge("alice")
                self.assertNotEqual(merged["active_epoch"], old)
                self.assertTrue(merged["previous_epoch_retained"])
                self.assertEqual([x["file_id"] for x in workspace.search("alice", vector_a)], ["file-a"])
                self.assertEqual([x["file_id"] for x in workspace.search("bob", vector_b)], ["file-b"])
            finally:
                pair.close()

    def test_concurrent_session_updates_are_serialized_and_remain_scoped(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pair = PairServers(root / "stores")
            try:
                workspace = Workspace.create(root / "client", pair.pair, capacity=8)
                axis_a = [1.0] + [0.0] * (DIMENSION - 1)
                axis_b = [0.0, 1.0] + [0.0] * (DIMENSION - 2)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    work = [
                        pool.submit(workspace.add, "alice", "a:one", record("a", "one", axis_a)),
                        pool.submit(workspace.add, "bob", "b:two", record("b", "two", axis_b)),
                        pool.submit(workspace.search, "alice", axis_a),
                        pool.submit(workspace.search, "bob", axis_b),
                    ]
                    for future in work:
                        future.result(timeout=30)
                self.assertEqual([x["file_id"] for x in workspace.search("alice", axis_a)], ["a"])
                self.assertEqual([x["file_id"] for x in workspace.search("bob", axis_b)], ["b"])
            finally:
                pair.close()


if __name__ == "__main__":
    unittest.main()
