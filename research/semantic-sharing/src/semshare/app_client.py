"""Narrow client for SecuShare's existing authenticated upload/share API."""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import re
import secrets
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_UPLOAD_BYTES = 1_000_000
SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")


class AppError(RuntimeError):
    pass


class SecuShareClient:
    def __init__(self, api_base: str, timeout: float = 5.0):
        parsed = urlsplit(api_base)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/v1"
        ):
            raise ValueError("research workflow requires a local loopback SecuShare API")
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.token: str | None = None
        self.csrf: str | None = None

    def request(self, path: str, method: str = "GET", body: bytes | None = None, content_type: str = "application/json"):
        if not path.startswith("/") or ".." in path.split("/"):
            raise ValueError("invalid API path")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if method not in ("GET", "HEAD", "OPTIONS") and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(self.api_base + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers.items()), response.read(MAX_UPLOAD_BYTES + 1024)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read(16 * 1024)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AppError(f"SecuShare request failed: {method} {path}") from exc

    def create_guest(self) -> dict:
        status, _headers, body = self.request("/auth/guest", "POST", b"{}")
        data = self._json_success(status, body)
        self.token = data.get("token")
        self.csrf = data.get("csrf_token")
        if not isinstance(self.token, str) or not self.token or not isinstance(self.csrf, str) or not self.csrf:
            raise AppError("SecuShare guest session omitted authentication or CSRF data")
        return data.get("user", {})

    def upload_encrypted(self, filename: str, plaintext: bytes) -> tuple[dict, dict]:
        if not SAFE_FILENAME.fullmatch(filename) or not plaintext or len(plaintext) > MAX_UPLOAD_BYTES - 32:
            raise ValueError("filename or plaintext exceeds the controlled upload limits")
        key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
        boundary = "semshare-" + secrets.token_hex(16)
        values = {
            "original_filename": filename,
            "mime_type": "text/plain",
            "file_size_bytes": str(len(plaintext)),
            "encrypted_size_bytes": str(len(ciphertext)),
            "iv_base64": base64.b64encode(nonce).decode("ascii"),
            "checksum_sha256": hashlib.sha256(ciphertext).hexdigest(),
        }
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"encrypted.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode(),
            ciphertext,
            b"\r\n",
        ]
        for name, value in values.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append(f"--{boundary}--\r\n".encode())
        status, _headers, body = self.request("/files/", "POST", b"".join(parts), f"multipart/form-data; boundary={boundary}")
        metadata = self._json_success(status, body)
        file_data = metadata.get("data") if isinstance(metadata.get("data"), dict) else metadata
        file_id = file_data.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise AppError("SecuShare upload returned no file ID")
        return file_data, {"key": key, "nonce": nonce, "ciphertext_sha256": values["checksum_sha256"], "plaintext_sha256": hashlib.sha256(plaintext).hexdigest()}

    def create_share(self, file_id: str) -> dict:
        body = json.dumps({"file_id": file_id}, separators=(",", ":")).encode()
        status, _headers, response = self.request("/shares/", "POST", body)
        return self._json_success(status, response)

    def download_share(self, share_id: str, secret: dict) -> tuple[bytes, bytes, bytes]:
        self._validate_id(share_id)
        body = json.dumps({}, separators=(",", ":")).encode()
        status, headers, ciphertext = self.request(f"/shares/{share_id}/file", "POST", body)
        if status != 200:
            raise AppError(f"SecuShare shared-file download failed with HTTP {status}")
        normalized = {key.lower(): value for key, value in headers.items()}
        checksum = normalized.get("x-checksum-sha256")
        if not isinstance(checksum, str) or hashlib.sha256(ciphertext).hexdigest() != checksum:
            raise AppError("shared ciphertext failed the SecuShare checksum check")
        nonce_text = normalized.get("x-iv-base64")
        if not nonce_text:
            raise AppError("shared-file response omitted its AES-GCM nonce")
        try:
            nonce = base64.b64decode(nonce_text, validate=True)
        except ValueError as exc:
            raise AppError("shared-file response contained an invalid AES-GCM nonce") from exc
        if len(nonce) != 12:
            raise AppError("shared-file response AES-GCM nonce has an invalid length")
        expected_nonce = secret.get("nonce")
        if expected_nonce is not None and nonce != expected_nonce:
            raise AppError("shared-file nonce differs from the locally recorded upload nonce")
        try:
            plaintext = AESGCM(secret["key"]).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise AppError("shared-file AES-GCM authentication failed") from exc
        return plaintext, ciphertext, nonce

    @staticmethod
    def parse_share_link(url: str, expected_web_base: str) -> tuple[str, bytes]:
        """Read the fragment key locally; fragments are not part of an HTTP request."""
        link = urlsplit(url)
        expected = urlsplit(expected_web_base)
        if (
            expected.scheme != "http"
            or expected.hostname != "127.0.0.1"
            or expected.port is None
            or expected.path not in ("", "/")
            or link.scheme != expected.scheme
            or link.netloc != expected.netloc
            or link.query
            or link.username is not None
            or link.password is not None
        ):
            raise ValueError("share link must use the expected local SecuShare origin without a query")
        match = re.fullmatch(r"/s/([A-Za-z0-9_-]{1,64})", link.path)
        if not match:
            raise ValueError("share link has an invalid SecuShare path")
        try:
            key = base64.b64decode(link.fragment, validate=True)
        except ValueError as exc:
            raise ValueError("share link fragment is not valid base64") from exc
        if len(key) != 32:
            raise ValueError("share link fragment must contain a 32-byte AES key")
        return match.group(1), key

    @staticmethod
    def verify_tamper_rejection(ciphertext: bytes, secret: dict) -> dict[str, bool]:
        """Check that this actual downloaded ciphertext rejects byte and nonce changes."""
        if len(ciphertext) < 16 or len(secret.get("key", b"")) != 32 or len(secret.get("nonce", b"")) != 12:
            raise ValueError("ciphertext or AES-GCM key/nonce has an invalid length")
        changed_ciphertext = bytearray(ciphertext)
        changed_ciphertext[-1] ^= 1
        changed_nonce = bytearray(secret["nonce"])
        changed_nonce[0] ^= 1
        for changed_data, nonce in ((bytes(changed_ciphertext), secret["nonce"]), (ciphertext, bytes(changed_nonce))):
            try:
                AESGCM(secret["key"]).decrypt(nonce, changed_data, None)
            except InvalidTag:
                continue
            raise AppError("AES-GCM accepted a mutated ciphertext or nonce")
        return {"ciphertext_mutation_rejected": True, "nonce_mutation_rejected": True}

    def fetch_owned_ciphertext(self, file_id: str) -> tuple[int, bytes]:
        self._validate_id(file_id)
        status, _headers, body = self.request(f"/files/{file_id}")
        return status, body

    def fetch_share_status(self, share_id: str) -> tuple[int, bytes]:
        self._validate_id(share_id)
        status, _headers, body = self.request(f"/shares/{share_id}/file", "POST", b"{}")
        return status, body

    def deactivate_share(self, share_id: str) -> None:
        self._validate_id(share_id)
        status, _headers, body = self.request(f"/shares/{share_id}", "DELETE")
        if status < 200 or status >= 300:
            raise AppError(f"SecuShare share revocation failed with HTTP {status}: {body[:256]!r}")

    def _json_success(self, status: int, body: bytes) -> dict:
        if status < 200 or status >= 300:
            raise AppError(f"SecuShare API returned HTTP {status}: {body[:256]!r}")
        try:
            response = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError("SecuShare API returned malformed JSON") from exc
        if not isinstance(response, dict) or response.get("success") is not True:
            raise AppError("SecuShare API response did not indicate success")
        data = response.get("data")
        if not isinstance(data, dict):
            raise AppError("SecuShare API response is missing its data object")
        return data

    @staticmethod
    def _validate_id(value: str) -> None:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("application ID has an invalid format")
