import hmac
import json
import mimetypes
import os
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import traceback
import uuid
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from .auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    SESSION_TOUCH_INTERVAL_SECONDS,
    LoginRateLimiter,
    api_authorized,
    check_bearer_token,
    hash_password,
    new_session_id,
    resolve_auth_context,
    session_lookup_id,
    verify_password,
)
from .database import LibraryDatabase
from .duplicate_cleanup import delete_duplicate_file, delete_duplicates
from .fnos_api import shared_accessible_folders
from .mdc_bridge import MDCManager
from .music import MusicJobs
from .organizer import QBIntegration
from .providers.local import media_tool_env
from .providers.tmdb import TMDBProvider
from .qbittorrent import validate_qb_url
from .scanner import scan_root


def api_authorized(api_token: str, path: str, authorization: str) -> bool:
    """API 访问鉴权。

    api_token 为空表示不启用认证（本地开发默认行为，保持向后兼容）。
    启用后所有 /api/* 接口都需要 Authorization: Bearer <token>，
    /api/health 豁免以便容器健康检查，静态页面不涉及敏感数据保持开放。
    """
    if not path.startswith("/api/") or path == "/api/health":
        return True
    if not api_token:
        return True
    scheme, _, credential = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(credential, api_token)


def validate_proxy_url(proxy_url: str) -> str:
    value = proxy_url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("代理地址必须是有效的 HTTP 或 HTTPS URL")
    try:
        parsed.port
    except ValueError:
        raise ValueError("代理端口无效")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("代理地址不能包含路径、查询参数或片段")
    return value


def public_provider_settings(settings: Dict[str, str]) -> Dict[str, Any]:
    return {
        "tmdb": {
            "enabled": settings["tmdb_enabled"] == "true",
            "configured": bool(settings["tmdb_token"]),
            "language": settings["tmdb_language"],
        },
        "proxy": {
            "enabled": settings["proxy_enabled"] == "true",
            "configured": bool(settings["proxy_url"]),
        },
    }


def public_qb_settings(settings: Dict[str, str], stats: Dict[str, int]) -> Dict[str, Any]:
    return {
        "enabled": settings["qb_enabled"] == "true",
        "url": settings["qb_url"],
        "username": settings["qb_username"],
        "password_configured": bool(settings["qb_password"]),
        "category": settings["qb_category"],
        "library_root": settings["qb_library_root"],
        "remote_prefix": settings["qb_remote_prefix"],
        "local_prefix": settings["qb_local_prefix"],
        "auto_cleanup": settings["qb_auto_cleanup"] == "true",
        "poll_seconds": int(settings["qb_poll_seconds"]),
        "last_sync_at": settings["qb_last_sync_at"],
        "last_sync_status": settings["qb_last_sync_status"],
        "last_sync_message": settings["qb_last_sync_message"],
        "links": stats,
    }


def public_music_settings(settings: Dict[str, str]) -> Dict[str, Any]:
    return {
        "enabled": settings["musicbrainz_enabled"] == "true",
        "library_root": settings["music_library_root"],
        "write_tags": settings["music_write_tags"] == "true",
        "download_cover": settings["music_download_cover"] == "true",
        "move_files": settings["music_move_files"] == "true",
    }


def effective_qb_settings(current: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, str]:
    values = dict(current)
    mapping = {
        "url": "qb_url",
        "username": "qb_username",
        "category": "qb_category",
        "library_root": "qb_library_root",
        "remote_prefix": "qb_remote_prefix",
        "local_prefix": "qb_local_prefix",
    }
    for incoming, stored in mapping.items():
        if incoming in payload:
            values[stored] = str(payload.get(incoming, "")).strip()
    if str(payload.get("password", "")):
        values["qb_password"] = str(payload["password"])
    if payload.get("clear_password"):
        values["qb_password"] = ""
    if "enabled" in payload:
        values["qb_enabled"] = "true" if payload.get("enabled") else "false"
    if "auto_cleanup" in payload:
        values["qb_auto_cleanup"] = "true" if payload.get("auto_cleanup") else "false"
    if "poll_seconds" in payload:
        values["qb_poll_seconds"] = str(payload["poll_seconds"])
    return values


def validate_qb_connection_settings(settings: Dict[str, str]) -> None:
    validate_qb_url(settings["qb_url"])


