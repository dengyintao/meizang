import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from .database import LibraryDatabase
from .scanner import scan_root


_request_lock = threading.Lock()
_last_request = 0.0


def safe_name(value: Any, fallback: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(value or "")).strip(" .")
    return text[:180] or fallback


class MusicBrainzClient:
    def __init__(self, proxy_url: str = ""):
        self.opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}) if proxy_url else ProxyHandler())

    def search(self, title: str, artist: str = "", album: str = "", duration: Optional[float] = None) -> Dict[str, Any]:
        global _last_request
        terms = ['recording:"{}"'.format(title.replace('"', ''))]
        if artist:
            terms.append('artist:"{}"'.format(artist.replace('"', '')))
        if album:
            terms.append('release:"{}"'.format(album.replace('"', '')))
        url = "https://musicbrainz.org/ws/2/recording/?fmt=json&limit=5&query=" + quote(" AND ".join(terms))
        with _request_lock:
            delay = 1.05 - (time.monotonic() - _last_request)
            if delay > 0:
                time.sleep(delay)
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "Meizang/0.9 (https://github.com/dengyintao/meizang)"})
            with self.opener.open(request, timeout=20) as response:
                payload = json.load(response)
            _last_request = time.monotonic()
        candidates = payload.get("recordings") or []
        if not candidates:
            raise ValueError("MusicBrainz 未找到匹配歌曲")
        recording = max(candidates, key=lambda item: int(item.get("score", 0)))
        score = int(recording.get("score", 0))
        if score < (75 if artist else 90):
            raise ValueError("MusicBrainz 匹配可信度不足（{}%）".format(score))
        credits = recording.get("artist-credit") or []
        artist_name = "".join(str(item.get("name", "")) + str(item.get("joinphrase", "")) for item in credits).strip()
        releases = recording.get("releases") or []
        release = releases[0] if releases else {}
        release_group = release.get("release-group") or {}
        date = release.get("date") or recording.get("first-release-date") or ""
        tags = sorted((recording.get("tags") or []), key=lambda item: int(item.get("count", 0)), reverse=True)
        return {
            "title": recording.get("title") or title,
            "artist": artist_name or artist,
            "album_artist": artist_name or artist,
            "album": release.get("title") or album or "未知专辑",
            "release_date": date,
            "year": int(date[:4]) if str(date)[:4].isdigit() else None,
            "genres": [item.get("name") for item in tags[:5] if item.get("name")],
            "musicbrainz_recording_id": recording.get("id", ""),
            "musicbrainz_release_id": release.get("id", ""),
            "musicbrainz_release_group_id": release_group.get("id", ""),
            "match_score": score,
            "provider": "musicbrainz",
            "poster_url": "https://coverartarchive.org/release/{}/front-500".format(release.get("id")) if release.get("id") else "",
        }

    def download_cover(self, release_id: str, destination: Path) -> bool:
        if not release_id or destination.exists():
            return False
        request = Request(
            "https://coverartarchive.org/release/{}/front-500".format(release_id),
            headers={"User-Agent": "Meizang/0.9 (https://github.com/dengyintao/meizang)"},
        )
        try:
            with self.opener.open(request, timeout=30) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            return True
        except OSError:
            destination.unlink(missing_ok=True)
            return False


def write_audio_tags(path: Path, metadata: Dict[str, Any]) -> None:
    try:
        from mutagen import File
    except ImportError as error:
        raise OSError("应用缺少音乐标签组件 mutagen") from error
    audio = File(str(path), easy=True)
    if audio is None:
        raise OSError("暂不支持写入该音频格式的标签")
    mapping = {"title": "title", "artist": "artist", "album_artist": "albumartist", "album": "album", "release_date": "date", "track": "tracknumber", "disc": "discnumber"}
    for source, target in mapping.items():
        if metadata.get(source):
            audio[target] = [str(metadata[source])]
    if metadata.get("genres"):
        audio["genre"] = [str(item) for item in metadata["genres"]]
    audio.save()


