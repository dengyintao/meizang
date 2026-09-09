import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

from .base import Metadata, MetadataProvider


YEAR_PATTERN = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)")


def media_tool_env() -> Dict[str, str]:
    env = os.environ.copy()
    library_paths = []
    if env.get("LD_LIBRARY_PATH"):
        library_paths.append(env["LD_LIBRARY_PATH"])
    library_paths.append("/usr/lib/x86_64-linux-gnu")
    env["LD_LIBRARY_PATH"] = ":".join(library_paths)
    return env


class FilenameProvider(MetadataProvider):
    name = "filename"

    def fetch(self, path: Path, media_type: str, current: Metadata) -> Metadata:
        stem = path.stem
        cleaned = re.sub(r"[._]+", " ", stem)
        cleaned = re.sub(r"\[[^]]*]", " ", cleaned)
        cleaned = re.sub(
            r"\b(?:2160p|1080p|720p|4k|x26[45]|hevc|bluray|web[- ]?dl|remux|hdr|dv)\b",
            " ", cleaned, flags=re.I,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")
        match = YEAR_PATTERN.search(stem)
        return {
            "title": cleaned or stem,
            "search_title": cleaned or stem,
            "year": int(match.group(1)) if match else None,
        }


class FFprobeProvider(MetadataProvider):
    name = "ffprobe"

    def supports(self, path: Path, media_type: str) -> bool:
        return media_type in ("video", "audio") and bool(shutil.which("ffprobe"))

    def fetch(self, path: Path, media_type: str, current: Metadata) -> Metadata:
        command = [
            shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
            "format=duration,bit_rate:format_tags=title,date,creation_time:stream=index,codec_type,codec_name,width,height,channels,sample_rate",
            "-of", "json", str(path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True, env=media_tool_env())
        probe = json.loads(completed.stdout or "{}")
        result: Metadata = {"technical": probe}
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
                result.update(width=stream.get("width"), height=stream.get("height"), codec=stream.get("codec_name", ""))
                break
            if stream.get("codec_type") == "audio" and not result.get("codec"):
                result["codec"] = stream.get("codec_name", "")
        return result


def _texts(root: ET.Element, tag: str) -> List[str]:
    return [str(node.text).strip() for node in root.findall(tag) if node.text and node.text.strip()]


class NfoProvider(MetadataProvider):
    name = "nfo"

    def supports(self, path: Path, media_type: str) -> bool:
        return media_type == "video" and path.with_suffix(".nfo").is_file()

    def fetch(self, path: Path, media_type: str, current: Metadata) -> Metadata:
        nfo_path = path.with_suffix(".nfo")
        root = ET.parse(str(nfo_path)).getroot()
        value = lambda name: (root.findtext(name) or "").strip()
        year_text = value("year") or (value("premiered")[:4] if value("premiered") else "")
        cast = []
        for actor in root.findall("actor"):
            name = (actor.findtext("name") or "").strip()
            if name:
                cast.append({"name": name, "character": (actor.findtext("role") or "").strip(), "profile_url": (actor.findtext("thumb") or "").strip()})
        unique_ids = {
            node.attrib.get("type", "unknown"): (node.text or "").strip()
            for node in root.findall("uniqueid") if (node.text or "").strip()
        }
        result: Metadata = {
            "title": value("title"),
            "original_title": value("originaltitle"),
            "year": int(year_text) if year_text.isdigit() else None,
            "release_date": value("premiered") or value("releasedate") or value("release"),
            "plot": value("plot") or value("outline"),
            "runtime": int(value("runtime")) if value("runtime").isdigit() else None,
            "director": value("director"),
            "studio": value("studio") or value("maker"),
            "collection": value("set"),
            "rating": float(value("rating")) if re.fullmatch(r"\d+(?:\.\d+)?", value("rating")) else None,
            "genres": _texts(root, "genre"),
            "tags": _texts(root, "tag"),
            "cast": cast,
            "external_ids": unique_ids,
            "poster_url": value("poster") or value("thumb"),
            "backdrop_url": value("fanart"),
            "trailer_url": value("trailer"),
            "provider_id": unique_ids.get("tmdb") or unique_ids.get("imdb") or value("id"),
            "nfo_path": str(nfo_path),
        }
        return result