def validate_qb_settings(application, settings: Dict[str, str]) -> Dict[str, str]:
    validate_qb_connection_settings(settings)
    category = settings["qb_category"].strip()
    if not category or len(category) > 80:
        raise ValueError("请填写长度不超过 80 个字符的 qB 分类")
    try:
        interval = int(settings["qb_poll_seconds"])
    except (TypeError, ValueError):
        raise ValueError("同步间隔必须是整数")
    if interval < 30 or interval > 3600:
        raise ValueError("同步间隔必须在 30 到 3600 秒之间")

    library_root = settings["qb_library_root"].strip()
    if settings["qb_enabled"] == "true" and not library_root:
        raise ValueError("启用 qB 集成前请选择真实媒体库目录")
    if library_root:
        library_root = str(application.normalize_writable_directory(library_root))

    remote_prefix = settings["qb_remote_prefix"].strip()
    local_prefix = settings["qb_local_prefix"].strip()
    if bool(remote_prefix) != bool(local_prefix):
        raise ValueError("qB 路径映射的远端和本地前缀必须同时填写")
    if remote_prefix and not PurePosixPath(remote_prefix).is_absolute():
        raise ValueError("qB 远端路径前缀必须是绝对路径")
    if local_prefix:
        local_prefix = str(application.normalize_writable_directory(local_prefix))
        if library_root and Path(local_prefix) not in Path(library_root).parents:
            raise ValueError('路径映射时媒体库也须位于同一本地挂载前缀内，确保 qB 可以读取软链接')
    current = application.database.settings()
    if application.database.managed_links():
        for key, value in [('qb_url', settings['qb_url']), ('qb_username', settings['qb_username']), ('qb_library_root', library_root), ('qb_remote_prefix', remote_prefix), ('qb_local_prefix', local_prefix)]:
            if current[key] != value:
                raise ValueError('已有整理台账，请保持 qB 实例、账号及目录映射不变')

    return {
        "qb_enabled": settings["qb_enabled"],
        "qb_url": validate_qb_url(settings["qb_url"]),
        "qb_username": settings["qb_username"],
        "qb_category": category,
        "qb_library_root": library_root,
        "qb_remote_prefix": remote_prefix,
        "qb_local_prefix": local_prefix,
        "qb_auto_cleanup": settings["qb_auto_cleanup"],
        "qb_poll_seconds": str(interval),
    }


class ScanJobs:
    def __init__(self, database: LibraryDatabase):
        self.database = database
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.root_jobs: Dict[int, str] = {}
        self.lock = threading.Lock()

    def start(self, root_id: int, force_metadata: bool = False) -> Dict[str, Any]:
        with self.lock:
            existing_id = self.root_jobs.get(root_id)
            if existing_id and self.jobs.get(existing_id, {}).get("status") == "running":
                return self.jobs[existing_id]
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "root_id": root_id, "status": "running", "force_metadata": force_metadata, "result": None, "error": ""}
            self.jobs[job_id] = job
            self.root_jobs[root_id] = job_id
        threading.Thread(target=self._run, args=(job_id,), daemon=True).start()
        return job

    def _run(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
        if not job:
            return
        try:
            result = scan_root(self.database, job["root_id"], job.get("force_metadata", False))
            with self.lock:
                job.update(status="completed", result=result)
        except Exception as error:
            with self.lock:
                job.update(status="failed", error=str(error))

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def is_running(self, root_id: int) -> bool:
        with self.lock:
            job_id = self.root_jobs.get(root_id)
            return bool(job_id and self.jobs.get(job_id, {}).get("status") == "running")


class DuplicateCleanupJobs:
    def __init__(self, database: LibraryDatabase, authorize_path):
        self.database = database
        self.authorize_path = authorize_path
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.active_id = ""
        self.lock = threading.Lock()

    def start(self, expected_groups: int, confirmation: str, force_protected: bool = False) -> Dict[str, Any]:
        required_confirmation = "FORCE_DELETE_DUPLICATES" if force_protected else "DELETE_DUPLICATES"
        if confirmation != required_confirmation:
            raise ValueError("缺少重复文件删除确认")
        with self.lock:
            if self.active_id and self.jobs.get(self.active_id, {}).get("status") == "running":
                return dict(self.jobs[self.active_id])
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id, "status": "running", "processed_groups": 0,
                "total_groups": expected_groups, "deleted": 0, "skipped": 0,
                "force_protected": force_protected, "result": None, "error": "",
            }
            self.jobs[job_id] = job
            self.active_id = job_id
        threading.Thread(
            target=self._run, args=(job_id, expected_groups, confirmation, force_protected), daemon=True,
        ).start()
        return dict(job)

    def _run(self, job_id: str, expected_groups: int, confirmation: str, force_protected: bool) -> None:
        def progress(values: Dict[str, int]) -> None:
            with self.lock:
                self.jobs[job_id].update(values)
        try:
            result = delete_duplicates(
                self.database, self.authorize_path, expected_groups, confirmation, progress,
                force_protected,
            )
            with self.lock:
                self.jobs[job_id].update(status="completed", result=result)
        except Exception as error:
            with self.lock:
                self.jobs[job_id].update(status="failed", error=str(error))

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None


