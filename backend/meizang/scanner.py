import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, Optional

from .database import LibraryDatabase
from .metadata import extract_metadata


EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".raw", ".cr2", ".nef", ".arw"},
    "video": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".m2ts", ".webm", ".flv", ".iso"},
    "audio": {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape", ".wma"},
}


def media_type(path: Path) -> str:
    extension = path.suffix.lower()
    for kind, extensions in EXTENSIONS.items():
        if extension in extensions:
            return kind
    return "other"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_root(database: LibraryDatabase, root_id: int) -> Dict[str, int]:
    root = database.root(root_id)
    if not root:
        raise ValueError("媒体目录不存在")
    root_path = Path(root["path"])
    if not root_path.is_dir() or not os.access(str(root_path), os.R_OK):
        with database.connect() as connection:
            connection.execute(
                "UPDATE library_roots SET last_scan_at=CURRENT_TIMESTAMP, "
                "last_scan_status='failed', last_scan_message=? WHERE id=?",
                ("目录不存在或没有读取权限", root_id),
            )
        raise ValueError("目录不存在或没有读取权限，索引未发生改变")

    token = uuid.uuid4().hex
    counters = {"inserted": 0, "updated": 0, "unchanged": 0, "removed": 0, "failed": 0}
    traversal_complete = True

    with database.connect() as connection:
        existing = {
            row["path"]: row
            for row in connection.execute(
                "SELECT id, path, size, mtime_ns FROM media_assets WHERE root_id=?", (root_id,)
            )
        }

        def on_error(_error):
            nonlocal traversal_complete
            traversal_complete = False
            counters["failed"] += 1

        for current, directories, files in os.walk(str(root_path), onerror=on_error, followlinks=False):
            directories[:] = [name for name in directories if not name.startswith(".@")]
            for filename in files:
                path = Path(current) / filename
                if path.is_symlink():
                    continue
                kind = media_type(path)
                if kind == "other":
                    continue
                try:
                    stat = path.stat()
                    old = existing.get(str(path))
                    if old and old["size"] == stat.st_size and old["mtime_ns"] == stat.st_mtime_ns:
                        connection.execute("UPDATE media_assets SET scan_token=? WHERE id=?", (token, old["id"]))
                        counters["unchanged"] += 1
                        continue
                    metadata = extract_metadata(path, kind)
                    digest = sha256_file(path)
                    relative = str(path.relative_to(root_path))
                    values = (
                        root_id, str(path), relative, filename, path.suffix.lower(), kind,
                        stat.st_size, stat.st_mtime_ns, digest, metadata["title"], metadata["year"],
                        metadata["duration"], metadata["width"], metadata["height"], metadata["codec"],
                        json.dumps(metadata["raw"], ensure_ascii=False), token,
                    )
                    connection.execute(
                        "INSERT INTO media_assets(root_id,path,relative_path,filename,extension,media_type,size,mtime_ns,sha256,title,year,duration,width,height,codec,metadata_json,scan_token) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(root_id,path) DO UPDATE SET "
                        "relative_path=excluded.relative_path, filename=excluded.filename, extension=excluded.extension, "
                        "media_type=excluded.media_type, size=excluded.size, mtime_ns=excluded.mtime_ns, sha256=excluded.sha256, "
                        "title=excluded.title, year=excluded.year, duration=excluded.duration, width=excluded.width, "
                        "height=excluded.height, codec=excluded.codec, metadata_json=excluded.metadata_json, "
                        "scan_token=excluded.scan_token, updated_at=CURRENT_TIMESTAMP",
                        values,
                    )
                    counters["updated" if old else "inserted"] += 1
                except (OSError, ValueError):
                    traversal_complete = False
                    counters["failed"] += 1

        if traversal_complete:
            counters["removed"] = connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE root_id=? AND scan_token<>?", (root_id, token)
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM media_assets WHERE root_id=? AND scan_token<>?", (root_id, token)
            )
        message = "扫描完成" if traversal_complete else "部分目录不可访问，已保留可能离线的旧记录"
        connection.execute(
            "UPDATE library_roots SET last_scan_at=CURRENT_TIMESTAMP, last_scan_status=?, last_scan_message=? WHERE id=?",
            ("ok" if traversal_complete else "partial", message, root_id),
        )
    return counters
