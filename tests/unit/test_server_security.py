"""Server security middleware."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class ServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)

    def test_security_headers_are_set(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("object-src 'none'", response.headers["content-security-policy"])

    def test_untrusted_host_is_rejected(self):
        response = self.client.get("/", headers={"host": "evil.example"})

        self.assertEqual(response.status_code, 400)

    def test_large_request_body_is_rejected(self):
        old_limit = server.MAX_REQUEST_BYTES
        server.MAX_REQUEST_BYTES = 4
        try:
            response = self.client.post(
                "/api/login",
                content=b"12345",
                headers={"content-type": "application/json"},
            )
        finally:
            server.MAX_REQUEST_BYTES = old_limit

        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
