import json
import mimetypes
import os
import shutil
import socket
import socketserver
import subprocess
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .database import LibraryDatabase
from .duplicate_cleanup import delete_duplicates
from .fnos_api import shared_accessible_folders
from .mdc_bridge import MDCManager
from .organizer import QBIntegration
from .providers.local import media_tool_env
from .providers.tmdb import TMDBProvider
from .qbittorrent import validate_qb_url
from .scanner import scan_root


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
        job = self.jobs[job_id]
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


class MeizangApplication:
    def __init__(self, database_path: str, static_dir: str, prefix: str = "/app/meizang"):
        self.database = LibraryDatabase(database_path)
        self.static_dir = Path(static_dir).resolve()
        self.data_dir = Path(database_path).expanduser().resolve().parent
        self.thumbnail_dir = self.data_dir / "thumbnails"
        self.prefix = prefix.rstrip("/")
        self.jobs = ScanJobs(self.database)
        raw_paths = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS", "")
        self.allowed_paths = [Path(item).resolve() for item in raw_paths.split(":") if item]
        self.enforce_allowed_paths = "TRIM_DATA_ACCESSIBLE_PATHS" in os.environ or bool(
            os.environ.get("TRIM_API_TOKEN", "").strip()
        )
        self.qb = QBIntegration(self.database, self.is_authorized_path)
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

    def is_authorized_path(self, path: Path) -> bool:
        candidate = path.parent.resolve() / path.name
        return not (self.enforce_allowed_paths or self.allowed_paths) or any(
            candidate == root or root in candidate.parents for root in self.allowed_paths
        )

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

        def send_json(self, status: int, payload: Any):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

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
                self.send_header(
                    "Content-Disposition",
                    '{}; filename="{}"'.format(disposition, download_name.replace('"', "")),
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
                if path == "/api/settings/mdc/reset":
                    return self.send_json(200, application.mdc.config.reset())
                if path.startswith("/api/mdc/jobs/") and path.endswith("/cancel"):
                    return self.send_json(200, application.mdc.cancel(path.split("/")[-2]))
                if path == "/api/mdc/schedules":
                    application.refresh_allowed_paths()
                    return self.send_json(201, application.mdc.create_schedule(payload))
                if path == "/api/roots":
                    root = application.normalize_root(str(payload.get("path", "")).strip())
                    return self.send_json(201, application.database.add_root(str(root), str(payload.get("label", "")).strip()))
                if path == "/api/scans":
                    root_id = int(payload.get("root_id", 0))
                    if not application.database.root(root_id):
                        return self.send_json(404, {"error": "媒体目录不存在"})
                    return self.send_json(202, application.jobs.start(root_id, payload.get("force_metadata") is True))
                if path == "/api/duplicates/delete":
                    application.refresh_allowed_paths()
                    return self.send_json(200, delete_duplicates(
                        application.database,
                        application.is_authorized_path,
                        int(payload.get("expected_groups", -1)),
                        str(payload.get("confirmation", "")),
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
                payload = self.body_json()
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
                if path.startswith("/api/mdc/schedules/"):
                    application.database.delete_mdc_schedule(int(path.rsplit("/", 1)[-1]))
                    return self.send_json(200, {"status": "ok"})
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


def serve(database_path: str, static_dir: str, host: str, port: int, socket_path: str, prefix: str):
    application = MeizangApplication(database_path, static_dir, prefix)
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
