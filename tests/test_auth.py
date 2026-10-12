import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from meizang.auth import (
    SESSION_COOKIE,
    LoginRateLimiter,
    hash_password,
    new_session_id,
    resolve_auth_context,
    session_lookup_id,
    verify_password,
)
from meizang.database import LibraryDatabase


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify_roundtrip(self):
        stored = hash_password("correct-horse-123")
        self.assertTrue(verify_password("correct-horse-123", stored))
        self.assertTrue(stored.startswith("scrypt$"))

    def test_wrong_password_rejected(self):
        stored = hash_password("correct-horse-123")
        self.assertFalse(verify_password("wrong-password", stored))
        self.assertFalse(verify_password("", stored))

    def test_malformed_hash_rejected(self):
        self.assertFalse(verify_password("x", "not-a-hash"))
        self.assertFalse(verify_password("x", "md5$n=1$r=1$p=1$abcd$efgh"))
        self.assertFalse(verify_password("x", ""))

    def test_salts_are_random(self):
        self.assertNotEqual(hash_password("same"), hash_password("same"))


class RateLimiterTests(unittest.TestCase):
    def test_lockout_after_max_attempts(self):
        limiter = LoginRateLimiter(max_attempts=3, window_seconds=60, lock_seconds=60)
        locked, _ = limiter.is_locked("1.2.3.4")
        self.assertFalse(locked)
        limiter.register_failure("1.2.3.4")
        limiter.register_failure("1.2.3.4")
        locked, _ = limiter.is_locked("1.2.3.4")
        self.assertFalse(locked)
        limiter.register_failure("1.2.3.4")
        locked, retry_after = limiter.is_locked("1.2.3.4")
        self.assertTrue(locked)
        self.assertGreater(retry_after, 0)

    def test_success_resets_counter(self):
        limiter = LoginRateLimiter(max_attempts=2, window_seconds=60, lock_seconds=60)
        limiter.register_failure("1.2.3.4")
        limiter.register_success("1.2.3.4")
        limiter.register_failure("1.2.3.4")
        locked, _ = limiter.is_locked("1.2.3.4")
        self.assertFalse(locked)

    def test_lock_expires(self):
        limiter = LoginRateLimiter(max_attempts=1, window_seconds=60, lock_seconds=0)
        limiter.register_failure("1.2.3.4")
        time.sleep(0.05)
        locked, _ = limiter.is_locked("1.2.3.4")
        self.assertFalse(locked)


class ResolveAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = LibraryDatabase(str(Path(self.temporary.name) / "library.db"))
        self.user = self.database.create_user("admin", hash_password("password123"), is_admin=True)

    def tearDown(self):
        self.temporary.cleanup()

    def _headers(self, **extra):
        return dict(extra)

    def test_bearer_token_accepted(self):
        context = resolve_auth_context(
            self.database, "s3cr3t", False, self._headers(Authorization="Bearer s3cr3t"), {})
        self.assertIsNotNone(context)
        self.assertEqual(context["method"], "token")

    def test_bearer_token_rejected_without_config(self):
        context = resolve_auth_context(
            self.database, "", False, self._headers(Authorization="Bearer s3cr3t"), {})
        self.assertIsNone(context)

    def test_bearer_wrong_token_rejected(self):
        context = resolve_auth_context(
            self.database, "s3cr3t", False, self._headers(Authorization="Bearer wrong"), {})
        self.assertIsNone(context)

    def _login_session_cookie(self):
        raw = new_session_id()
        lookup = session_lookup_id(raw)
        self.database.create_session(lookup, self.user["id"], lookup, time.time() + 3600)
        return raw

    def test_session_cookie_accepted(self):
        raw = self._login_session_cookie()
        context = resolve_auth_context(
            self.database, "", False, self._headers(), {SESSION_COOKIE: raw})
        self.assertIsNotNone(context)
        self.assertEqual(context["method"], "session")
        self.assertEqual(context["username"], "admin")
        self.assertTrue(context["is_admin"])

    def test_expired_session_rejected(self):
        raw = new_session_id()
        lookup = session_lookup_id(raw)
        self.database.create_session(lookup, self.user["id"], lookup, time.time() - 10)
        context = resolve_auth_context(
            self.database, "", False, self._headers(), {SESSION_COOKIE: raw})
        self.assertIsNone(context)

    def test_unknown_session_rejected(self):
        context = resolve_auth_context(
            self.database, "", False, self._headers(), {SESSION_COOKIE: "nope"})
        self.assertIsNone(context)

    def test_gateway_nas_identity_trusted_on_socket(self):
        context = resolve_auth_context(
            self.database, "", True,
            self._headers(**{"X-Trim-Username": "nasuser", "X-Trim-Userid": "1001",
                             "X-Trim-Isadmin": "0"}), {})
        self.assertIsNotNone(context)
        self.assertEqual(context["method"], "nas")
        self.assertEqual(context["username"], "nasuser")
        self.assertFalse(context["is_admin"])

    def test_gateway_nas_admin_flag_parsing(self):
        for value, expected in (("1", True), ("true", True), ("yes", True), ("0", False), ("", False)):
            context = resolve_auth_context(
                self.database, "", True,
                self._headers(**{"X-Trim-Username": "u", "X-Trim-Isadmin": value}), {})
            self.assertEqual(context["is_admin"], expected, value)

    def test_gateway_headers_ignored_on_tcp(self):
        """直连 TCP 时必须忽略网关身份头，防止客户端伪造。"""
        context = resolve_auth_context(
            self.database, "", False,
            self._headers(**{"X-Trim-Username": "attacker", "X-Trim-Isadmin": "1"}), {})
        self.assertIsNone(context)

    def test_priority_bearer_over_session_over_gateway(self):
        raw = self._login_session_cookie()
        gateway = {"X-Trim-Username": "nasuser", "X-Trim-Isadmin": "1"}
        context = resolve_auth_context(
            self.database, "s3cr3t", True,
            self._headers(Authorization="Bearer s3cr3t", **gateway),
            {SESSION_COOKIE: raw})
        self.assertEqual(context["method"], "token")
        context = resolve_auth_context(
            self.database, "", True, self._headers(**gateway), {SESSION_COOKIE: raw})
        self.assertEqual(context["method"], "session")


if __name__ == "__main__":
    unittest.main()
