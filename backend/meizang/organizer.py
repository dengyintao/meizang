import os
import json
import stat
import re
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Set

from .database import LibraryDatabase
from .qbittorrent import QBittorrentClient
from .scanner import EXTENSIONS, scan_root


SIDECAR_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".nfo"}
MANAGED_EXTENSIONS = EXTENSIONS['video'] | SIDECAR_EXTENSIONS


def safe_component(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:180] or "未命名"


def map_qb_path(remote_path: str, remote_prefix: str = "", local_prefix: str = "") -> Path:
    remote = PurePosixPath(remote_path)
    if not remote.is_absolute() or ".." in remote.parts:
        raise ValueError("qBittorrent 返回了不安全的文件路径")
    if remote_prefix or local_prefix:
        if not remote_prefix or not local_prefix:
            raise ValueError("qB 路径映射的远端和本地前缀必须同时填写")
        try:
            relative = remote.relative_to(PurePosixPath(remote_prefix))
        except ValueError:
            raise ValueError("qB 路径不在配置的远端路径前缀内：{}".format(remote_path))
        return Path(local_prefix).joinpath(*relative.parts).absolute()
    return Path(remote_path).absolute()


def managed_relative_path(torrent_name: str, file_name: str) -> Path:
    relative = PurePosixPath(file_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("qBittorrent 返回了不安全的相对路径")
    parts = list(relative.parts)
    if parts and safe_component(parts[0]).casefold() == safe_component(torrent_name).casefold():
        parts = parts[1:]
    if not parts:
        parts = [safe_component(torrent_name)]
    return Path(safe_component(torrent_name)).joinpath(*(safe_component(part) for part in parts))


def symlink_points_to(source: Path, destination: Path) -> bool:
    if not source.is_symlink():
        return False
    linked = Path(os.readlink(str(source)))
    if not linked.is_absolute():
        linked = source.parent / linked
    return linked.resolve(strict=False) == destination.resolve(strict=False)


class MediaOrganizer:
    def __init__(self, database: LibraryDatabase, authorize_path: Callable[[Path], bool]):
        self.database = database
        self.authorize_path = authorize_path

    def _check_parent(self, path):
        for parent in (path.parent, *path.parent.parents):
            if parent.is_symlink():
                raise ValueError('路径包含目录软链接，停止操作')

    @staticmethod
    def _identity(path):
        info = path.lstat()
        return {'dev': info.st_dev, 'ino': info.st_ino, 'size': info.st_size, 'mtime': info.st_mtime_ns}

    def organize_torrent(self, client: QBittorrentClient, torrent: Dict[str, Any], settings: Dict[str, str]) -> Dict[str, int]:
        result = {"created": 0, "unchanged": 0, "failed": 0}
        torrent_hash = str(torrent.get("hash", "")).lower()
        torrent_name = str(torrent.get("name") or torrent_hash)
        files = [
            item for item in client.files(torrent_hash)
            if float(item.get("progress", 0)) >= 0.999999
            and int(item.get("priority", 1)) != 0
            and Path(str(item.get("name", ""))).suffix.lower() in MANAGED_EXTENSIONS
        ]
        if not files:
            return result

        library_root = Path(settings["qb_library_root"]).absolute()
        candidates = []
        for item in files:
            source = self._source_path(torrent, item, settings, len(files))
            destination = library_root / managed_relative_path(torrent_name, str(item["name"]))
            candidates.append((source, destination))

        requires_pause = any(source.exists() and not source.is_symlink() for source, _destination in candidates)
        state = str(torrent.get("state", "")).lower()
        resume_after = requires_pause and not any(word in state for word in ("paused", "stopped", "error"))
        try:
            if requires_pause:
                client.pause(torrent_hash)
                for attempt in range(20):
                    fresh = next((item for item in client.torrents() if item['hash'].lower() == torrent_hash), None)
                    if not fresh:
                        raise ValueError('任务已移除，停止整理')
                    if fresh.get('save_path') != torrent.get('save_path'):
                        raise ValueError('暂停期间下载目录改变，停止整理')
                    if str(fresh['state']).lower() in ('pausedup', 'stoppedup') and fresh.get('amount_left') == 0:
                        break
                    time.sleep(0.25)
                else:
                    raise ValueError('qB 未确认暂停，停止整理')
            for source, destination in candidates:
                try:
                    changed = self._ensure_managed_link(torrent_hash, torrent_name, source, destination, library_root)
                    result["created" if changed else "unchanged"] += 1
                except Exception as error:
                    result["failed"] += 1
                    existing = self.database.managed_link_for_source(str(source))
                    if existing:
                        self.database.update_managed_link(existing["id"], "conflict", str(error))
        finally:
            if resume_after:
                client.resume(torrent_hash)
        return result

    def _source_path(self, torrent: Dict[str, Any], item: Dict[str, Any], settings: Dict[str, str], file_count: int) -> Path:
        file_name = str(item.get("name", ""))
        if PurePosixPath(file_name).is_absolute() or '..' in PurePosixPath(file_name).parts:
            raise ValueError('qB 返回不安全的文件路径')
        save_path = str(torrent.get("save_path", ""))
        remote = str(PurePosixPath(save_path) / PurePosixPath(file_name))
        source = map_qb_path(remote, settings.get("qb_remote_prefix", ""), settings.get("qb_local_prefix", ""))
        return source

    def _ensure_managed_link(
        self, torrent_hash: str, torrent_name: str, source: Path, destination: Path, library_root: Path,
    ) -> bool:
        self._check_parent(source)
        self._check_parent(destination)
        if not self.authorize_path(source) or not self.authorize_path(destination):
            raise ValueError("源文件或媒体库目录未在 fnOS 中授权")
        if library_root.resolve(strict=False) not in destination.resolve(strict=False).parents:
            raise ValueError("整理目标超出媒体库目录")

        record = self.database.managed_link_for_source(str(source))
        if record:
            if record['library_path'] != str(destination) or record['torrent_hash'] != torrent_hash or record['status'] == 'removed':
                raise ValueError('路径已有其他整理记录，需要人工处理')
            identity = json.loads(record['identity_json'])
            if not identity:
                raise ValueError('旧台账缺少文件身份信息，停止操作')
            if destination.is_file() and not destination.is_symlink() and self._identity(destination) == identity:
                if symlink_points_to(source, destination):
                    self.database.update_managed_link(record['id'], 'active')
                    return False
                if not os.path.lexists(source):
                    source.symlink_to(os.path.relpath(destination, source.parent))
                    self.database.update_managed_link(record['id'], 'active')
                    return True
                if source.is_symlink() or self._identity(source) != identity:
                    raise ValueError('下载路径被其他文件占用，停止恢复')
                # Recover an interrupted no-copy transfer with both hard-link names present.
                source.unlink()
                source.symlink_to(os.path.relpath(destination, source.parent))
                self.database.update_managed_link(record['id'], 'active')
                return True
        if os.path.lexists(destination):
            raise ValueError('媒体库目标已存在，未覆盖任何文件')
        if source.is_symlink() or not source.is_file():
            raise ValueError('源路径不是普通文件；不接管已有软链接')
        if library_root.resolve() in source.resolve().parents:
            raise ValueError('源文件已在媒体库内，停止重复整理')
        identity = self._identity(source)
        if record and json.loads(record['identity_json']) != identity:
            raise ValueError('源文件身份已改变，停止操作')
        if source.stat().st_dev != library_root.stat().st_dev:
            raise ValueError('跨文件系统不能无复制迁移')
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._check_parent(destination)
        if not record:
            record = self.database.begin_managed_link(torrent_hash, torrent_name, str(source), str(destination), identity)
        # link() fails if a target appears concurrently; it never overwrites and copies no bytes.
        os.link(source, destination, follow_symlinks=False)
        if self._identity(source) != identity or self._identity(destination) != identity:
            raise ValueError('迁移期间文件发生变化，保留两端等待处理')
        source.unlink()
        try:
            source.symlink_to(os.path.relpath(destination, source.parent))
        except OSError:
            # Exclusive rollback: do not overwrite a newly occupied download path.
            if not os.path.lexists(source):
                os.link(destination, source, follow_symlinks=False)
                destination.unlink()
            raise
        self.database.update_managed_link(record["id"], "active")
        return True

    def cleanup_removed(self, active_hashes: Set[str]) -> Dict[str, int]:
        result = {"removed": 0, "missing": 0, "conflict": 0}
        for link in self.database.managed_links():
            if link["status"] != "active" or link["torrent_hash"].lower() in active_hashes:
                continue
            source = Path(link["source_path"])
            destination = Path(link["library_path"])
            self._check_parent(source)
            self._check_parent(destination)
            identity = json.loads(link['identity_json'])
            if not identity or destination.is_symlink() or not destination.is_file() or self._identity(destination) != identity:
                self.database.update_managed_link(link['id'], 'conflict', '真实文件身份改变，保留链接')
                result['conflict'] += 1
                continue
            if not self.authorize_path(source) or not self.authorize_path(destination):
                self.database.update_managed_link(link["id"], "conflict", "路径不再位于 fnOS 授权目录，未删除")
                result["conflict"] += 1
                continue
            if symlink_points_to(source, destination):
                source.unlink()
                self.database.update_managed_link(link["id"], "removed", "qB 任务已移除，软链接已清理")
                result["removed"] += 1
            elif not os.path.lexists(str(source)):
                self.database.update_managed_link(link["id"], "removed", "qB 任务及软链接均已不存在")
                result["missing"] += 1
            else:
                self.database.update_managed_link(link["id"], "conflict", "qB 路径不再是指向媒体库文件的软链接，未删除")
                result["conflict"] += 1
        return result


class QBIntegration:
    def __init__(self, database: LibraryDatabase, authorize_path: Callable[[Path], bool]):
        self.database = database
        self.organizer = MediaOrganizer(database, authorize_path)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.missing_since = {}

    def client(self, settings: Dict[str, str]) -> QBittorrentClient:
        return QBittorrentClient(settings["qb_url"], settings.get("qb_username", ""), settings.get("qb_password", ""))

    def test_connection(self, settings: Dict[str, str]) -> Dict[str, Any]:
        client = self.client(settings)
        version = client.login()
        torrents = client.torrents()
        return {"ok": True, "version": version, "torrents": len(torrents)}

    def sync_once(self) -> Dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            raise ValueError("qB 同步任务正在运行")
        try:
            settings = self.database.settings()
            if settings.get("qb_enabled") != "true":
                raise ValueError("请先启用 qBittorrent 集成")
            client = self.client(settings)
            version = client.login()
            torrents = client.torrents()
            active_hashes = {str(item.get("hash", "")).lower() for item in torrents if item.get("hash")}
            tracked = {row['torrent_hash'] for row in self.database.managed_links('active')}
            now = time.monotonic()
            missing = tracked - active_hashes
            self.missing_since = {key: self.missing_since.get(key, now) for key in missing}
            confirmed = {key for key in missing if now - self.missing_since[key] >= 30}
            cleanup = self.organizer.cleanup_removed(active_hashes | (tracked - confirmed)) if settings.get("qb_auto_cleanup") == "true" else {"removed": 0, "missing": 0, "conflict": 0}
            organized = {"created": 0, "unchanged": 0, "failed": 0, "torrents": 0}
            category = settings.get("qb_category", "meizang")
            for torrent in torrents:
                if str(torrent.get("category", "")) != category or torrent.get('progress', 0) < 1 or torrent.get('amount_left') != 0 or str(torrent.get('state', '')).lower() not in ('uploading', 'stalledup', 'queuedup', 'forcedup', 'pausedup', 'stoppedup'):
                    continue
                organized["torrents"] += 1
                outcome = self.organizer.organize_torrent(client, torrent, settings)
                for key in ("created", "unchanged", "failed"):
                    organized[key] += outcome[key]
            if organized['created']:
                root = self.database.add_root(settings['qb_library_root'], 'qB 整理媒体库')
                scan_root(self.database, root['id'])
            result = {"version": version, "active_torrents": len(active_hashes), "organized": organized, "cleanup": cleanup}
            self.database.update_settings({
                "qb_last_sync_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "qb_last_sync_status": "ok" if organized["failed"] == 0 and cleanup["conflict"] == 0 else "partial",
                "qb_last_sync_message": json_summary(result),
            })
            return result
        except Exception as error:
            self.missing_since.clear()
            self.database.update_settings({
                "qb_last_sync_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "qb_last_sync_status": "failed",
                "qb_last_sync_message": str(error),
            })
            raise
        finally:
            self.lock.release()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._worker, name="meizang-qb-sync", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _worker(self) -> None:
        next_run = 0.0
        while not self.stop_event.wait(5):
            settings = self.database.settings()
            if settings.get("qb_enabled") != "true":
                next_run = 0.0
                continue
            now = time.monotonic()
            if now < next_run:
                continue
            try:
                self.sync_once()
            except Exception:
                pass
            interval = max(30, min(int(settings.get("qb_poll_seconds", "60")), 3600))
            next_run = now + interval


def json_summary(result: Dict[str, Any]) -> str:
    organized = result["organized"]
    cleanup = result["cleanup"]
    return "整理 {}，保持 {}，失败 {}；清理软链接 {}，冲突 {}".format(
        organized["created"], organized["unchanged"], organized["failed"], cleanup["removed"], cleanup["conflict"],
    )
