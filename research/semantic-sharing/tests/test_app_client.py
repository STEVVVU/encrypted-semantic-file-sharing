from __future__ import annotations

import base64
import hashlib
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from semshare.app_client import AppError, SecuShareClient


class SecuShareClientTests(unittest.TestCase):
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
