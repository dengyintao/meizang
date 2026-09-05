import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict


YEAR_PATTERN = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)")


def filename_metadata(path: Path) -> Dict[str, Any]:
    """Fallback provider inspired by Movie_Data_Capture's filename parser."""
    stem = path.stem
    cleaned = re.sub(r"[._]+", " ", stem)
    cleaned = re.sub(r"\[[^]]*]", " ", cleaned)
    cleaned = re.sub(r"\b(?:2160p|1080p|720p|4k|x26[45]|hevc|bluray|web[- ]?dl)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")
    match = YEAR_PATTERN.search(stem)
    return {"title": cleaned or stem, "year": int(match.group(1)) if match else None, "provider": "filename"}


def ffprobe_metadata(path: Path) -> Dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        return {}
    command = [
        executable, "-v", "error", "-show_entries",
        "format=duration,bit_rate:format_tags=title,date,creation_time:stream=index,codec_type,codec_name,width,height",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True)
        return json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def extract_metadata(path: Path, media_type: str) -> Dict[str, Any]:
    fallback = filename_metadata(path)
    result: Dict[str, Any] = {
        "title": fallback["title"], "year": fallback["year"],
        "duration": None, "width": None, "height": None, "codec": "",
        "raw": {"filename": fallback},
    }
    if media_type not in ("video", "audio"):
        return result

    probe = ffprobe_metadata(path)
    if not probe:
        return result
    result["raw"]["ffprobe"] = probe
    format_info = probe.get("format", {})
    tags = format_info.get("tags", {}) or {}
    if tags.get("title"):
        result["title"] = tags["title"]
    try:
        result["duration"] = float(format_info.get("duration"))
    except (TypeError, ValueError):
        pass
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            result["width"] = stream.get("width")
            result["height"] = stream.get("height")
            result["codec"] = stream.get("codec_name", "")
            break
        if stream.get("codec_type") == "audio" and not result["codec"]:
            result["codec"] = stream.get("codec_name", "")
    return result

