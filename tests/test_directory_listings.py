import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import directory_listings as cache
import file_browser
import turtle_service as turtle

ENTRY = dict(name='photo.jpg', directory=False, size=12, modified=123.0)


class DirectoryListingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.paths = turtle.Paths(home=temp.name)
        self.paths.prepare()
        self.connection = dict(id='fixture', backend='sftp', host='example.test', remotePath='/photos')
        with turtle.Store(self.paths).update() as state:
            state['connections'] = [self.connection]

    def run_listing(self, pages=(), code=0, refresh=False):
        process = Mock(stdout=io.BytesIO(b''.join(json.dumps(p).encode() + b'\n' for p in pages)))
        process.wait.return_value = code; process.poll.return_value = code
        output = io.StringIO()
        with patch.object(turtle, 'executable', return_value='/rclone'), \
             patch.object(turtle, 'mount_environment', return_value={}), \
             patch.object(file_browser, 'listing_command', return_value=['fixture']), \
             patch.object(file_browser.signal, 'signal'), \
             patch.object(file_browser.subprocess, 'Popen', return_value=process) as spawn, \
             contextlib.redirect_stdout(output):
            result = file_browser.run(self.paths, 'fixture', 'album', refresh)
        return result, [json.loads(line) for line in output.getvalue().splitlines()], spawn

    def test_complete_listing_survives_reopen_without_remote_request(self):
        result, _, _ = self.run_listing([dict(entries=[ENTRY]), dict(complete=True)])
        self.assertEqual(result, 0)
        result, pages, spawn = self.run_listing()
        spawn.assert_not_called()
        self.assertEqual(pages[0]['entries'], [ENTRY])
        self.assertTrue(pages[-1]['complete']); self.assertTrue(pages[-1]['cached'])
        self.assertEqual(cache.location(self.paths, self.connection, 'album').stat().st_mode & 0o777, 0o600)

    def test_stale_snapshot_precedes_refresh_and_replaces_deleted_entries(self):
        with patch.object(cache.time, 'time', return_value=time.time() - 60):
            cache.save(self.paths, self.connection, 'album', [ENTRY])
        _, pages, spawn = self.run_listing([dict(complete=True)])
        spawn.assert_called_once()
        self.assertTrue(pages[0]['cached'])
        self.assertEqual(pages[0]['entries'], [ENTRY])
        self.assertTrue(pages[2]['refreshing'])
        self.assertEqual(cache.load(self.paths, self.connection, 'album')['entries'], [])

    def test_explicit_refresh_bypasses_fresh_snapshot(self):
        cache.save(self.paths, self.connection, 'album', [ENTRY])
        _, pages, spawn = self.run_listing([dict(complete=True)], refresh=True)
        spawn.assert_called_once()
        self.assertTrue(pages[0]['cached']); self.assertNotIn('cached', pages[-1])

    def test_failed_or_partial_refresh_preserves_last_complete_snapshot(self):
        cache.save(self.paths, self.connection, 'album', [ENTRY])
        for pages, code in (([dict(entries=[])], 0), ([dict(complete=True)], 1), ([dict(error='offline')], 1)):
            with self.subTest(pages=pages, code=code):
                result, emitted, _ = self.run_listing(pages, code, refresh=True)
                self.assertNotEqual(result, 0)
                self.assertFalse(any(p.get('complete') for p in emitted))
                self.assertEqual(cache.load(self.paths, self.connection, 'album')['entries'], [ENTRY])

    def test_scope_tracks_remote_and_directory_but_not_reconnection_revision(self):
        cache.save(self.paths, self.connection, 'album', [ENTRY])
        self.assertIsNotNone(cache.load(self.paths, dict(self.connection, revision=99), 'album'))
        self.assertIsNone(cache.load(self.paths, dict(self.connection, host='other.test'), 'album'))
        self.assertIsNone(cache.load(self.paths, self.connection, 'other'))

    def test_corrupt_expired_oversized_and_symlink_snapshots_are_ignored(self):
        path = cache.location(self.paths, self.connection, 'album')
        cache.save(self.paths, self.connection, 'album', [ENTRY])
        good = path.read_bytes()
        for data in (b'bad json', json.dumps(dict(version=1, savedAt=0, entries=[ENTRY])).encode(),
                     json.dumps(dict(version=1, savedAt=time.time(), entries=[dict(ENTRY, name='../bad')])).encode()):
            path.write_bytes(data); self.assertIsNone(cache.load(self.paths, self.connection, 'album'))
        path.write_bytes(good)
        with patch.object(cache, 'MAX_BYTES', 1):
            self.assertIsNone(cache.load(self.paths, self.connection, 'album'))
        path.unlink(); target = Path(self.paths.home) / 'private'; target.write_bytes(good); path.symlink_to(target)
        self.assertIsNone(cache.load(self.paths, self.connection, 'album'))

    def test_cache_budget_evicts_snapshots_without_touching_file_data(self):
        data = self.paths.cache / 'file-data'; data.parent.mkdir(exist_ok=True); data.write_bytes(b'keep')
        with patch.object(cache, 'TOTAL_BYTES', 1):
            cache.save(self.paths, self.connection, 'album', [ENTRY])
        self.assertIsNone(cache.load(self.paths, self.connection, 'album'))
        self.assertEqual(data.read_bytes(), b'keep')
