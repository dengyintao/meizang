import configparser
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .database import LibraryDatabase


MDC_ROOT = Path(__file__).resolve().parents[1] / "third_party" / "movie_data_capture"
MDC_ENTRY = MDC_ROOT / "Movie_Data_Capture.py"
SECRET_FIELDS = {("translate", "key")}
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def parse_ini(text: str) -> Dict[str, Dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as error:
        raise ValueError("MDC 配置格式错误：{}".format(error))
    if not parser.has_section("common"):
        raise ValueError("MDC 配置缺少 [common] 分组")
    required = {"main_mode", "source_folder", "failed_output_folder", "success_output_folder"}
    missing = required - set(parser["common"])
    if missing:
        raise ValueError("MDC 配置缺少：{}".format(", ".join(sorted(missing))))
    try:
        mode = parser.getint("common", "main_mode")
    except ValueError:
        raise ValueError("common.main_mode 必须是整数")
    if mode not in (1, 2, 3):
        raise ValueError("common.main_mode 只能是 1、2 或 3")
    return {section: dict(parser[section]) for section in parser.sections()}


def write_ini(sections: Dict[str, Dict[str, Any]]) -> str:
    parser = configparser.ConfigParser(interpolation=None)
    for section, values in sections.items():
        if not isinstance(values, dict):
            raise ValueError("配置分组 {} 必须是对象".format(section))
        parser[section] = {str(key): str(value) for key, value in values.items()}
    stream = io.StringIO()
    parser.write(stream)
    text = stream.getvalue()
    parse_ini(text)
    return text


class MDCConfigStore:
    def __init__(self, database: LibraryDatabase):
        self.database = database

    def text(self) -> str:
        stored = self.database.settings().get("mdc_config_ini", "")
        return stored or (MDC_ROOT / "config.ini").read_text(encoding="utf-8-sig")

    def public(self) -> Dict[str, Any]:
        sections = parse_ini(self.text())
        secrets = {}
        for section, key in SECRET_FIELDS:
            value = sections.get(section, {}).get(key, "")
            secrets["{}.{}".format(section, key)] = bool(value)
            if value:
                sections[section][key] = ""
        return {"sections": sections, "secrets": secrets, "provider_count": self.provider_count()}

    def save(self, incoming: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(incoming, dict):
            raise ValueError("MDC 配置必须是对象")
        current = parse_ini(self.text())
        for section, values in incoming.items():
            if section not in current:
                raise ValueError("未知 MDC 配置分组：{}".format(section))
            if not isinstance(values, dict):
                raise ValueError("配置分组 {} 必须是对象".format(section))
            for key, value in values.items():
                if key not in current[section]:
                    raise ValueError("未知 MDC 配置项：{}.{}".format(section, key))
                if (section, key) in SECRET_FIELDS and str(value) == "" and current[section][key]:
                    continue
                current[section][key] = str(value)
        text = write_ini(current)
        self.database.update_settings({"mdc_config_ini": text})
        return self.public()

    def reset(self) -> Dict[str, Any]:
        self.database.update_settings({"mdc_config_ini": ""})
        return self.public()

    @staticmethod
    def provider_count() -> int:
        return len([path for path in (MDC_ROOT / "scrapinglib").glob("*.py") if path.stem not in {"__init__", "api", "parser", "utils", "httprequest", "storyline"}])


class MDCManager:
    def __init__(
        self,
        database: LibraryDatabase,
        normalize_root: Callable[[str], Path],
        is_authorized_path: Callable[[Path], bool],
        on_complete: Optional[Callable[[int], None]] = None,
    ):
        self.database = database
        self.config = MDCConfigStore(database)
        self.normalize_root = normalize_root
        self.is_authorized_path = is_authorized_path
        self.on_complete = on_complete
        self.lock = threading.Lock()
        self.processes: Dict[str, subprocess.Popen] = {}
        self.stop_event = threading.Event()
        self.scheduler = threading.Thread(target=self._schedule_loop, name="mdc-scheduler", daemon=True)
        self.database.recover_mdc_jobs()

    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            process.terminate()

    def submit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize_payload(payload)
        with self.lock:
            if self.processes or any(job["status"] in ("queued", "running") for job in self.database.mdc_jobs(20)):
                raise ValueError("已有 MDC 任务正在运行")
            job_id = uuid.uuid4().hex
            job = self.database.create_mdc_job(job_id, normalized["kind"], normalized)
        threading.Thread(target=self._run, args=(job_id,), name="mdc-{}".format(job_id[:8]), daemon=True).start()
        return job

    def cancel(self, job_id: str) -> Dict[str, Any]:
        job = self.database.mdc_job(job_id)
        if not job:
            raise ValueError("MDC 任务不存在")
        self.database.cancel_mdc_job(job_id)
        with self.lock:
            process = self.processes.get(job_id)
        if process and process.poll() is None:
            process.terminate()
        return self.database.mdc_job(job_id)

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(payload.get("kind", "scan"))
        if kind not in ("scan", "file", "search"):
            raise ValueError("不支持的 MDC 任务类型")
        value: Dict[str, Any] = {"kind": kind}
        if kind == "search":
            number = str(payload.get("number", "")).strip()
            if not number:
                raise ValueError("搜索任务必须填写番号")
            value.update(number=number, source=str(payload.get("source", "")).strip())
            return value
        root_id = int(payload.get("root_id", 0))
        root = self.database.root(root_id)
        if not root:
            raise ValueError("媒体目录不存在")
        source = str(self.normalize_root(str(payload.get("source_folder") or root["path"])))
        kind = "file" if kind == "file" else "scan"
        if kind == "file" and not Path(source).is_file():
            raise ValueError("单文件任务路径不是文件")
        mode = int(payload.get("mode", parse_ini(self.config.text())["common"]["main_mode"]))
        if mode not in (1, 2, 3):
            raise ValueError("任务模式只能是 1、2 或 3")
        self._validate_outputs(source, mode)
        value.update(
            kind=kind, root_id=root_id, source_folder=source, mode=mode,
            no_network=bool(payload.get("no_network", False)), dry_run=bool(payload.get("dry_run", False)),
            regex=str(payload.get("regex", "")), source=str(payload.get("source", "")).strip(),
        )
        return value

    def _validate_outputs(self, source: str, mode: int) -> None:
        sections = parse_ini(self.config.text())
        base = Path(source).parent if Path(source).is_file() else Path(source)
        names = ["failed_output_folder"] + (["success_output_folder"] if mode in (1, 2) else [])
        for name in names:
            configured = Path(sections["common"][name]).expanduser()
            target = configured if configured.is_absolute() else base / configured
            candidate = target.parent.resolve() / target.name
            if not self.is_authorized_path(candidate):
                raise ValueError("{} 不在飞牛授权目录内：{}".format(name, candidate))

    def _job_config(self, job_id: str, payload: Dict[str, Any]) -> Path:
        sections = parse_ini(self.config.text())
        sections["common"]["source_folder"] = payload.get("source_folder", sections["common"]["source_folder"])
        source = Path(payload.get("source_folder", "."))
        base = source.parent if source.is_file() else source
        for key in ("failed_output_folder", "success_output_folder"):
            configured = Path(sections["common"][key]).expanduser()
            if not configured.is_absolute():
                sections["common"][key] = str(base / configured)
        if "mode" in payload:
            sections["common"]["main_mode"] = str(payload["mode"])
        workspace = Path(self.database.path).parent / "mdc" / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "config.ini"
        path.write_text(write_ini(sections), encoding="utf-8")
        return workspace

    def _command(self, payload: Dict[str, Any]) -> list:
        command = [sys.executable, str(MDC_ENTRY), "-o", ""]
        if payload["kind"] == "search":
            command += ["--search", payload["number"]]
            if payload.get("source"):
                command += ["--specified-source", payload["source"]]
            return command
        if payload["kind"] == "file":
            command.append(payload["source_folder"])
        else:
            command += ["--path", payload["source_folder"]]
        command += ["--main-mode", str(payload["mode"]), "--auto-exit"]
        if payload.get("regex"):
            command += ["--regex-query", payload["regex"]]
        if payload.get("no_network"):
            command.append("--no-network-operation")
        if payload.get("dry_run"):
            command.append("--zero-operation")
        if payload.get("source"):
            command += ["--website", payload["source"]]
        return command

    def _run(self, job_id: str) -> None:
        job = self.database.mdc_job(job_id)
        payload = job["payload"]
        workspace = self._job_config(job_id, payload)
        env = dict(os.environ)
        runtime_path = str(MDC_ROOT)
        env["PYTHONPATH"] = runtime_path + os.pathsep + env.get("PYTHONPATH", "")
        self.database.update_mdc_job(job_id, status="running", started_at=time.strftime("%Y-%m-%d %H:%M:%S"), message="MDC 引擎启动中")
        try:
            process = subprocess.Popen(
                self._command(payload), cwd=str(workspace), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            )
            with self.lock:
                self.processes[job_id] = process
            assert process.stdout is not None
            for line in process.stdout:
                line = ANSI_ESCAPE.sub("", line)
                self.database.append_mdc_log(job_id, line)
                count_match = re.search(r"Find\s+(\d+)\s+movies", line, re.I)
                progress_match = re.search(r"\[(\d+)\s*/\s*(\d+)\]", line)
                if count_match:
                    self.database.update_mdc_job(job_id, total=int(count_match.group(1)))
                elif progress_match:
                    self.database.update_mdc_job(job_id, progress=int(progress_match.group(1)), total=int(progress_match.group(2)))
                current = self.database.mdc_job(job_id)
                if current and current["cancel_requested"] and process.poll() is None:
                    process.terminate()
            return_code = process.wait()
            current = self.database.mdc_job(job_id)
            if current and current["cancel_requested"]:
                self.database.update_mdc_job(job_id, status="cancelled", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"), message="任务已取消")
            elif return_code:
                self.database.update_mdc_job(job_id, status="failed", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"), error="MDC 退出码 {}".format(return_code), message="MDC 执行失败")
            else:
                self.database.update_mdc_job(job_id, status="completed", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"), message="MDC 执行完成")
                if self.on_complete and payload.get("root_id"):
                    self.on_complete(payload["root_id"])
        except Exception as error:
            self.database.append_mdc_log(job_id, "错误：{}".format(error))
            self.database.update_mdc_job(job_id, status="failed", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"), error=str(error), message="MDC 执行失败")
        finally:
            with self.lock:
                self.processes.pop(job_id, None)

    def create_schedule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = str(payload.get("name", "MDC 定时刮削")).strip()
        interval = int(payload.get("interval_seconds", 86400))
        if not name or interval < 60:
            raise ValueError("任务名称不能为空，间隔不能小于 60 秒")
        normalized = self._normalize_payload(payload.get("payload", {}))
        return self.database.create_mdc_schedule(name, interval, normalized)

    def _schedule_loop(self) -> None:
        while not self.stop_event.wait(2):
            now = time.time()
            for schedule in self.database.due_mdc_schedules(now):
                try:
                    self.submit(schedule["payload"])
                except ValueError:
                    continue
                self.database.advance_mdc_schedule(schedule["id"], now, schedule["interval_seconds"])
