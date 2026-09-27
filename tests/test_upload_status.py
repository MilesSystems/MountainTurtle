import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('upload_status', Path(__file__).resolve().parents[1] / 'service/upload_status.py')
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)


class UploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.paths = SimpleNamespace(cache=Path(temp.name))
        self.connection = dict(id='drive', readOnly=False)
        self.root = self.paths.cache / 'drive/vfsMeta/volume'
        self.root.mkdir(parents=True)
        self.queue = dict(queue=[])
        self.post = Mock(return_value=self.queue)

    def snapshot(self, mounted=True):
        return upload.snapshot(self.connection, self.paths, {}, mounted, self.post)

    def test_clear_requires_live_queue_and_complete_cache(self):
        (self.root / 'file').write_text(json.dumps(dict(Dirty=False)))
        self.assertEqual(self.snapshot()['state'], 'clear')
        self.post.side_effect = OSError()
        self.assertEqual(self.snapshot()['state'], 'unknown')

    def test_dirty_cache_is_pending_even_if_queue_empty_or_read_only(self):
        (self.root / 'file').write_text(json.dumps(dict(Dirty=True)))
        for read_only in (False, True):
            self.connection['readOnly'] = read_only
            self.assertEqual(self.snapshot()['state'], 'pending')
            self.assertEqual(self.snapshot(False)['state'], 'pending')

    def test_corrupt_and_symlink_metadata_never_report_clear(self):
        item = self.root / 'file'
        for content in ('{', '[]', '{}', '{"Dirty": "false"}'):
            item.write_text(content)
            self.assertEqual(self.snapshot()['state'], 'unknown')
        item.unlink()
        item.symlink_to('/does-not-exist')
        self.assertEqual(self.snapshot()['state'], 'unknown')

    def test_empty_queue_does_not_claim_open_apps_have_saved(self):
        result = self.snapshot()
        self.assertEqual(result['title'], 'No queued uploads')
        self.assertIn('unsaved changes', result['message'])
        self.post.assert_called_once_with({}, 'vfs/queue', {}, timeout=1, max_response_bytes=1024 * 1024)

    def test_invalid_queue_is_unknown_not_empty(self):
        for queue in (None, {}, [None], [{'id': 1, 'uploading': 'false', 'tries': 0}],
                      [{'id': 1, 'uploading': False, 'tries': -1}]):
            self.queue['queue'] = queue
            self.assertEqual(self.snapshot()['state'], 'unknown')

    def test_bounded_scan_cannot_claim_completion(self):
        (self.root / 'file').write_text('{"Dirty":false}')
        self.assertEqual(upload.dirty_cache(self.connection, self.paths, limit=0), (0, False))

    def test_waiting_active_and_retried_uploads_are_distinguished(self):
        self.queue['queue'] = [dict(id=1, uploading=False, tries=0), dict(id=2, uploading=True, tries=1)]
        result = self.snapshot()
        self.assertTrue(result['canRetry'])
        self.assertEqual((result['queued'], result['active']), (1, 1))
        self.assertEqual(result['state'], 'pending')
        self.queue['queue'][0]['tries'] = 1
        self.assertEqual(self.snapshot()['state'], 'attention')

    def test_stalled_directory_statistics_are_never_requested(self):
        def local_control(live, method, payload, **kwargs):
            if method == 'vfs/stats':
                raise AssertionError('This endpoint locks the directory tree')
            self.assertEqual(method, 'vfs/queue')
            return {'queue': []}
        self.post.side_effect = local_control
        self.assertEqual(self.snapshot()['state'], 'clear')

    def test_disconnected_never_contacts_control_api(self):
        self.assertEqual(self.snapshot(False)['state'], 'unknown')
        self.post.assert_not_called()

    def test_retry_only_queued_ids_and_does_not_remount(self):
        self.post.side_effect = [dict(queue=[dict(id=1, uploading=False), dict(id=2, uploading=True)]), {}]
        self.assertTrue(upload.retry(self.connection, {}, True, self.post)['ok'])
        self.assertEqual(self.post.call_args.args[1:], ('vfs/queue-set-expiry', {'id': 1, 'expiry': -1}))
        self.assertEqual(self.post.call_count, 2)

    def test_retry_rejects_read_only_or_disconnected(self):
        for readonly, mounted in ((True, True), (False, False)):
            self.connection['readOnly'] = readonly
            with self.assertRaises(ValueError):
                upload.retry(self.connection, {}, mounted, self.post)
        self.post.assert_not_called()

    def test_retry_failure_keeps_original_queue(self):
        self.post.side_effect = OSError()
        with self.assertRaisesRegex(ValueError, 'automatic retry'):
            upload.retry(self.connection, {}, True, self.post)