class MeizangApplication:
    def __init__(self, database_path: str, static_dir: str, prefix: str = "/app/meizang", api_token: str = ""):
        self.database = LibraryDatabase(database_path)
        self.static_dir = Path(static_dir).resolve()
        self.data_dir = Path(database_path).expanduser().resolve().parent
        self.thumbnail_dir = self.data_dir / "thumbnails"
        self.prefix = prefix.rstrip("/")
        self.api_token = (api_token or os.environ.get("MEIZANG_TOKEN", "")).strip()
        # 仅当经由飞牛统一网关 Unix Socket 进入时，才采信网关注入的 NAS 身份头；
        # 直连 TCP 端口时忽略这些头，防止客户端伪造。
        self.trust_gateway_headers = False
        self.login_limiter = LoginRateLimiter()
        self.jobs = ScanJobs(self.database)
        raw_paths = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS", "")
        self.allowed_paths = [Path(item).resolve() for item in raw_paths.split(":") if item]
        self.enforce_allowed_paths = "TRIM_DATA_ACCESSIBLE_PATHS" in os.environ or bool(
            os.environ.get("TRIM_API_TOKEN", "").strip()
        )
        self.qb = QBIntegration(self.database, self.is_authorized_path)
        self.duplicate_jobs = DuplicateCleanupJobs(self.database, self.is_authorized_path)
        self.music = MusicJobs(self.database, self.is_authorized_path, self.normalize_writable_directory)
        self.mdc = MDCManager(
            self.database, self.normalize_root, self.is_authorized_path,
            on_complete=lambda root_id: self.jobs.start(root_id, force_metadata=True),
        )

    def refresh_allowed_paths(self) -> None:
        if not os.environ.get("TRIM_API_TOKEN", "").strip():
            return
        try:
            paths = shared_accessible_folders()
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return
        self.allowed_paths = [Path(item).resolve() for item in paths]
        self.enforce_allowed_paths = True

    def normalize_root(self, raw_path: str) -> Path:
        self.refresh_allowed_paths()
        path = Path(raw_path).expanduser().resolve()
        if (self.enforce_allowed_paths or self.allowed_paths) and not any(
            path == root or root in path.parents for root in self.allowed_paths
        ):
            raise ValueError("该目录尚未在飞牛应用设置中授权")
        if not path.is_dir():
            raise ValueError("目录不存在")
        if not os.access(str(path), os.R_OK):
            raise ValueError("目录没有读取权限")
        return path

    def add_library_root(self, raw_path: str, label: str = "") -> Dict[str, Any]:
        path = self.normalize_root(raw_path)
        for root in self.database.roots():
            existing = Path(root["path"]).resolve()
            if path == existing:
                return self.database.add_root(str(path), label or root.get("label", ""))
            if existing in path.parents or path in existing.parents:
                raise ValueError(
                    "不能添加相互包含的媒体目录；已存在：{}。请保留范围更合适的一个目录。".format(existing)
                )
        return self.database.add_root(str(path), label)

    def is_authorized_path(self, path: Path) -> bool:
        candidate = path.parent.resolve() / path.name
        return not (self.enforce_allowed_paths or self.allowed_paths) or any(
            candidate == root or root in candidate.parents for root in self.allowed_paths
        )

    def resolve_request_identity(self, headers, cookies: Dict[str, str]) -> Optional[Dict[str, Any]]:
        return resolve_auth_context(
            self.database, self.api_token, self.trust_gateway_headers, headers, cookies,
        )

    def needs_setup(self) -> bool:
        # 网关模式下 NAS 管理员即应用管理员，无需本地初始化
        return self.database.count_users() == 0 and not self.trust_gateway_headers

    def auth_status(self, headers, cookies: Dict[str, str]) -> Dict[str, Any]:
        context = self.resolve_request_identity(headers, cookies)
        return {
            "logged_in": bool(context),
            "username": context["username"] if context else "",
            "is_admin": bool(context and context["is_admin"]),
            "method": context["method"] if context else "",
            "needs_setup": self.needs_setup() and not bool(context),
            "token_configured": bool(self.api_token),
            "gateway_nas": self.trust_gateway_headers,
        }

    def issue_session(self, user_id: int) -> str:
        """签发会话：返回写入 Cookie 的随机令牌，服务端只存其哈希。"""
        now = time.time()
        self.database.delete_expired_sessions(now)
        raw_token = new_session_id()
        lookup_id = session_lookup_id(raw_token)
        self.database.create_session(lookup_id, user_id, lookup_id, now + SESSION_TTL_SECONDS)
        return raw_token

    def find_session(self, raw_token: str) -> Optional[Dict[str, Any]]:
        if not raw_token:
            return None
        return self.database.get_session(session_lookup_id(raw_token), time.time())

    def revoke_session(self, raw_token: str) -> None:
        if raw_token:
            self.database.delete_session(session_lookup_id(raw_token))

    def maybe_touch_session(self, raw_token: str) -> None:
        now = time.time()
        session = self.find_session(raw_token)
        if session and now - (session["last_seen_at"] or 0) >= SESSION_TOUCH_INTERVAL_SECONDS:
            self.database.touch_session(session_lookup_id(raw_token), now, now + SESSION_TTL_SECONDS)

    def normalize_writable_directory(self, raw_path: str) -> Path:
        path = self.normalize_root(raw_path)
        if not os.access(str(path), os.W_OK):
            raise ValueError("目录没有写入权限")
        return path

    def resolve_asset_file(self, asset_id: int) -> Dict[str, Any]:
        asset = self.database.asset(asset_id)
        if not asset:
            raise FileNotFoundError("媒体文件不存在")
        path = Path(asset["path"]).resolve()
        if not path.is_file():
            raise FileNotFoundError("媒体文件已不在原位置")
        if not self.is_authorized_path(path):
            raise PermissionError("该媒体文件尚未授权访问")
        asset["file_path"] = path
        return asset

    def thumbnail_path(self, asset: Dict[str, Any]) -> Path:
        return self.thumbnail_dir / "{}.jpg".format(asset["id"])

    def ensure_thumbnail(self, asset: Dict[str, Any]) -> Path:
        source = Path(asset["path"]).resolve()
        if asset["media_type"] == "image":
            return source
        if asset["media_type"] != "video":
            raise ValueError("该媒体类型没有缩略图")
        target = self.thumbnail_path(asset)
        if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return target
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise FileNotFoundError("系统未找到 ffmpeg，无法生成视频预览图")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp.jpg")
        try:
            subprocess.run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", "00:00:03", "-i", str(source),
                    "-frames:v", "1", "-vf", "scale=640:-2", str(temporary),
                ],
                check=True,
                env=media_tool_env(),
                timeout=45,
            )
            temporary.replace(target)
        except (subprocess.SubprocessError, OSError) as error:
            raise FileNotFoundError("视频预览图生成失败：{}".format(error)) from error
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return target


