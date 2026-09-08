import json
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .base import Metadata, MetadataProvider


class TMDBProvider(MetadataProvider):
    name = "tmdb"
    api_base = "https://api.themoviedb.org/3"
    image_base = "https://image.tmdb.org/t/p"

    def __init__(self, token: str, language: str = "zh-CN", timeout: float = 10.0, opener=None, proxy_url: str = ""):
        self.token = token.strip()
        self.language = language or "zh-CN"
        self.timeout = timeout
        self.proxy_url = proxy_url.strip()
        self.opener = opener or build_opener(ProxyHandler(
            {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else {}
        )).open

    def supports(self, path: Path, media_type: str) -> bool:
        return media_type == "video" and bool(self.token)

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        query = dict(params or {})
        query.setdefault("language", self.language)
        headers = {"Accept": "application/json", "User-Agent": "Meizang/0.8.2"}
        if self.token.startswith("eyJ"):
            headers["Authorization"] = "Bearer {}".format(self.token)
        else:
            query["api_key"] = self.token
        url = "{}{}?{}".format(self.api_base, endpoint, urlencode(query))
        response = self.opener(Request(url, headers=headers), timeout=self.timeout)
        try:
            return json.loads(response.read().decode("utf-8"))
        finally:
            response.close()

    def test_connection(self) -> bool:
        response = self._get("/configuration")
        return bool(response.get("images"))

    def fetch(self, path: Path, media_type: str, current: Metadata) -> Metadata:
        query = str(current.get("search_title") or current.get("title") or path.stem)
        params: Dict[str, Any] = {"query": query, "include_adult": "false"}
        if current.get("year"):
            params["year"] = current["year"]
        search = self._get("/search/movie", params)
        results = search.get("results", [])
        if not results:
            return {}
        match = results[0]
        details = self._get("/movie/{}".format(match["id"]), {"append_to_response": "credits,videos"})
        credits = details.get("credits", {})
        director = next((item.get("name", "") for item in credits.get("crew", []) if item.get("job") == "Director"), "")
        cast = [
            {
                "name": item.get("name", ""),
                "character": item.get("character", ""),
                "profile_url": self._image(item.get("profile_path"), "w185"),
            }
            for item in credits.get("cast", [])[:20] if item.get("name")
        ]
        trailer = next(
            (item for item in details.get("videos", {}).get("results", []) if item.get("site") == "YouTube" and item.get("type") == "Trailer"),
            None,
        )
        release_date = details.get("release_date", "")
        return {
            "title": details.get("title"),
            "original_title": details.get("original_title"),
            "year": int(release_date[:4]) if release_date[:4].isdigit() else current.get("year"),
            "release_date": release_date,
            "plot": details.get("overview"),
            "runtime": details.get("runtime"),
            "director": director,
            "studio": (details.get("production_companies") or [{}])[0].get("name", ""),
            "collection": (details.get("belongs_to_collection") or {}).get("name", ""),
            "rating": details.get("vote_average"),
            "votes": details.get("vote_count"),
            "genres": [item.get("name") for item in details.get("genres", []) if item.get("name")],
            "cast": cast,
            "external_ids": {"tmdb": str(details.get("id"))},
            "provider_id": str(details.get("id")),
            "provider_url": "https://www.themoviedb.org/movie/{}".format(details.get("id")),
            "poster_url": self._image(details.get("poster_path"), "w500"),
            "backdrop_url": self._image(details.get("backdrop_path"), "w1280"),
            "trailer_url": "https://www.youtube.com/watch?v={}".format(trailer["key"]) if trailer else "",
        }

    def _image(self, path: Optional[str], size: str) -> str:
        return "{}{}/{}".format(self.image_base, "/" + size, path.lstrip("/")) if path else ""
