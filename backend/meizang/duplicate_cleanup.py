import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .database import LibraryDatabase
from .scanner import sha256_file


CONFIRMATION = "DELETE_DUPLICATES"
FORCE_CONFIRMATION = "FORCE_DELETE_DUPLICATES"


def delete_duplicate_file(
    database: LibraryDatabase,
    authorize_path: Callable[[Path], bool],
    raw_path: str,
    sha256: str,
    confirmation: str,
    force_protected: bool = False,
) -> Dict[str, Any]:
    required_confirmation = FORCE_CONFIRMATION if force_protected else CONFIRMATION
    if confirmation != required_confirmation:
        raise ValueError("缺少重复文件删除确认")

    group = next((item for item in database.duplicates() if item["sha256"] == sha256), None)
    if not group:
        raise ValueError("该重复组已经变化，请刷新后重试")
    item = next((file for file in group["files"] if file["path"] == raw_path), None)
    if not item or len(group["files"]) < 2:
        raise ValueError("该文件已不属于重复组，请刷新后重试")
    if item["protected"] and not force_protected:
        raise OSError("该文件受 qB 任务保护；如确定不再需要做种，请使用强制删除")

    path = Path(raw_path)
    if not authorize_path(path):
        raise OSError("路径不在当前 fnOS 授权范围内")
    if path.is_symlink() or not path.is_file():
        raise OSError("文件不存在或不是普通文件")
    stat = path.stat()
    if stat.st_size != group["size"] or stat.st_mtime_ns != item["mtime_ns"]:
        raise OSError("文件在扫描后发生变化，请重新扫描后再删除")
    if not os.access(str(path.parent), os.W_OK):
        raise OSError("应用账户没有该目录的删除权限")
    if sha256_file(path) != group["sha256"]:
        raise OSError("文件内容在扫描后发生变化，请重新扫描后再删除")

    try:
        path.unlink()
    except PermissionError as error:
        raise OSError("系统拒绝删除，请在飞牛中为媒藏授予该目录的读写权限") from error
    database.delete_assets_by_path(str(path))
    return {"deleted": 1, "released_bytes": group["size"], "path": str(path)}


def delete_duplicates(
    database: LibraryDatabase,
    authorize_path: Callable[[Path], bool],
    expected_groups: int,
    confirmation: str,
    on_progress: Optional[Callable[[Dict[str, int]], None]] = None,
    force_protected: bool = False,
) -> Dict[str, Any]:
    required_confirmation = FORCE_CONFIRMATION if force_protected else CONFIRMATION
    if confirmation != required_confirmation:
        raise ValueError("缺少重复文件删除确认")

    groups = database.duplicates()
    if expected_groups != len(groups):
        raise ValueError("重复文件列表已经变化，请刷新后重试")

    protected = database.protected_media_paths()
    result = {
        "groups": len(groups),
        "deleted": 0,
        "released_bytes": 0,
        "skipped": 0,
        "errors": [],
    }

    for index, group in enumerate(groups, 1):
        files = group["files"]
        keepers = [] if force_protected else [item for item in files if item["path"] in protected]
        keeper = keepers[0] if keepers else min(
            files, key=lambda item: (len(item["path"]), item["path"])
        )
        keeper_path = Path(keeper["path"])
        try:
            if (
                not authorize_path(keeper_path)
                or keeper_path.is_symlink()
                or not keeper_path.is_file()
                or keeper_path.stat().st_size != group["size"]
                or sha256_file(keeper_path) != group["sha256"]
            ):
                raise OSError("保留文件不存在、已变化或未授权")
        except OSError as error:
            result["skipped"] += max(0, len(files) - 1)
            result["errors"].append({"path": str(keeper_path), "reason": str(error)})
            if on_progress:
                on_progress({
                    "processed_groups": index,
                    "total_groups": len(groups),
                    "deleted": result["deleted"],
                    "skipped": result["skipped"],
                })
            continue

        for item in files:
            path = Path(item["path"])
            if item["path"] == keeper["path"] or (not force_protected and item["path"] in protected):
                continue
            try:
                if not authorize_path(path):
                    raise OSError("路径不在当前 fnOS 授权范围内")
                if path.is_symlink() or not path.is_file():
                    raise OSError("文件不存在或不是普通文件")
                stat = path.stat()
                if stat.st_size != group["size"] or stat.st_mtime_ns != item["mtime_ns"]:
                    raise OSError("文件在扫描后发生变化")
                if not os.access(str(path.parent), os.W_OK):
                    raise OSError("目录没有删除权限")
                if sha256_file(path) != group["sha256"]:
                    raise OSError("文件内容在扫描后发生变化")
                path.unlink()
                database.delete_assets_by_path(str(path))
                result["deleted"] += 1
                result["released_bytes"] += group["size"]
            except OSError as error:
                result["skipped"] += 1
                result["errors"].append({"path": str(path), "reason": str(error)})

        if on_progress:
            on_progress({
                "processed_groups": index,
                "total_groups": len(groups),
                "deleted": result["deleted"],
                "skipped": result["skipped"],
            })

    return result
