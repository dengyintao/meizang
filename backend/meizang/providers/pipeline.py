from pathlib import Path
from typing import Any, Dict, Iterable, List

from .base import Metadata, MetadataProvider, merge_metadata
from .local import FFprobeProvider, FilenameProvider, NfoProvider
from .tmdb import TMDBProvider


class ProviderPipeline:
    def __init__(self, providers: Iterable[MetadataProvider]):
        self.providers = list(providers)

    @classmethod
    def from_settings(cls, settings: Dict[str, str]):
        providers: List[MetadataProvider] = [FilenameProvider(), FFprobeProvider(), NfoProvider()]
        if settings.get("tmdb_enabled") == "true" and settings.get("tmdb_token"):
            proxy_url = settings.get("proxy_url", "") if settings.get("proxy_enabled") == "true" else ""
            providers.append(TMDBProvider(
                settings["tmdb_token"], settings.get("tmdb_language", "zh-CN"), proxy_url=proxy_url,
            ))
        return cls(providers)

    def extract(self, path: Path, media_type: str) -> Metadata:
        metadata: Metadata = {"sources": [], "provider_errors": {}}
        for provider in self.providers:
            if not provider.supports(path, media_type):
                continue
            try:
                result = provider.fetch(path, media_type, metadata)
                if result:
                    metadata = merge_metadata(metadata, result, provider.name)
            except Exception as error:
                metadata.setdefault("provider_errors", {})[provider.name] = str(error)
        metadata.setdefault("title", path.stem)
        metadata.setdefault("year", None)
        metadata.setdefault("duration", None)
        metadata.setdefault("width", None)
        metadata.setdefault("height", None)
        metadata.setdefault("codec", "")
        metadata["provider"] = (metadata.get("sources") or ["filename"])[-1]
        return metadata
