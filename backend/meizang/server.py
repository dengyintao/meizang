import json
import mimetypes
import os
import socket
import socketserver
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .database import LibraryDatabase
from .fnos_api import shared_accessible_folders
from .scanner import scan_root


class ScanJobs:
    def __init__(self, database: LibraryDatabase):
        self.database = database
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.root_jobs: Dict[int, str] = {}
        self.lock = threading.Lock()

    def start(self, root_id: int) -> Dict[str, Any]:
        with self.lock:
            existing_id = self.root_jobs.get(root_id)
            if existing_id and self.jobs.get(existing_id, {}).get("status") == "running":
                return self.jobs[existing_id]
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "root_id": root_id, "status": "running", "result": None, "error": ""}
            self.jobs[job_id] = job
            self.root_jobs[root_id] = job_id
        threading.Thread(target=self._run, args=(job_id,), daemon=True).start()
        return job

    def _run(self, job_id: str) -> None:
        job = self.jobs[job_id]
        try:
            result = scan_root(self.database, job["root_id"])
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
        self.prefix = prefix.rstrip("/")
        self.jobs = ScanJobs(self.database)
        raw_paths = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS", "")
        self.allowed_paths = [Path(item).resolve() for item in raw_paths.split(":") if item]
        self.enforce_allowed_paths = "TRIM_DATA_ACCESSIBLE_PATHS" in os.environ or bool(
            os.environ.get("TRIM_API_TOKEN", "").strip()
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
                if path == "/api/assets":
                    items = application.database.assets(
                        query.get("type", [""])[0], query.get("q", [""])[0],
                        int(query.get("limit", ["200"])[0]),
                    )
                    return self.send_json(200, items)
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
                if path == "/api/roots":
                    root = application.normalize_root(str(payload.get("path", "")).strip())
                    return self.send_json(201, application.database.add_root(str(root), str(payload.get("label", "")).strip()))
                if path == "/api/scans":
                    root_id = int(payload.get("root_id", 0))
                    if not application.database.root(root_id):
                        return self.send_json(404, {"error": "媒体目录不存在"})
                    return self.send_json(202, application.jobs.start(root_id))
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
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if socket_path:
            try:
                Path(socket_path).unlink()
            except FileNotFoundError:
                pass
