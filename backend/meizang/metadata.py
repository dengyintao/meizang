from pathlib import Path
from typing import Any, Dict, Optional

from .providers import ProviderPipeline


def extract_metadata(path: Path, media_type: str, settings: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return ProviderPipeline.from_settings(settings or {}).extract(path, media_type)
