"""Minimal loopback object service for one randomized share of each ORAM bucket."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_OBJECT_BYTES = 20 * 1024
OBJECT_ID = re.compile(r"[0-9a-f]{32}\Z")


class ObjectStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, object_id: str) -> Path:
        if not OBJECT_ID.fullmatch(object_id):
            raise ValueError("invalid object id")
        return self.root / f"{object_id}.share"

    def put(self, object_id: str, data: bytes) -> None:
        if not data or len(data) > MAX_OBJECT_BYTES:
            raise ValueError("object length outside configured bounds")
        target = self.path(object_id)
        if target.exists():
            raise FileExistsError(object_id)
        fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temp_name, target)
            except FileExistsError:
                raise
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def get(self, object_id: str) -> bytes:
        return self.path(object_id).read_bytes()

    def delete(self, object_id: str) -> None:
        self.path(object_id).unlink(missing_ok=True)


def handler_for(store: ObjectStore, instance_id: str = ""):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SecuShareShareStore/1"
        sys_version = ""

        def log_message(self, _format: str, *args: object) -> None:
            # Request paths contain only random object identifiers, never document data.
            print(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

        def do_GET(self) -> None:
            if self.path == "/health":
                body = b"ok" if not instance_id else f"ok:{instance_id}".encode("ascii")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            object_id = self.path.removeprefix("/objects/")
            try:
                data = store.get(object_id)
            except (ValueError, FileNotFoundError):
                self.send_error(404)
                return
            if len(data) > MAX_OBJECT_BYTES:
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self) -> None:
            object_id = self.path.removeprefix("/objects/")
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal() or int(length) < 1 or int(length) > MAX_OBJECT_BYTES:
                self.send_error(413)
                return
            try:
                data = self.rfile.read(int(length))
                if len(data) != int(length):
                    self.send_error(400)
                    return
                store.put(object_id, data)
            except FileExistsError:
                self.send_error(409)
                return
            except ValueError:
                self.send_error(400)
                return
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self) -> None:
            object_id = self.path.removeprefix("/objects/")
            try:
                store.delete(object_id)
            except ValueError:
                self.send_error(400)
                return
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_error(self, format: str, *args: object) -> None:
            print(f"error: {format % args}", flush=True)

    return Handler


def serve(host: str, port: int, data_dir: Path, instance_id: str = "") -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("sidecar bind address must be a numeric loopback IP") from exc
    if not address.is_loopback:
        raise ValueError("sidecar bind address must be loopback")
    if not 1 <= port <= 65535:
        raise ValueError("port outside valid range")
    server = ThreadingHTTPServer((str(address), port), handler_for(ObjectStore(data_dir), instance_id))
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--instance-id", default="")
    args = parser.parse_args()
    serve(args.host, args.port, args.data_dir, args.instance_id)


if __name__ == "__main__":
    main()
