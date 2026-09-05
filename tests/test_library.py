import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from meizang.database import LibraryDatabase
from meizang.providers import ProviderPipeline
from meizang.providers.base import merge_metadata
from meizang.providers.local import FilenameProvider, NfoProvider
from meizang.providers.tmdb import TMDBProvider
from meizang.scanner import media_type, scan_root
from meizang.server import MeizangApplication


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "media"
        self.root.mkdir()
        self.database = LibraryDatabase(str(Path(self.temporary.name) / "library.db"))
        self.root_record = self.database.add_root(str(self.root), "测试媒体")

    def tearDown(self):
        self.temporary.cleanup()

    def test_media_classification_is_case_insensitive(self):
        self.assertEqual(media_type(Path("PHOTO.JPG")), "image")
        self.assertEqual(media_type(Path("movie.MKV")), "video")
        self.assertEqual(media_type(Path("song.FLAC")), "audio")
        self.assertEqual(media_type(Path("notes.txt")), "other")

    def test_incremental_scan_and_duplicates(self):
        content = b"same-image-content"
        (self.root / "one.JPG").write_bytes(content)
        (self.root / "two.png").write_bytes(content)
        (self.root / "movie.mkv").write_bytes(b"not-a-real-video")

        first = scan_root(self.database, self.root_record["id"])
        self.assertEqual(first["inserted"], 3)
        self.assertEqual(first["removed"], 0)
        self.assertEqual(self.database.stats()["duplicate_groups"], 1)

        second = scan_root(self.database, self.root_record["id"])
        self.assertEqual(second["unchanged"], 3)

        (self.root / "movie.mkv").write_bytes(b"changed-video")
        third = scan_root(self.database, self.root_record["id"])
        self.assertEqual(third["updated"], 1)

        (self.root / "two.png").unlink()
        fourth = scan_root(self.database, self.root_record["id"])
        self.assertEqual(fourth["removed"], 1)
        self.assertEqual(self.database.stats()["total"], 2)

    def test_missing_root_keeps_index(self):
        (self.root / "photo.jpg").write_bytes(b"photo")
        scan_root(self.database, self.root_record["id"])
        self.root.rename(self.root.with_name("offline"))
        with self.assertRaisesRegex(ValueError, "索引未发生改变"):
            scan_root(self.database, self.root_record["id"])
        self.assertEqual(self.database.stats()["total"], 1)

    def test_authorized_path_boundary(self):
        previous = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS")
        os.environ["TRIM_DATA_ACCESSIBLE_PATHS"] = str(self.root)
        try:
            app = MeizangApplication(
                str(Path(self.temporary.name) / "app.db"),
                str(Path(__file__).resolve().parents[1] / "frontend"),
            )
            self.assertEqual(app.normalize_root(str(self.root)), self.root.resolve())
            with self.assertRaisesRegex(ValueError, "尚未.*授权"):
                app.normalize_root(str(Path(self.temporary.name)))
        finally:
            if previous is None:
                os.environ.pop("TRIM_DATA_ACCESSIBLE_PATHS", None)
            else:
                os.environ["TRIM_DATA_ACCESSIBLE_PATHS"] = previous

    def test_authorized_paths_refresh_after_picker(self):
        app = MeizangApplication(
            str(Path(self.temporary.name) / "refresh.db"),
            str(Path(__file__).resolve().parents[1] / "frontend"),
        )
        app.allowed_paths = [Path(self.temporary.name) / "previous"]
        app.enforce_allowed_paths = True
        with patch("meizang.server.shared_accessible_folders", return_value=[str(self.root)]):
            with patch.dict(os.environ, {"TRIM_API_TOKEN": "test-token"}):
                self.assertEqual(app.normalize_root(str(self.root)), self.root.resolve())

    def test_provider_settings_are_persisted(self):
        settings = self.database.update_settings({
            "tmdb_enabled": "true",
            "tmdb_token": "secret-token",
            "tmdb_language": "zh-CN",
            "unknown": "ignored",
        })
        self.assertEqual(settings["tmdb_token"], "secret-token")
        self.assertNotIn("unknown", settings)

    def test_nfo_provider_overrides_filename_metadata(self):
        movie = self.root / "Example.Movie.2024.mkv"
        movie.write_bytes(b"video")
        movie.with_suffix(".nfo").write_text(
            "<movie><title>示例电影</title><year>2024</year><genre>剧情</genre>"
            "<actor><name>演员甲</name><role>主角</role></actor>"
            "<uniqueid type='tmdb'>123</uniqueid></movie>",
            encoding="utf-8",
        )
        metadata = ProviderPipeline([FilenameProvider(), NfoProvider()]).extract(movie, "video")
        self.assertEqual(metadata["title"], "示例电影")
        self.assertEqual(metadata["genres"], ["剧情"])
        self.assertEqual(metadata["cast"][0]["name"], "演员甲")
        self.assertEqual(metadata["provider"], "nfo")

    def test_provider_merge_supports_object_lists(self):
        merged = merge_metadata(
            {"cast": [{"name": "演员甲"}]},
            {"cast": [{"name": "演员甲"}, {"name": "演员乙"}]},
            "test",
        )
        self.assertEqual([item["name"] for item in merged["cast"]], ["演员甲", "演员乙"])

    def test_tmdb_provider_maps_search_and_details(self):
        responses = iter([
            {"results": [{"id": 123}]},
            {
                "id": 123,
                "title": "示例电影",
                "original_title": "Example Movie",
                "release_date": "2024-03-01",
                "overview": "一段简介",
                "runtime": 118,
                "vote_average": 8.2,
                "vote_count": 42,
                "genres": [{"name": "剧情"}],
                "poster_path": "/poster.jpg",
                "credits": {
                    "crew": [{"job": "Director", "name": "导演甲"}],
                    "cast": [{"name": "演员甲", "character": "主角"}],
                },
                "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "abc"}]},
            },
        ])
        requested_urls = []

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

            def close(self):
                pass

        def opener(request, timeout):
            requested_urls.append(request.full_url)
            return Response(next(responses))

        provider = TMDBProvider("api-key", opener=opener)
        metadata = provider.fetch(Path("Example.Movie.2024.mkv"), "video", {"title": "Example Movie", "year": 2024})
        self.assertEqual(metadata["title"], "示例电影")
        self.assertEqual(metadata["director"], "导演甲")
        self.assertEqual(metadata["poster_url"], "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertIn("api_key=api-key", requested_urls[0])

    def test_force_metadata_scan_updates_unchanged_video(self):
        movie = self.root / "Movie.2024.mkv"
        movie.write_bytes(b"video")
        scan_root(self.database, self.root_record["id"])
        refreshed = {
            "title": "刷新后的标题", "year": 2024, "duration": 100.0,
            "width": 1920, "height": 1080, "codec": "h264", "provider": "test",
        }
        with patch("meizang.scanner.extract_metadata", return_value=refreshed):
            result = scan_root(self.database, self.root_record["id"], force_metadata=True)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.database.assets("video")[0]["title"], "刷新后的标题")


if __name__ == "__main__":
    unittest.main()
