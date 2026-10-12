import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from meizang.server import api_authorized


class ApiAuthTests(unittest.TestCase):
    def test_empty_token_disables_auth(self):
        self.assertTrue(api_authorized("", "/api/stats", ""))
        self.assertTrue(api_authorized("", "/api/duplicates/delete", ""))

    def test_health_endpoint_is_exempt(self):
        self.assertTrue(api_authorized("secret", "/api/health", ""))

    def test_static_pages_are_exempt(self):
        self.assertTrue(api_authorized("secret", "/", ""))
        self.assertTrue(api_authorized("secret", "/index.html", ""))

    def test_valid_bearer_token(self):
        self.assertTrue(api_authorized("secret", "/api/stats", "Bearer secret"))
        self.assertTrue(api_authorized("secret", "/api/stats", "bearer secret"))

    def test_wrong_or_missing_token_rejected(self):
        self.assertFalse(api_authorized("secret", "/api/stats", ""))
        self.assertFalse(api_authorized("secret", "/api/stats", "Bearer wrong"))
        self.assertFalse(api_authorized("secret", "/api/stats", "Basic c2VjcmV0"))
        self.assertFalse(api_authorized("secret", "/api/stats", "secret"))


if __name__ == "__main__":
    unittest.main()
