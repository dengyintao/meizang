import json
from pathlib import Path
from typing import Any, Dict


Metadata = Dict[str, Any]


class MetadataProvider:
    name = "base"

    def supports(self, path: Path, media_type: str) -> bool:
        return True

    def fetch(self, path: Path, media_type: str, current: Metadata) -> Metadata:
        raise NotImplementedError


def has_value(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def merge_metadata(current: Metadata, incoming: Metadata, provider_name: str) -> Metadata:
    merged = dict(current)
    for key, value in incoming.items():
        if not has_value(value):
            continue
        if isinstance(value, list):
            existing = merged.get(key, []) if isinstance(merged.get(key), list) else []
            unique = []
            seen = set()
            for item in [*existing, *value]:
                marker = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
                if marker not in seen:
                    seen.add(marker)
                    unique.append(item)
            merged[key] = unique
        elif isinstance(value, dict):
            existing = merged.get(key, {}) if isinstance(merged.get(key), dict) else {}
            merged[key] = {**existing, **value}
        else:
            merged[key] = value
    sources = list(merged.get("sources", []))
    if provider_name not in sources:
        sources.append(provider_name)
    merged["sources"] = sources
    return merged