def make_handler(application: MeizangApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MediaVault/{}".format(__version__)

        def log_message(self, format_string, *args):
            print("{} - {}".format(self.client_address or "gateway", format_string % args))

        def send_json(self, status: int, payload: Any, extra_headers: Optional[Dict[str, str]] = None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def get_cookies(self) -> Dict[str, str]:
            jar = http_cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except Exception:
                return {}
            return {key: morsel.value for key, morsel in jar.items()}

        def session_cookie_header(self, raw_token: str, max_age: int) -> Dict[str, str]:
            return {
                "Set-Cookie": "{}={}; Path=/; HttpOnly; SameSite=Lax; Max-Age={}".format(
                    SESSION_COOKIE, raw_token, max_age,
                )
            }

        def clear_session_cookie_header(self) -> Dict[str, str]:
            return {
                "Set-Cookie": "{}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0".format(SESSION_COOKIE)
            }

        def client_ip(self) -> str:
            address = self.client_address
            if isinstance(address, tuple) and address:
                return str(address[0])
            # Unix Socket（飞牛网关）直连时没有对端 IP
            return "unix-socket"

        def auth_setup(self, payload: Dict[str, Any]):
            """首次初始化管理员账户：仅当尚未创建任何本地用户且非网关模式时可用。"""
            if not application.needs_setup():
                raise ValueError("管理员账户已存在，无需初始化")
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            if not username or len(username) > 32:
                raise ValueError("用户名长度必须在 1 到 32 个字符之间")
            if len(password) < 8 or len(password) > 128:
                raise ValueError("密码长度必须在 8 到 128 个字符之间")
            user = application.database.create_user(username, hash_password(password), is_admin=True)
            raw_token = application.issue_session(user["id"])
            return self.send_json(
                201,
                {"username": user["username"], "is_admin": True},
                self.session_cookie_header(raw_token, SESSION_TTL_SECONDS),
            )

        def auth_login(self, payload: Dict[str, Any]):
            ip = self.client_ip()
            locked, retry_after = application.login_limiter.is_locked(ip)
            if locked:
                return self.send_json(429, {"error": "登录失败次数过多，请 {} 秒后再试".format(retry_after)})
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            if not username or not password:
                application.login_limiter.register_failure(ip)
                raise ValueError("请输入用户名和密码")
            user = application.database.get_user_by_username(username)
            # 统一错误信息，避免用户名枚举
            if not user or not verify_password(password, user["password_hash"]):
                application.login_limiter.register_failure(ip)
                raise ValueError("用户名或密码错误")
            application.login_limiter.register_success(ip)
            raw_token = application.issue_session(user["id"])
            return self.send_json(
                200,
                {"username": user["username"], "is_admin": user["is_admin"]},
                self.session_cookie_header(raw_token, SESSION_TTL_SECONDS),
            )

        def auth_logout(self):
            application.revoke_session(self.get_cookies().get(SESSION_COOKIE, ""))
            return self.send_json(200, {"status": "ok"}, self.clear_session_cookie_header())

        def send_file(self, candidate: Path, download_name: str = "", inline: bool = True):
            mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            size = candidate.stat().st_size
            start, end = 0, size - 1
            status = 200
            range_header = self.headers.get("Range", "")
            if range_header.startswith("bytes="):
                requested = range_header[6:].split(",", 1)[0]
                left, _dash, right = requested.partition("-")
                try:
                    if left:
                        start = int(left)
                        end = int(right) if right else end
                    elif right:
                        start = max(0, size - int(right))
                    if start < 0 or end < start or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */{}".format(size))
                        self.end_headers()
                        return
                    end = min(end, size - 1)
                    status = 206
                except ValueError:
                    start, end = 0, size - 1
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "private, max-age=3600")
            if status == 206:
                self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
            if download_name:
                disposition = "inline" if inline else "attachment"
                suffix = candidate.suffix if candidate.suffix.isascii() else ""
                fallback = "media{}".format(suffix.replace('"', ""))
                self.send_header(
                    "Content-Disposition",
                    '{}; filename="{}"; filename*=UTF-8\'\'{}'.format(
                        disposition, fallback, quote(download_name, safe=""),
                    ),
                )
            self.end_headers()
            with candidate.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def body_json(self) -> Dict[str, Any]:
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 1024 * 1024)
                return json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                raise ValueError("请求 JSON 无效")

        def require_api_auth(self, path: str) -> bool:
            context = application.resolve_request_identity(self.headers, self.get_cookies())
            if context:
                if context.get("method") == "session":
                    application.maybe_touch_session(context.get("session_id", ""))
                self.auth_context = context
                return True
            self.auth_context = None
            # 401 时告诉前端走哪种登录：有本地用户（或可初始化）走登录页，
            # 仅配置了 API Token 且无本地用户时走 Token 输入。
            auth_hint = "session" if (application.database.count_users() > 0
                                      or not application.api_token) else "token"
            self.send_json(401, {"error": "需要登录", "auth": auth_hint})
            return False

        def route_path(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == application.prefix:
                path = "/"
            elif path.startswith(application.prefix + "/"):
                path = path[len(application.prefix):]
            return path, parse_qs(parsed.query)

        def do_GET(self):
            try:
                path, query = self.route_path()
                if path == "/api/health":
                    return self.send_json(200, {"status": "ok", "version": __version__})
                if path == "/api/auth/status":
                    return self.send_json(
                        200, application.auth_status(self.headers, self.get_cookies()))
                if not self.require_api_auth(path):
                    return
                if path == "/api/stats":
                    return self.send_json(200, application.database.stats())
                if path == "/api/roots":
                    return self.send_json(200, application.database.roots())
                if path == "/api/authorized-paths":
                    application.refresh_allowed_paths()
                    return self.send_json(200, [str(path) for path in application.allowed_paths])
                if path == "/api/settings/providers":
                    return self.send_json(200, public_provider_settings(application.database.settings()))
                if path == "/api/settings/qbittorrent":
                    return self.send_json(200, public_qb_settings(
                        application.database.settings(), application.database.managed_link_stats(),
                    ))
                if path == "/api/settings/mdc":
                    return self.send_json(200, application.mdc.config.public())
                if path == "/api/settings/music":
                    return self.send_json(200, public_music_settings(application.database.settings()))
                if path == "/api/music/jobs/latest":
                    job = application.music.latest()
                    return self.send_json(200, job) if job else self.send_json(200, {})
                if path.startswith("/api/music/jobs/"):
                    job = application.music.get(path.rsplit("/", 1)[-1])
                    return self.send_json(200, job) if job else self.send_json(404, {"error": "音乐任务不存在"})
                if path == "/api/mdc/jobs":
                    return self.send_json(200, application.database.mdc_jobs(int(query.get("limit", ["50"])[0])))
                if path.startswith("/api/mdc/jobs/"):
                    job = application.database.mdc_job(path.rsplit("/", 1)[-1])
                    return self.send_json(200, job) if job else self.send_json(404, {"error": "MDC 任务不存在"})
                if path == "/api/mdc/schedules":
                    return self.send_json(200, application.database.mdc_schedules())
                if path == "/api/assets":
                    items = application.database.assets(
                        query.get("type", [""])[0], query.get("q", [""])[0],
                        int(query.get("limit", ["200"])[0]),
                    )
                    return self.send_json(200, items)
                if path.startswith("/api/assets/") and path.endswith("/file"):
                    asset_id = int(path.split("/")[-2])
                    asset = application.resolve_asset_file(asset_id)
                    return self.send_file(asset["file_path"], asset["filename"], inline=True)
                if path.startswith("/api/assets/") and path.endswith("/thumbnail"):
                    asset_id = int(path.split("/")[-2])
                    asset = application.resolve_asset_file(asset_id)
                    thumbnail = application.ensure_thumbnail(asset)
                    return self.send_file(thumbnail, "{}.jpg".format(asset["id"]))
                if path == '/api/qbittorrent/links':
                    return self.send_json(200, [{key: row[key] for key in ('source_path', 'library_path', 'status', 'message')} for row in application.database.managed_links()[:100]])
                if path == "/api/duplicates":
                    return self.send_json(200, application.database.duplicates())
                if path.startswith("/api/duplicates/delete/"):
                    job = application.duplicate_jobs.get(path.rsplit("/", 1)[-1])
                    return self.send_json(200, job) if job else self.send_json(404, {"error": "删除任务不存在"})
                if path.startswith("/api/scans/"):
                    job = application.jobs.get(path.rsplit("/", 1)[-1])
                    return self.send_json(200, job) if job else self.send_json(404, {"error": "扫描任务不存在"})
                if path.startswith("/api/"):
                    return self.send_json(404, {"error": "接口不存在"})
                return self.serve_static(path)
            except (ValueError, OSError) as error:
                return self.send_json(400, {"error": str(error)})
            except Exception as error:
                traceback.print_exc()
                return self.send_json(500, {"error": "服务器内部错误"})

        def do_POST(self):
            try:
                path, _query = self.route_path()
                payload = self.body_json()
                if path == "/api/auth/setup":
                    return self.auth_setup(payload)
                if path == "/api/auth/login":
                    return self.auth_login(payload)
                if not self.require_api_auth(path):
                    return
                if path == "/api/auth/logout":
                    return self.auth_logout()
                if path == "/api/settings/providers/test":
                    settings = application.database.settings()
                    tmdb = payload.get("tmdb", {})
                    proxy = payload.get("proxy", {})
                    token = str(tmdb.get("token", "")).strip() or settings["tmdb_token"]
                    if not token:
                        raise ValueError("测试连接前请填写 TMDB API Token")
                    proxy_enabled = proxy.get("enabled") is True
                    proxy_url = str(proxy.get("url", "")).strip() or settings["proxy_url"]
                    if proxy_enabled:
                        proxy_url = validate_proxy_url(proxy_url)
                    else:
                        proxy_url = ""
                    try:
                        connected = TMDBProvider(
                            token, str(tmdb.get("language", settings["tmdb_language"])),
                            timeout=10.0, proxy_url=proxy_url,
                        ).test_connection()
                    except Exception as error:
                        message = str(error).replace(proxy_url, "***") if proxy_url else str(error)
                        raise ValueError("TMDB 连接失败：{}".format(message))
                    if not connected:
                        raise ValueError("TMDB 返回了无效响应")
                    return self.send_json(200, {"ok": True, "message": "TMDB 连接成功"})
                if path == "/api/qbittorrent/test":
                    settings = effective_qb_settings(application.database.settings(), payload)
                    validate_qb_connection_settings(settings)
                    result = application.qb.test_connection(settings)
                    return self.send_json(200, result)
                if path == "/api/qbittorrent/sync":
                    application.refresh_allowed_paths()
                    return self.send_json(200, application.qb.sync_once())
                if path == "/api/mdc/jobs":
                    application.refresh_allowed_paths()
                    return self.send_json(202, application.mdc.submit(payload))
                if path == "/api/music/jobs":
                    application.refresh_allowed_paths()
                    root_id = int(payload.get("root_id", 0))
                    if application.jobs.is_running(root_id):
                        raise ValueError("该目录仍在扫描，请等待扫描完成后再开始音乐刮削")
                    return self.send_json(202, application.music.start(root_id, payload))
                if path.startswith("/api/music/jobs/") and path.endswith("/cancel"):
                    return self.send_json(200, application.music.cancel(path.split("/")[-2]))
                if path == "/api/settings/mdc/reset":
                    return self.send_json(200, application.mdc.config.reset())
                if path.startswith("/api/mdc/jobs/") and path.endswith("/cancel"):
                    return self.send_json(200, application.mdc.cancel(path.split("/")[-2]))
                if path == "/api/mdc/schedules":
                    application.refresh_allowed_paths()
                    return self.send_json(201, application.mdc.create_schedule(payload))
                if path == "/api/roots":
                    root = application.add_library_root(
                        str(payload.get("path", "")).strip(), str(payload.get("label", "")).strip(),
                    )
                    return self.send_json(201, root)
                if path == "/api/scans":
                    root_id = int(payload.get("root_id", 0))
                    if not application.database.root(root_id):
                        return self.send_json(404, {"error": "媒体目录不存在"})
                    return self.send_json(202, application.jobs.start(root_id, payload.get("force_metadata") is True))
                if path == "/api/duplicates/delete":
                    application.refresh_allowed_paths()
                    return self.send_json(202, application.duplicate_jobs.start(
                        int(payload.get("expected_groups", -1)),
                        str(payload.get("confirmation", "")),
                        payload.get("force_protected") is True,
                    ))
                if path == "/api/duplicates/delete-file":
                    application.refresh_allowed_paths()
                    return self.send_json(200, delete_duplicate_file(
                        application.database,
                        application.is_authorized_path,
                        str(payload.get("path", "")),
                        str(payload.get("sha256", "")),
                        str(payload.get("confirmation", "")),
                        payload.get("force_protected") is True,
                    ))
                return self.send_json(404, {"error": "接口不存在"})
            except (ValueError, OSError) as error:
                return self.send_json(400, {"error": str(error)})
            except Exception:
                traceback.print_exc()
                return self.send_json(500, {"error": "服务器内部错误"})

        def do_PUT(self):
            try:
                path, _query = self.route_path()
                if not self.require_api_auth(path):
                    return
                payload = self.body_json()
                if path == "/api/settings/music":
                    library_root = str(payload.get("library_root", "")).strip()
                    if payload.get("move_files"):
                        library_root = str(application.normalize_writable_directory(library_root))
                    settings = application.database.update_settings({
                        "musicbrainz_enabled": "true" if payload.get("enabled", True) else "false",
                        "music_library_root": library_root,
                        "music_write_tags": "true" if payload.get("write_tags") else "false",
                        "music_download_cover": "true" if payload.get("download_cover") else "false",
                        "music_move_files": "true" if payload.get("move_files") else "false",
                    })
                    return self.send_json(200, public_music_settings(settings))
                if path == "/api/settings/qbittorrent":
                    current = application.database.settings()
                    settings = effective_qb_settings(current, payload)
                    updates = validate_qb_settings(application, settings)
                    if payload.get("password"):
                        updates["qb_password"] = str(payload["password"])
                    if payload.get("clear_password"):
                        updates["qb_password"] = ""
                    saved = application.database.update_settings(updates)
                    return self.send_json(200, public_qb_settings(saved, application.database.managed_link_stats()))
                if path == "/api/settings/mdc":
                    return self.send_json(200, application.mdc.config.save(payload.get("sections", payload)))
                if path != "/api/settings/providers":
                    return self.send_json(404, {"error": "接口不存在"})
                tmdb = payload.get("tmdb", {})
                proxy = payload.get("proxy", {})
                language = str(tmdb.get("language", "zh-CN"))
                if language not in ("zh-CN", "zh-TW", "en-US", "ja-JP"):
                    raise ValueError("不支持的 TMDB 语言")
                updates = {
                    "tmdb_enabled": "true" if tmdb.get("enabled") else "false",
                    "tmdb_language": language,
                }
                token = str(tmdb.get("token", "")).strip()
                current_token = application.database.settings()["tmdb_token"]
                if tmdb.get("enabled") and not (token or current_token):
                    raise ValueError("启用 TMDB 前请填写 API Token")
                if token:
                    updates["tmdb_token"] = token
                if tmdb.get("clear_token"):
                    updates["tmdb_token"] = ""
                    updates["tmdb_enabled"] = "false"
                proxy_url = str(proxy.get("url", "")).strip()
                current_proxy_url = application.database.settings()["proxy_url"]
                if proxy.get("clear_url"):
                    proxy_url = ""
                    current_proxy_url = ""
                effective_proxy_url = proxy_url or current_proxy_url
                if proxy.get("enabled") and not effective_proxy_url:
                    raise ValueError("启用代理前请填写代理地址")
                if effective_proxy_url:
                    validate_proxy_url(effective_proxy_url)
                updates["proxy_enabled"] = "true" if proxy.get("enabled") else "false"
                if proxy_url or proxy.get("clear_url"):
                    updates["proxy_url"] = proxy_url
                settings = application.database.update_settings(updates)
                return self.send_json(200, public_provider_settings(settings))
            except (ValueError, OSError) as error:
                return self.send_json(400, {"error": str(error)})
            except Exception:
                traceback.print_exc()
                return self.send_json(500, {"error": "服务器内部错误"})

        def do_PATCH(self):
            try:
                path, _query = self.route_path()
                if not self.require_api_auth(path):
                    return
                payload = self.body_json()
                if path.startswith("/api/mdc/schedules/"):
                    schedule_id = int(path.rsplit("/", 1)[-1])
                    if "interval_seconds" in payload and int(payload["interval_seconds"]) < 60:
                        raise ValueError("间隔不能小于 60 秒")
                    application.database.update_mdc_schedule(schedule_id, payload)
                    return self.send_json(200, {"status": "ok"})
                return self.send_json(404, {"error": "接口不存在"})
            except (ValueError, OSError) as error:
                return self.send_json(400, {"error": str(error)})
            except Exception:
                traceback.print_exc()
                return self.send_json(500, {"error": "服务器内部错误"})

        def do_DELETE(self):
            try:
                path, _query = self.route_path()
                if not self.require_api_auth(path):
                    return
                if path.startswith("/api/mdc/schedules/"):
                    application.database.delete_mdc_schedule(int(path.rsplit("/", 1)[-1]))
                    return self.send_json(200, {"status": "ok"})
                if path.startswith("/api/roots/"):
                    root_id = int(path.rsplit("/", 1)[-1])
                    if application.jobs.is_running(root_id) or application.music.is_running(root_id):
                        raise ValueError("该目录仍有任务运行，请先等待任务结束或取消音乐任务")
                    root = application.database.delete_root(root_id)
                    if not root:
                        return self.send_json(404, {"error": "媒体目录不存在"})
                    return self.send_json(200, {
                        "status": "ok", "path": root["path"],
                        "message": "目录已从媒藏移除，磁盘文件未被删除",
                    })
                return self.send_json(404, {"error": "接口不存在"})
            except (ValueError, OSError) as error:
                return self.send_json(400, {"error": str(error)})
            except Exception:
                traceback.print_exc()
                return self.send_json(500, {"error": "服务器内部错误"})

        def serve_static(self, request_path: str):
            relative = unquote(request_path).lstrip("/") or "index.html"
            candidate = (application.static_dir / relative).resolve()
            if application.static_dir not in candidate.parents and candidate != application.static_dir:
                return self.send_json(403, {"error": "禁止访问"})
            if not candidate.is_file():
                candidate = application.static_dir / "index.html"
            if not candidate.is_file():
                return self.send_json(404, {"error": "页面资源不存在"})
            content = candidate.read_bytes()
            mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache" if candidate.name == "index.html" else "public, max-age=3600")
            self.end_headers()
            self.wfile.write(content)

    return Handler


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(database_path: str, static_dir: str, host: str, port: int, socket_path: str, prefix: str,
          api_token: str = ""):
    application = MeizangApplication(database_path, static_dir, prefix, api_token)
    # Unix Socket 模式即经由飞牛统一网关进入：网关已校验 NAS 会话，
    # 此时采信其注入的 X-Trim-Username 等身份头实现 NAS 账户直接登录。
    application.trust_gateway_headers = bool(socket_path)
    handler = make_handler(application)
    if socket_path:
        socket_file = Path(socket_path)
        socket_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            socket_file.unlink()
        except FileNotFoundError:
            pass
        server = ThreadingUnixHTTPServer(str(socket_file), handler)
        print("媒藏 listening on unix socket {}".format(socket_file), flush=True)
    else:
        server = ThreadingHTTPServer((host, port), handler)
        print("媒藏 listening on http://{}:{}{}".format(host, port, prefix), flush=True)
    application.qb.start()
    application.mdc.start()
    try:
        server.serve_forever()
    finally:
        application.mdc.stop()
        application.qb.stop()
        server.server_close()
        if socket_path:
            try:
                Path(socket_path).unlink()
            except FileNotFoundError:
                pass
