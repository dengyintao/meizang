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

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS managed_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    torrent_hash TEXT NOT NULL,
    torrent_name TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL UNIQUE,
    library_path TEXT NOT NULL UNIQUE,
    identity_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'planning',
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_managed_links_hash ON managed_links(torrent_hash);
CREATE INDEX IF NOT EXISTS idx_managed_links_status ON managed_links(status);

CREATE TABLE IF NOT EXISTS mdc_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    log_text TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS mdc_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    next_run_at REAL NOT NULL,
    last_run_at REAL
);
"""

DEFAULT_SETTINGS = {
    "tmdb_enabled": "false",
    "tmdb_token": "",
    "tmdb_language": "zh-CN",
    "proxy_enabled": "false",
    "proxy_url": "",
    "qb_enabled": "false",
    "qb_url": "http://127.0.0.1:8080",
    "qb_username": "admin",
    "qb_password": "",
    "qb_category": "meizang",
    "qb_library_root": "",
    "qb_remote_prefix": "",
    "qb_local_prefix": "",
    "qb_auto_cleanup": "true",
    "qb_poll_seconds": "60",
    "qb_last_sync_at": "",
    "qb_last_sync_status": "never",
    "qb_last_sync_message": "",
    "mdc_config_ini": "",
}


class LibraryDatabase:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(managed_links)')}
            if 'identity_json' not in columns:
                connection.execute("ALTER TABLE managed_links ADD COLUMN identity_json TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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

    def settings(self) -> Dict[str, str]:
        values = dict(DEFAULT_SETTINGS)
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
        values.update({row["key"]: row["value"] for row in rows})
        return values

    def update_settings(self, values: Dict[str, str]) -> Dict[str, str]:
        allowed = set(DEFAULT_SETTINGS)
        with self.connect() as connection:
            for key, value in values.items():
                if key not in allowed:
                    continue
                connection.execute(
                    "INSERT INTO app_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                    (key, str(value)),
                )
        return self.settings()

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
                "WHERE sha256 <> '' GROUP BY size, sha256 HAVING COUNT(DISTINCT path) > 1)"
            ).fetchone()[0]
        return {
            "total": totals["total"],
            "bytes": totals["bytes"],
            "types": {name: counts.get(name, 0) for name in ("image", "video", "audio", "other")},
            "duplicate_groups": duplicate_groups,
        }

    def duplicates(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            protected = {
                row["library_path"] for row in connection.execute(
                    "SELECT library_path FROM managed_links WHERE status <> 'removed'"
                )
            }
            groups = connection.execute(
                "SELECT size, sha256, COUNT(DISTINCT path) AS count FROM media_assets "
                "WHERE sha256 <> '' GROUP BY size, sha256 HAVING COUNT(DISTINCT path) > 1 "
                "ORDER BY size * COUNT(DISTINCT path) DESC"
            ).fetchall()
            result = []
            for group in groups:
                files = connection.execute(
                    "SELECT MIN(id) AS id, path, MIN(filename) AS filename, "
                    "MIN(media_type) AS media_type, size, MIN(mtime_ns) AS mtime_ns "
                    "FROM media_assets WHERE size=? AND sha256=? GROUP BY path ORDER BY path",
                    (group["size"], group["sha256"]),
                ).fetchall()
                file_items = []
                for row in files:
                    item = dict(row)
                    item["protected"] = item["path"] in protected
                    file_items.append(item)
                protected_count = sum(1 for item in file_items if item["protected"])
                removable_count = group["count"] - protected_count if protected_count else group["count"] - 1
                result.append({
                    "size": group["size"],
                    "sha256": group["sha256"],
                    "count": group["count"],
                    "removable_count": max(0, removable_count),
                    "reclaimable_bytes": group["size"] * max(0, removable_count),
                    "files": file_items,
                })
            return result

    def protected_media_paths(self) -> set:
        """Paths managed by qB must never be removed by duplicate cleanup."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT library_path FROM managed_links WHERE status <> 'removed'"
            ).fetchall()
        return {row["library_path"] for row in rows}

    def delete_assets_by_path(self, path: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM media_assets WHERE path=?", (path,))

    @staticmethod
    def _mdc_job(row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        item["cancel_requested"] = bool(item["cancel_requested"])
        return item

    def create_mdc_job(self, job_id: str, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO mdc_jobs(id,kind,payload_json) VALUES(?,?,?)",
                (job_id, kind, json.dumps(payload, ensure_ascii=False)),
            )
            return self._mdc_job(connection.execute("SELECT * FROM mdc_jobs WHERE id=?", (job_id,)).fetchone())

    def mdc_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            return self._mdc_job(connection.execute("SELECT * FROM mdc_jobs WHERE id=?", (job_id,)).fetchone())

    def mdc_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM mdc_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._mdc_job(row) for row in rows]

    def update_mdc_job(self, job_id: str, **values) -> None:
        allowed = {"status", "progress", "total", "message", "error", "log_text", "cancel_requested", "started_at", "finished_at"}
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return
        assignments = ",".join("{}=?".format(key) for key in values)
        with self.connect() as connection:
            connection.execute("UPDATE mdc_jobs SET {} WHERE id=?".format(assignments), (*values.values(), job_id))

    def append_mdc_log(self, job_id: str, line: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE mdc_jobs SET log_text=substr(log_text || ?, -200000), message=? WHERE id=?",
                (line.rstrip() + "\n", line.strip()[-500:], job_id),
            )

    def cancel_mdc_job(self, job_id: str) -> None:
        self.update_mdc_job(job_id, cancel_requested=1, message="正在取消")

    def recover_mdc_jobs(self) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE mdc_jobs SET status='failed', error='服务重启导致任务中止', finished_at=CURRENT_TIMESTAMP "
                "WHERE status IN ('queued','running')"
            )

    def create_mdc_schedule(self, name: str, interval_seconds: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        import time
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO mdc_schedules(name,interval_seconds,payload_json,next_run_at) VALUES(?,?,?,?)",
                (name, interval_seconds, json.dumps(payload, ensure_ascii=False), time.time() + interval_seconds),
            )
            row = connection.execute("SELECT * FROM mdc_schedules WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._mdc_schedule(row)

    @staticmethod
    def _mdc_schedule(row) -> Dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
        return item

    def mdc_schedules(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            return [self._mdc_schedule(row) for row in connection.execute("SELECT * FROM mdc_schedules ORDER BY id")]

    def update_mdc_schedule(self, schedule_id: int, values: Dict[str, Any]) -> None:
        allowed = {"name", "enabled", "interval_seconds"}
        updates = {key: values[key] for key in allowed if key in values}
        if "payload" in values:
            updates["payload_json"] = json.dumps(values["payload"], ensure_ascii=False)
        if not updates:
            return
        assignments = ",".join("{}=?".format(key) for key in updates)
        with self.connect() as connection:
            connection.execute("UPDATE mdc_schedules SET {} WHERE id=?".format(assignments), (*updates.values(), schedule_id))

    def delete_mdc_schedule(self, schedule_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM mdc_schedules WHERE id=?", (schedule_id,))

    def due_mdc_schedules(self, now: float) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM mdc_schedules WHERE enabled=1 AND next_run_at<=?", (now,)).fetchall()
            return [self._mdc_schedule(row) for row in rows]

    def advance_mdc_schedule(self, schedule_id: int, now: float, interval: int) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE mdc_schedules SET last_run_at=?,next_run_at=? WHERE id=?", (now, now + interval, schedule_id))

    def managed_links(self, status: str = "") -> List[Dict[str, Any]]:
        where = " WHERE status=?" if status else ""
        values = (status,) if status else ()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM managed_links{} ORDER BY id DESC".format(where), values
            ).fetchall()
            return [dict(row) for row in rows]

    def managed_link_for_source(self, source_path: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM managed_links WHERE source_path=?", (source_path,)
            ).fetchone()
            return dict(row) if row else None

    def begin_managed_link(self, torrent_hash: str, torrent_name: str, source_path: str, library_path: str, identity=None) -> Dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO managed_links(torrent_hash,torrent_name,source_path,library_path,identity_json) VALUES(?,?,?,?,?)",
                (torrent_hash.lower(), torrent_name, source_path, library_path, json.dumps(identity or {})),
            )
            row = connection.execute(
                "SELECT * FROM managed_links WHERE source_path=?", (source_path,)
            ).fetchone()
            return dict(row)

    def update_managed_link(self, link_id: int, status: str, message: str = "") -> None:
        removed = "CURRENT_TIMESTAMP" if status == "removed" else "NULL"
        with self.connect() as connection:
            connection.execute(
                "UPDATE managed_links SET status=?,message=?,updated_at=CURRENT_TIMESTAMP,removed_at={} WHERE id=?".format(removed),
                (status, message, link_id),
            )

    def managed_link_stats(self) -> Dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM managed_links GROUP BY status"
            ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        return {
            "active": counts.get("active", 0),
            "conflict": counts.get("conflict", 0),
            "removed": counts.get("removed", 0),
            "total": sum(counts.values()),
        }
