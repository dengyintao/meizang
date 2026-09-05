import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS library_roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_scan_at TEXT,
    last_scan_status TEXT NOT NULL DEFAULT 'never',
    last_scan_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL REFERENCES library_roots(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    year INTEGER,
    duration REAL,
    width INTEGER,
    height INTEGER,
    codec TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    scan_token TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(root_id, path)
);

CREATE INDEX IF NOT EXISTS idx_assets_type ON media_assets(media_type);
CREATE INDEX IF NOT EXISTS idx_assets_hash ON media_assets(size, sha256);
CREATE INDEX IF NOT EXISTS idx_assets_root ON media_assets(root_id);
"""


class LibraryDatabase:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def add_root(self, path: str, label: str = "") -> Dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO library_roots(path, label) VALUES(?, ?) "
                "ON CONFLICT(path) DO UPDATE SET label=excluded.label",
                (path, label),
            )
            row = connection.execute(
                "SELECT * FROM library_roots WHERE path = ?", (path,)
            ).fetchone()
            return dict(row)

    def roots(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT r.*, COUNT(a.id) AS asset_count "
                "FROM library_roots r LEFT JOIN media_assets a ON a.root_id=r.id "
                "GROUP BY r.id ORDER BY r.id"
            ).fetchall()
            return [dict(row) for row in rows]

    def root(self, root_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM library_roots WHERE id = ?", (root_id,)
            ).fetchone()
            return dict(row) if row else None

    def assets(self, media_type: str = "", query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        clauses, values = [], []
        if media_type and media_type != "all":
            clauses.append("media_type = ?")
            values.append(media_type)
        if query:
            clauses.append("(filename LIKE ? OR title LIKE ? OR relative_path LIKE ?)")
            pattern = "%{}%".format(query)
            values.extend([pattern, pattern, pattern])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(limit, 1000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, root_id, path, relative_path, filename, extension, "
                "media_type, size, mtime_ns, sha256, title, year, duration, "
                "width, height, codec, metadata_json, created_at, updated_at "
                "FROM media_assets{} ORDER BY updated_at DESC, id DESC LIMIT ?".format(where),
                values,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json"))
            except (TypeError, json.JSONDecodeError):
                item["metadata"] = {}
                item.pop("metadata_json", None)
            result.append(item)
        return result

    def stats(self) -> Dict[str, Any]:
        with self.connect() as connection:
            counts = {
                row["media_type"]: row["count"]
                for row in connection.execute(
                    "SELECT media_type, COUNT(*) AS count FROM media_assets GROUP BY media_type"
                )
            }
            totals = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(size), 0) AS bytes FROM media_assets"
            ).fetchone()
            duplicate_groups = connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM media_assets "
                "WHERE sha256 <> '' GROUP BY size, sha256 HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        return {
            "total": totals["total"],
            "bytes": totals["bytes"],
            "types": {name: counts.get(name, 0) for name in ("image", "video", "audio", "other")},
            "duplicate_groups": duplicate_groups,
        }

    def duplicates(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            groups = connection.execute(
                "SELECT size, sha256, COUNT(*) AS count FROM media_assets "
                "WHERE sha256 <> '' GROUP BY size, sha256 HAVING COUNT(*) > 1 "
                "ORDER BY size * COUNT(*) DESC"
            ).fetchall()
            result = []
            for group in groups:
                files = connection.execute(
                    "SELECT id, path, filename, media_type, size FROM media_assets "
                    "WHERE size=? AND sha256=? ORDER BY path",
                    (group["size"], group["sha256"]),
                ).fetchall()
                result.append({
                    "size": group["size"],
                    "sha256": group["sha256"],
                    "count": group["count"],
                    "reclaimable_bytes": group["size"] * (group["count"] - 1),
                    "files": [dict(row) for row in files],
                })
            return result

