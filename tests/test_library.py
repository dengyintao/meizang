import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from meizang.database import LibraryDatabase
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


if __name__ == "__main__":
    unittest.main()
