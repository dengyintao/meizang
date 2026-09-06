import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from meizang.database import LibraryDatabase
from meizang.organizer import MediaOrganizer, QBIntegration, map_qb_path
from meizang.qbittorrent import QBittorrentClient


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.library = self.base / 'library'
        self.downloads = self.base / 'downloads'
        self.library.mkdir()
        self.downloads.mkdir()
        self.source = self.downloads / 'film.mkv'
        self.target = self.library / 'Film' / 'film.mkv'
        self.source.write_bytes(b'original media bytes')
        self.db = LibraryDatabase(str(self.base / 'test.db'))
        self.organizer = MediaOrganizer(self.db, lambda path: self.base in path.parents)

    def transfer(self):
        return self.organizer._ensure_managed_link('abc', 'Film', self.source, self.target, self.library)

    def test_no_copy_transfer_and_cleanup_preserve_original_inode(self):
        original = self.source.stat()
        self.assertTrue(self.transfer())
        self.assertTrue(self.source.is_symlink())
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.target.stat().st_ino, original.st_ino)
        self.assertEqual(self.source.read_bytes(), b'original media bytes')
        self.assertFalse(self.transfer())
        self.assertEqual(self.organizer.cleanup_removed({'abc'})['removed'], 0)
        self.assertEqual(self.organizer.cleanup_removed(set())['removed'], 1)
        self.assertFalse(self.source.is_symlink())
        self.assertEqual(self.target.read_bytes(), b'original media bytes')

    def test_never_overwrite_existing_target(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'other film')
        with self.assertRaises(ValueError):
            self.transfer()
        self.assertEqual(self.target.read_bytes(), b'other film')
        self.assertEqual(self.source.read_bytes(), b'original media bytes')
        self.assertEqual(self.db.managed_links(), [])

    def test_do_not_adopt_existing_symlink(self):
        self.target.parent.mkdir()
        self.source.rename(self.target)
        self.source.symlink_to(self.target)
        with self.assertRaises(ValueError):
            self.transfer()
        self.assertEqual(self.db.managed_links(), [])
        self.organizer.cleanup_removed(set())
        self.assertTrue(self.source.is_symlink())

    def test_symlink_failure_rolls_back_without_copy(self):
        inode = self.source.stat().st_ino
        with patch.object(Path, 'symlink_to', side_effect=OSError('permission denied')):
            with self.assertRaises(OSError):
                self.transfer()
        self.assertTrue(self.source.is_file())
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.stat().st_ino, inode)

    def test_cleanup_keeps_replaced_download_file(self):
        self.transfer()
        self.source.unlink()
        self.source.write_bytes(b'new download')
        self.assertEqual(self.organizer.cleanup_removed(set())['conflict'], 1)
        self.assertEqual(self.source.read_bytes(), b'new download')
        self.assertEqual(self.target.read_bytes(), b'original media bytes')

    def test_recover_interrupted_transfer_from_durable_identity(self):
        identity = self.organizer._identity(self.source)
        self.db.begin_managed_link('abc', 'Film', str(self.source), str(self.target), identity)
        self.target.parent.mkdir()
        self.source.rename(self.target)
        self.assertTrue(self.transfer())
        self.assertEqual(self.source.read_bytes(), b'original media bytes')

    def test_directory_symlink_rejected(self):
        (self.library / 'Film').symlink_to(self.downloads)
        with self.assertRaises(ValueError):
            self.transfer()
        self.assertFalse(self.source.is_symlink())

    def test_cross_device_failure_keeps_source(self):
        with patch('meizang.organizer.os.link', side_effect=OSError(18, 'Cross-device link')):
            with self.assertRaises(OSError):
                self.transfer()
        self.assertEqual(self.source.read_bytes(), b'original media bytes')
        self.assertFalse(self.target.exists())

    def test_mapping_rejects_relative_and_traversal_paths(self):
        for path in ('relative/file.mkv', '/downloads/../film.mkv'):
            with self.assertRaises(ValueError):
                map_qb_path(path)
        self.assertEqual(map_qb_path('/downloads/film.mkv', '/downloads', str(self.downloads)), self.source)

    def test_bad_qb_response_is_not_empty_snapshot(self):
        client = QBittorrentClient('http://localhost:8080', opener=lambda *args, **kwargs: io.BytesIO(b'{}'))
        with self.assertRaises(ValueError):
            client.torrents()

    def test_cleanup_requires_separate_successful_snapshots(self):
        self.transfer()
        self.db.update_settings({'qb_enabled': 'true', 'qb_library_root': str(self.library)})
        integration = QBIntegration(self.db, lambda path: self.base in path.parents)
        client = Mock()
        client.login.return_value = 'v5.0'
        client.torrents.return_value = []
        with patch.object(integration, 'client', return_value=client), patch('meizang.organizer.time.monotonic', side_effect=[0, 31]):
            integration.sync_once()
            self.assertTrue(self.source.is_symlink())
            integration.sync_once()
        self.assertFalse(self.source.is_symlink())
        self.assertEqual(self.target.read_bytes(), b'original media bytes')

    def test_offline_qb_keeps_links_and_resets_missing_confirmation(self):
        self.transfer()
        self.db.update_settings({'qb_enabled': 'true'})
        integration = QBIntegration(self.db, lambda path: True)
        integration.missing_since = {'abc': 0}
        with patch.object(integration, 'client', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                integration.sync_once()
        self.assertEqual(integration.missing_since, {})
        self.assertTrue(self.source.is_symlink())

    def test_waits_for_pause_before_file_changes(self):
        client = Mock()
        client.files.return_value = [{'name':'film.mkv', 'progress':1, 'priority':1}]
        torrent = {'hash':'abc', 'name':'Film', 'state':'uploading', 'save_path': str(self.downloads)}
        client.torrents.return_value = [dict(torrent, amount_left=0)]
        with patch('meizang.organizer.time.sleep'):
            with self.assertRaises(ValueError):
                self.organizer.organize_torrent(client, torrent, {'qb_library_root':str(self.library)})
        self.assertFalse(self.source.is_symlink())
        self.assertFalse(self.target.exists())

    def test_full_sync_moves_video_indexes_library_and_preserves_music(self):
        music = self.downloads / 'song.flac'
        music.write_bytes(b'music')
        torrent = {'hash':'abc', 'name':'Film', 'state':'uploading', 'save_path':str(self.downloads), 'progress':1, 'amount_left':0, 'category':'meizang'}
        client = Mock()
        client.login.return_value = 'v5.0'
        client.torrents.side_effect = [[torrent], [dict(torrent, state='stoppedUP')]]
        client.files.return_value = [{'name':name, 'progress':1, 'priority':1} for name in ('film.mkv', 'song.flac')]
        self.db.update_settings({'qb_enabled':'true', 'qb_library_root':str(self.library)})
        integration = QBIntegration(self.db, lambda path: self.base in path.parents)
        with patch.object(integration, 'client', return_value=client):
            result = integration.sync_once()
        self.assertEqual(result['organized']['created'], 1)
        self.assertEqual(self.db.assets('video')[0]['path'], str(self.target))
        self.assertFalse(music.is_symlink())
        self.assertEqual(music.read_bytes(), b'music')
        client.pause.assert_called_once_with('abc')
        client.resume.assert_called_once_with('abc')


if __name__ == '__main__':
    unittest.main()
