from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from semshare.oram import BUCKET_BYTES, ORAMError, Sidecar
from semshare.sidecar import MAX_OBJECT_BYTES, ObjectStore, handler_for


@contextmanager
def server_for(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TransportTests(unittest.TestCase):
    def test_reuses_connection_and_preserves_exact_methods_and_counters(self):
        with tempfile.TemporaryDirectory() as root:
            requests = []
            base = handler_for(ObjectStore(Path(root)))

            class Handler(base):
                def log_message(self, *_args):
                    requests.append((self.command, self.client_address))

            with server_for(Handler) as url, Sidecar(url) as side:
                object_id = "a" * 32
                payload = b"x" * BUCKET_BYTES
                side.put(object_id, payload)
                self.assertEqual(side.get(object_id), payload)
                side.delete(object_id)
                side.health()
                self.assertEqual([method for method, _ in requests], ["PUT", "GET", "DELETE", "GET"])
                self.assertEqual(len({peer for _, peer in requests}), 1)
                self.assertEqual((side.messages, side.bytes_sent, side.bytes_received), (4, BUCKET_BYTES, BUCKET_BYTES + 2))
                with self.assertRaises(ORAMError):
                    side.get(object_id)
                side.health()
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(lambda _: side.health(), range(32)))
                self.assertEqual((side.messages, side.bytes_sent, side.bytes_received), (37, BUCKET_BYTES, BUCKET_BYTES + 68))
            self.assertTrue(side._client.is_closed)

    def test_bad_status_redirect_oversize_truncation_and_stale_connection(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            mode = "healthy"
            calls = 0

            def log_message(self, *_args):
                pass

            def do_GET(self):
                type(self).calls += 1
                status = 302 if self.mode == "redirect" else 500 if self.mode == "error" else 200
                body = b"x" * (BUCKET_BYTES + 100) if self.mode == "oversize" else b"ok"
                self.send_response(status)
                self.send_header("Content-Length", str(100 if self.mode == "truncated" else len(body)))
                if self.mode == "redirect":
                    self.send_header("Location", "http://192.0.2.1:80/private")
                if self.mode == "encoded":
                    self.send_header("Content-Encoding", "gzip")
                if self.mode != "healthy":
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

        with server_for(Handler) as url, Sidecar(url) as side:
            for mode in ("redirect", "error", "oversize", "truncated", "encoded"):
                Handler.mode = mode
                before = Handler.calls
                with self.subTest(mode=mode), self.assertRaises(ORAMError):
                    side.health()
                self.assertEqual(Handler.calls, before + 1)
                Handler.mode = "healthy"
                side.health()
            Handler.mode = "stale"
            side.health()
            Handler.mode = "healthy"
            side.health()


class ObjectStoreTests(unittest.TestCase):
    def test_immutable_bounded_objects_and_missing_reads(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = ObjectStore(Path(root))
            store.put("a" * 32, b"share")
            self.assertEqual(store.get("a" * 32), b"share")
            with self.assertRaises(FileExistsError):
                store.put("a" * 32, b"replacement")
            with self.assertRaises(FileNotFoundError):
                store.get("b" * 32)
            with self.assertRaises(ValueError):
                store.put("../outside", b"share")
            with self.assertRaises(ValueError):
                store.put("c" * 32, b"x" * (MAX_OBJECT_BYTES + 1))
            store.delete("a" * 32)
            self.assertFalse((Path(root) / f"{'a' * 32}.share").exists())


if __name__ == "__main__":
    unittest.main()