class MusicJobs:
    def __init__(self, database: LibraryDatabase, authorize_path: Callable[[Path], bool], normalize_directory: Callable[[str], Path]):
        self.database = database
        self.authorize_path = authorize_path
        self.normalize_directory = normalize_directory
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.root_jobs: Dict[int, str] = {}
        self.lock = threading.Lock()

    def start(self, root_id: int, options: Dict[str, Any]) -> Dict[str, Any]:
        root = self.database.root(root_id)
        if not root:
            raise ValueError("音乐目录不存在")
        if self.database.audio_asset_count(root_id) == 0:
            raise ValueError("该目录尚未扫描到音乐文件，请先完成扫描或选择包含音乐的目录")
        with self.lock:
            existing_id = self.root_jobs.get(root_id)
            if existing_id and self.jobs.get(existing_id, {}).get("status") == "running":
                return dict(self.jobs[existing_id])
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "root_id": root_id, "status": "running", "progress": 0, "total": 0, "matched": 0, "organized": 0, "failed": 0, "errors": [], "message": "正在读取音乐文件"}
        with self.lock:
            self.jobs[job_id] = job
            self.root_jobs[root_id] = job_id
        threading.Thread(target=self._run, args=(job_id, root, options), daemon=True).start()
        return dict(job)

    def _run(self, job_id: str, root: Dict[str, Any], options: Dict[str, Any]) -> None:
        try:
            self._execute(job_id, root, options)
        except Exception as error:
            self._update(job_id, status="failed", message="音乐任务失败", error=str(error))

    def _execute(self, job_id: str, root: Dict[str, Any], options: Dict[str, Any]) -> None:
        settings = self.database.settings()
        proxy = settings.get("proxy_url", "") if settings.get("proxy_enabled") == "true" else ""
        client = MusicBrainzClient(proxy)
        assets = self.database.audio_assets(root["id"])
        output_root = Path(root["path"])
        if options.get("move_files"):
            output_root = self.normalize_directory(str(options.get("library_root", "")))
        self._update(job_id, total=len(assets), message="开始匹配 MusicBrainz")
        for index, asset in enumerate(assets, 1):
            path = Path(asset["path"])
            try:
                if not self.authorize_path(path):
                    raise OSError("文件不在当前飞牛授权范围内")
                current = asset.get("metadata") or {}
                title = str(current.get("title") or path.stem)
                artist = str(current.get("artist") or "")
                album = str(current.get("album") or "")
                if not artist and " - " in path.stem:
                    artist, title = (part.strip() for part in path.stem.split(" - ", 1))
                metadata = {**current, **client.search(title, artist, album, asset.get("duration"))}
                sidecar = path.with_suffix(path.suffix + ".music.json")
                sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
                if options.get("write_tags"):
                    write_audio_tags(path, metadata)
                target = path
                if options.get("move_files"):
                    album_dir = output_root / safe_name(metadata.get("album_artist") or metadata.get("artist"), "未知艺术家") / safe_name(metadata.get("album"), "未知专辑")
                    album_dir.mkdir(parents=True, exist_ok=True)
                    track_text = str(metadata.get("track") or "").split("/", 1)[0]
                    prefix = "{:02d} - ".format(int(track_text)) if track_text.isdigit() else ""
                    target = album_dir / (prefix + safe_name(metadata.get("title"), path.stem) + path.suffix.lower())
                    if target != path:
                        if target.exists():
                            raise OSError("整理目标已存在：{}".format(target))
                        try:
                            os.replace(str(path), str(target))
                        except OSError as error:
                            raise OSError("无法移动文件，请确认来源与目标位于同一存储空间且均有写入权限") from error
                        if sidecar.exists():
                            os.replace(str(sidecar), str(target.with_suffix(target.suffix + ".music.json")))
                        self._increment(job_id, "organized")
                if options.get("download_cover"):
                    client.download_cover(metadata.get("musicbrainz_release_id", ""), target.parent / "cover.jpg")
                self._increment(job_id, "matched")
            except Exception as error:
                with self.lock:
                    self.jobs[job_id]["failed"] += 1
                    self.jobs[job_id]["errors"].append({"path": str(path), "reason": str(error)})
            self._update(job_id, progress=index, message="已处理 {}/{}".format(index, len(assets)))
        try:
            scan_root(self.database, root["id"], force_metadata=True)
            if output_root != Path(root["path"]):
                output = self.database.add_root(str(output_root), "音乐整理库")
                scan_root(self.database, output["id"], force_metadata=True)
        except Exception:
            pass
        job = self.get(job_id) or {}
        message = "音乐整理完成"
        if job.get("failed") and not job.get("matched"):
            message = "音乐任务完成，但所有文件均处理失败"
        self._update(job_id, status="completed", message=message)

    def _update(self, job_id: str, **values) -> None:
        with self.lock:
            self.jobs[job_id].update(values)

    def _increment(self, job_id: str, key: str) -> None:
        with self.lock:
            self.jobs[job_id][key] += 1

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return dict(self.jobs[job_id]) if job_id in self.jobs else None

    def latest(self) -> Optional[Dict[str, Any]]:
        with self.lock:
            if not self.jobs:
                return None
            return dict(next(reversed(self.jobs.values())))
