"""登录鉴权：本地用户密码哈希、会话签发、登录限流与统一鉴权解析。"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple


SESSION_COOKIE = "meizang_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
SESSION_TOUCH_INTERVAL_SECONDS = 3600

# 网关注入的 NAS 身份头（仅当请求经由飞牛统一网关 Unix Socket 进入时可信，
# 网关已在转发前校验过 NAS 会话；直连 TCP 端口时必须忽略，防止头部伪造）。
GATEWAY_USER_ID_HEADER = "X-Trim-Userid"
GATEWAY_USERNAME_HEADER = "X-Trim-Username"
GATEWAY_ISADMIN_HEADER = "X-Trim-Isadmin"


# scrypt(n=2^15, r=8) 约需 33MB 内存，超过 OpenSSL 默认 32MB 上限，显式放宽。
_SCRYPT_MAXMEM = 128 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, dklen=32,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "scrypt$n=32768,r=8,p=1${}${}".format(salt.hex(), digest.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, params, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        options = dict(part.split("=") for part in params.split(","))
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(options["n"]),
            r=int(options["r"]),
            p=int(options["p"]),
            dklen=len(bytes.fromhex(digest_hex)),
            maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, KeyError, AttributeError):
        return False


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def session_lookup_id(raw_token: str) -> str:
    """会话 Cookie 明文仅存于客户端；服务端只保存其 SHA-256 哈希。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def check_bearer_token(api_token: str, authorization: str) -> bool:
    if not api_token:
        return False
    scheme, _, credential = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(credential, api_token)


def api_authorized(api_token: str, path: str, authorization: str) -> bool:
    """兼容旧逻辑：纯 Token 模式下的 API 鉴权判定（保留给测试与文档）。"""
    if not path.startswith("/api/") or path == "/api/health":
        return True
    return check_bearer_token(api_token, authorization)


class LoginRateLimiter:
    """登录失败限流：同一 key 短时间内失败过多则锁定一段时间。"""

    def __init__(self, max_attempts: int = 8, window_seconds: int = 300, lock_seconds: int = 600):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self._lock = threading.Lock()
        self._records: Dict[str, Dict[str, float]] = {}

    def _prune(self, now: float) -> None:
        expired = [
            key for key, record in self._records.items()
            if now - record["first"] > self.window_seconds and now > record["locked_until"]
        ]
        for key in expired:
            del self._records[key]

    def is_locked(self, key: str) -> Tuple[bool, int]:
        now = time.time()
        with self._lock:
            self._prune(now)
            record = self._records.get(key)
            if record and now < record["locked_until"]:
                return True, int(record["locked_until"] - now)
            return False, 0

    def register_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(now)
            record = self._records.setdefault(key, {"count": 0, "first": now, "locked_until": 0.0})
            if now - record["first"] > self.window_seconds:
                record.update(count=0, first=now, locked_until=0.0)
            record["count"] += 1
            if record["count"] >= self.max_attempts:
                record["locked_until"] = now + self.lock_seconds

    def register_success(self, key: str) -> None:
        with self._lock:
            self._records.pop(key, None)


def _gateway_nas_identity(headers, trust_gateway_headers: bool) -> Optional[Dict]:
    """从网关注入头解析 NAS 用户身份；非网关模式一律返回 None。"""
    if not trust_gateway_headers:
        return None
    username = (headers.get(GATEWAY_USERNAME_HEADER) or "").strip()
    if not username:
        return None
    is_admin = (headers.get(GATEWAY_ISADMIN_HEADER) or "").strip().lower() in ("1", "true", "yes")
    return {
        "method": "nas",
        "username": username,
        "is_admin": is_admin,
        "nas_userid": (headers.get(GATEWAY_USER_ID_HEADER) or "").strip(),
        "user_id": None,
    }


def resolve_auth_context(database, api_token: str, trust_gateway_headers: bool,
                         headers, cookies: Dict[str, str]) -> Optional[Dict]:
    """按优先级解析请求身份：Bearer Token > 会话 Cookie > 网关 NAS 身份头。

    网关 NAS 身份头仅在 trust_gateway_headers 为 True（Unix Socket 网关模式）
    时采信；直连 TCP 时忽略，防止客户端伪造。
    """
    now = time.time()
    if check_bearer_token(api_token, headers.get("Authorization", "")):
        return {"method": "token", "username": "api-token", "is_admin": True, "user_id": None}
    session_raw = (cookies or {}).get(SESSION_COOKIE, "")
    if session_raw:
        session = database.get_session(session_lookup_id(session_raw), now)
        if session:
            return {
                "method": "session",
                "username": session["username"],
                "is_admin": session["is_admin"],
                "user_id": session["user_id"],
                "session_id": session_raw,
            }
    return _gateway_nas_identity(headers, trust_gateway_headers)
