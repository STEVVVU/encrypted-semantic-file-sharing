from __future__ import annotations

import base64
import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from semshare.app_client import AppError, SecuShareClient


class SecuShareClientTests(unittest.TestCase):
    def test_guest_csrf_token_is_sent_as_cookie_and_header_on_unsafe_requests(self):
        csrf = "a" * 64
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append((self.path, self.headers.get("Cookie"), self.headers.get("X-CSRF-Token")))
                if self.path == "/api/v1/auth/guest":
                    body = json.dumps({"success": True, "data": {"token": "guest-token", "csrf_token": csrf}}).encode()
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"csrf_token={csrf}; Path=/; Secure; SameSite=Strict")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.headers.get("Cookie") != f"csrf_token={csrf}" or self.headers.get("X-CSRF-Token") != csrf:
                    self.send_error(403, "invalid CSRF token")
                    return
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = SecuShareClient(f"http://127.0.0.1:{server.server_port}/api/v1")
            client.create_guest()
            status, _headers, _body = client.request("/shares/", "POST", b"{}")
            self.assertEqual(status, 200)
            self.assertEqual(observed[1][1:], (f"csrf_token={csrf}", csrf))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_guest_rejects_csrf_values_that_cannot_be_safely_sent_as_a_cookie(self):
        client = SecuShareClient("http://127.0.0.1:8080/api/v1")
        client.request = lambda *_args, **_kwargs: (
            200,
            {},
            json.dumps({"success": True, "data": {"token": "guest-token", "csrf_token": "bad\r\nInjected: yes"}}).encode(),
        )

        with self.assertRaisesRegex(AppError, "CSRF data"):
            client.create_guest()

    def test_request_revalidates_loopback_api_base_before_sending_credentials(self):
        client = SecuShareClient("http://127.0.0.1:8080/api/v1")
        client.token = "guest-token"
        client.csrf = "a" * 64
        client.api_base = "http://example.com/api/v1"

        with self.assertRaisesRegex(ValueError, "loopback"):
            client.request("/shares/", "POST", b"{}")

        client.api_base = "http://127.0.0.1:8081/api/v1"
        with self.assertRaisesRegex(ValueError, "cannot change"):
            client.request("/shares/", "POST", b"{}")

    def test_csrf_cookie_is_not_forwarded_to_another_origin(self):
        received = []

        class CaptureHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.headers.get("Cookie"))
                self.send_response(200)
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *_args):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
                self.end_headers()

            def log_message(self, *_args):
                pass

        source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (target, source)]
        for thread in threads:
            thread.start()
        try:
            client = SecuShareClient(f"http://127.0.0.1:{source.server_port}/api/v1")
            client.token, client.csrf = "guest-token", "b" * 64
            status, _headers, _body = client.request("/shares/", "POST", b"{}")
            self.assertEqual(status, 302)
            self.assertEqual(received, [])
        finally:
            for server in (source, target):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_download_rejects_nonce_that_differs_from_the_uploaded_file(self):
        key, original_nonce, changed_nonce = b"k" * 32, b"n" * 12, b"m" * 12
        ciphertext = AESGCM(key).encrypt(original_nonce, b"synthetic source", None)
        client = SecuShareClient("http://127.0.0.1:8080/api/v1")
        client.request = lambda *_args, **_kwargs: (
            200,
            {
                "X-Checksum-Sha256": hashlib.sha256(ciphertext).hexdigest(),
                "X-IV-Base64": base64.b64encode(changed_nonce).decode("ascii"),
            },
            ciphertext,
        )

        with self.assertRaisesRegex(AppError, "nonce differs"):
            client.download_share("share-1", {"key": key, "nonce": original_nonce})

    def test_downloaded_aes_gcm_bytes_reject_ciphertext_and_nonce_tampering(self):
        secret = {"key": b"k" * 32, "nonce": b"n" * 12}
        ciphertext = AESGCM(secret["key"]).encrypt(secret["nonce"], b"synthetic source", None)

        result = SecuShareClient.verify_tamper_rejection(ciphertext, secret)

        self.assertEqual(result, {"ciphertext_mutation_rejected": True, "nonce_mutation_rejected": True})


if __name__ == "__main__":
    unittest.main()
