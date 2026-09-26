import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import archive_folder as archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'Documents'
        self.source.mkdir()
        (self.source / 'empty').mkdir()
        (self.source / 'text.txt').write_text('hello' * 10000)
        (self.source / '.hidden').write_text('hidden')
        (self.source / 'line\nbreak').write_text('newline')
        self.output = self.root / 'result.zip'

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_round_trip_empty_hidden_and_unusual_names(self):
        updates = []
        archive.make_zip(self.source, self.output, lambda phase, **v: updates.append((phase, v)))
        with zipfile.ZipFile(self.output) as result:
            self.assertIsNone(result.testzip())
            self.assertEqual(result.read('Documents/text.txt'), b'hello' * 10000)
            self.assertEqual(result.read('Documents/line\nbreak'), b'newline')
            self.assertIn('Documents/empty/', result.namelist())
            self.assertIn('Documents/.hidden', result.namelist())
        self.assertEqual(updates[-1][1]['files'], 3)

    def test_symlink_is_not_followed(self):
        (self.source / 'link').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            archive.make_zip(self.source, self.output)

    def test_existing_zip_is_not_overwritten(self):
        self.output.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            archive.make_zip(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep')

    def test_space_guard(self):
        with patch.object(archive.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(10, 9, 1)):
            with self.assertRaisesRegex(ValueError, 'free space'):
                archive.check_space(self.root)

    def test_download_reports_stats_and_failure(self):
        code = 'import json,sys; print(json.dumps({"stats":{"transfers":2,"bytes":99}}),file=sys.stderr); sys.exit(1)'
        updates = []
        with self.assertRaisesRegex(ValueError, 'downloaded completely'):
            archive.download([sys.executable, '-c', code], None, self.root,
                             lambda phase, **v: updates.append(v))
        self.assertEqual(updates[-1]['bytes'], 99)

    def test_download_cancellation_reaps_child(self):
        children = []
        real = subprocess.Popen
        def spawn(*a, **kw):
            child = real(*a, **kw); children.append(child); return child
        with patch.object(archive.subprocess, 'Popen', side_effect=spawn), \
             patch.object(archive, 'check_space', side_effect=archive.Cancelled):
            with self.assertRaises(archive.Cancelled):
                archive.download([sys.executable, '-c', 'import time; time.sleep(60)'], None, self.root)
        self.assertIsNotNone(children[0].poll())

    def run_job(self, downloader, pending=False, destination=None, mounts=None):
        connection = {'id': 'test', 'readOnly': False}
        paths = archive.service.Paths(home=self.root)
        with patch.object(archive.service.Store, 'read', return_value={'connections': [connection]}), \
             patch.object(archive.service, '_folder_cache_target', return_value=(connection, 'folder/Documents')), \
             patch.object(archive.service, 'pending_writes', return_value=pending), \
             patch.object(archive.service, 'mount_table', return_value=mounts or set()), \
             patch.object(archive.service, 'executable', return_value='/rclone'), \
             patch.object(archive.service, 'connection_config', return_value=('test', 'volume:')), \
             patch.object(archive.service, 'mount_environment', return_value={}), \
             patch.object(archive, 'download', side_effect=downloader):
            archive.archive_folder(paths, '/mount/Documents', str(destination or self.output), lambda *a, **kw: None)

    def test_complete_job_and_temporary_cleanup(self):
        def download(command, *args):
            self.assertEqual(command[2], 'volume:folder/Documents')
            shutil.copytree(self.source, command[3], dirs_exist_ok=True)
        self.run_job(download)
        self.assertTrue(self.output.is_file())
        self.assertEqual(list(self.root.glob('.mountainturtle-compress-*')), [])

    def test_cancel_cleans_staging_without_publishing(self):
        def download(*args):
            raise archive.Cancelled()
        with self.assertRaises(archive.Cancelled):
            self.run_job(download)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.mountainturtle-compress-*')), [])

    def test_cancel_during_zip_cleans_staging(self):
        def download(command, *args):
            shutil.copytree(self.source, command[3], dirs_exist_ok=True)
        with patch.object(archive, 'make_zip', side_effect=archive.Cancelled):
            with self.assertRaises(archive.Cancelled):
                self.run_job(download)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.mountainturtle-compress-*')), [])

    def test_symlink_to_network_destination_is_rejected(self):
        network = self.root / 'network'
        network.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(network, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'local disk'):
            self.run_job(lambda *a: self.fail('must not download'),
                         destination=alias / 'test.zip', mounts={str(network)})

    def test_pending_uploads_block_remote_snapshot(self):
        with self.assertRaisesRegex(ValueError, 'waiting to upload'):
            self.run_job(lambda *a: self.fail('must not download'), pending=True)

    def test_destination_created_during_download_is_preserved(self):
        def download(command, *args):
            shutil.copytree(self.source, command[3], dirs_exist_ok=True)
            self.output.write_text('keep this')
        with self.assertRaises(FileExistsError):
            self.run_job(download)
        self.assertEqual(self.output.read_text(), 'keep this')
        self.assertEqual(list(self.root.glob('.mountainturtle-compress-*')), [])


if __name__ == '__main__':
    unittest.main()
