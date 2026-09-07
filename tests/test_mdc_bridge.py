import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from meizang.database import LibraryDatabase
from meizang.mdc_bridge import MDCConfigStore, MDCManager, parse_ini, write_ini


class MDCBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.media = self.base / "media"
        self.media.mkdir()
        self.database = LibraryDatabase(str(self.base / "library.db"))
        self.root = self.database.add_root(str(self.media), "影片")

    def tearDown(self):
        self.temporary.cleanup()

    def test_full_source_config_is_available(self):
        store = MDCConfigStore(self.database)
        value = store.public()
        self.assertGreaterEqual(value["provider_count"], 15)
        for section in ("common", "proxy", "Name_Rule", "priority", "translate", "watermark", "face", "actor_photo"):
            self.assertIn(section, value["sections"])

    def test_config_roundtrip_and_secret_preservation(self):
        store = MDCConfigStore(self.database)
        sections = parse_ini(store.text())
        sections["translate"]["key"] = "secret"
        store.save(sections)
        result = store.save({"translate": {"key": "", "switch": "1"}})
        self.assertTrue(result["secrets"]["translate.key"])
        self.assertEqual(parse_ini(store.text())["translate"]["key"], "secret")
        self.assertEqual(parse_ini(store.text())["translate"]["switch"], "1")

    def test_invalid_config_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "缺少"):
            parse_ini("[proxy]\nswitch=0\n")
        sections = parse_ini(MDCConfigStore(self.database).text())
        sections["common"]["main_mode"] = "9"
        with self.assertRaisesRegex(ValueError, "只能"):
            write_ini(sections)

    def test_job_payload_enforces_authorized_output(self):
        manager = MDCManager(
            self.database,
            lambda path: Path(path).resolve(),
            lambda path: self.media.resolve() == path.resolve() or self.media.resolve() in path.resolve().parents,
        )
        payload = manager._normalize_payload({"kind": "scan", "root_id": self.root["id"], "mode": 3, "dry_run": True})
        self.assertEqual(payload["source_folder"], str(self.media.resolve()))
        sections = parse_ini(manager.config.text())
        sections["common"]["failed_output_folder"] = str(self.base / "outside")
        manager.config.save(sections)
        with self.assertRaisesRegex(ValueError, "授权"):
            manager._normalize_payload({"kind": "scan", "root_id": self.root["id"], "mode": 3})

    def test_persistent_jobs_and_schedules(self):
        job = self.database.create_mdc_job("job-1", "scan", {"root_id": self.root["id"]})
        self.database.append_mdc_log(job["id"], "hello")
        self.assertIn("hello", self.database.mdc_job(job["id"])["log_text"])
        schedule = self.database.create_mdc_schedule("每天", 3600, {"kind": "scan"})
        self.assertEqual(self.database.mdc_schedules()[0]["id"], schedule["id"])


if __name__ == "__main__":
    unittest.main()
