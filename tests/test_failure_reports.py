import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import turtle_service as turtle
import failure_reports as failures
import operation_activity as activity


class FailureReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.paths.prepare()
        self.connection = dict(id='test-drive', name='Private client', backend='sftp', host='secret-host',
                               user='secret-user', password='secret-password', readOnly=False)
        self.log = self.paths.logs / 'test-drive.log'
        self.now = time.time()
        self.stamp = time.strftime('%Y/%m/%d %H:%M:%S', time.localtime(self.now))

    def line(self, body):
        return f'{self.stamp} ERROR : {body}\n'

    def test_delete_rename_listing_and_transfer_errors_have_reasons(self):
        cases = [('private/file: Failed to remove: permission denied', 'delete', 'permission'),
                 ('folder: Dir.Remove not empty', 'delete', 'notEmpty'),
                 ('secret.jpg: Failed to rename: file exists', 'move', 'exists'),
                 ('folder: ReadDir: connection reset', 'list', 'connection'),
                 ('file: vfs cache: failed upload: quota exceeded', 'upload', 'space')]
        for line, kind, reason in cases:
            with self.subTest(line=line):
                event = failures.parse(self.line(line), self.now)
                self.assertEqual((event['operation'], event['reason']), (kind, reason))
                self.assertTrue(event['path'])

    def test_persistent_cursors_partial_lines_and_rotation_do_not_duplicate(self):
        first = self.line('a: Failed to remove: permission denied')
        self.log.write_text(first + self.line('b: Failed to rename: permission denied')[:-1])
        self.assertEqual(len(failures.collect(self.paths, [self.connection], self.now)), 1)
        self.assertEqual(len(failures.collect(self.paths, [self.connection], self.now)), 1)
        with self.log.open('a') as out: out.write('\n')
        self.assertEqual(len(failures.collect(self.paths, [self.connection], self.now)), 2)
        self.log.rename(self.log.with_suffix('.old'))
        self.log.write_text(self.line('c: ReadDir: timeout'))
        self.assertEqual(len(failures.collect(self.paths, [self.connection], self.now)), 3)
        self.assertEqual((self.paths.base / 'failure-history.json').stat().st_mode & 0o777, 0o600)

    def test_export_is_allowlisted_not_a_copy_of_sensitive_data(self):
        self.log.write_text(self.line('Secret customer/medical.jpg: Failed to remove: permission denied token=SUPERSECRET https://secret-host/?key=SECRET'))
        report = failures.snapshot(self.paths, self.connection, {'state': 'error', 'rcPass': 'password'}, self.now)
        text = report['report']
        for secret in ('Secret customer', 'medical.jpg', 'SUPERSECRET', 'secret-host', 'secret-user', 'secret-password', 'test-drive', 'Private client', 'https://'):
            self.assertNotIn(secret, text)
        self.assertIn('item-1', text)
        self.assertIn('permission', text)
        self.assertIn('medical.jpg', report['events'][0]['path'])

    def test_history_is_bounded_retained_and_symlinks_are_not_followed(self):
        self.log.write_text(''.join(self.line(f'file{i}: Failed to remove: permission denied') for i in range(230)))
        self.assertEqual(len(failures.collect(self.paths, [self.connection], self.now)), failures.LIMIT)
        self.assertEqual(failures.collect(self.paths, [self.connection], self.now + failures.RETENTION + 1), [])
        other = self.paths.logs / 'other.log'
        other.write_text(self.line('secret: Failed to remove: permission denied'))
        self.log.unlink(); self.log.symlink_to(other)
        self.assertEqual(failures.collect(self.paths, [self.connection], self.now + failures.RETENTION + 1), [])

    def test_manual_report_is_available_without_logged_errors(self):
        report = failures.snapshot(self.paths, self.connection, now=self.now)
        self.assertEqual(report['events'], [])
        self.assertIn('Finder may reject', report['coverage'])
        self.assertIn('No backend failures captured', report['report'])

    def test_measured_failures_are_included_without_raw_error_strings(self):
        with self.assertRaises(RuntimeError):
            with activity.measured(self.paths, self.connection['id'], 'preview', 'private.jpg'):
                raise RuntimeError('sensitive provider output')
        report = failures.snapshot(self.paths, self.connection)
        self.assertEqual(report['events'][0]['operation'], 'preview')
        self.assertNotIn('sensitive provider output', report['report'])
        self.assertNotIn('private.jpg', report['report'])


class OperationTests(unittest.TestCase):
    setUp = FailureReportTests.setUp

    def test_measured_duration_and_failed_outcome(self):
        with patch.object(activity.time, 'monotonic', side_effect=[10, 10.125]):
            with activity.measured(self.paths, self.connection['id'], 'list', 'photos'):
                pass
        event = activity.rows(self.paths, self.connection['id'])[0]
        self.assertEqual(event['state'], 'complete')
        self.assertEqual(event['durationSeconds'], .125)

    def test_cancellation_is_not_reported_as_failure(self):
        class Cancelled(Exception):
            pass
        with self.assertRaises(Cancelled):
            with activity.measured(self.paths, self.connection['id'], 'preview', 'private', cancelled=(Cancelled,)):
                raise Cancelled()
        self.assertEqual(activity.rows(self.paths, self.connection['id'])[0]['state'], 'cancelled')
        self.assertEqual(failures.snapshot(self.paths, self.connection)['events'], [])

    def test_dead_owner_does_not_remain_running(self):
        activity.start(self.paths, self.connection['id'], 'preview', 'photo')
        with patch.object(activity.os, 'kill', side_effect=ProcessLookupError):
            self.assertEqual(activity.rows(self.paths, self.connection['id'])[0]['state'], 'unknown')

    def test_live_transfers_use_only_safe_counter_endpoint_and_end_as_unconfirmed(self):
        post = Mock(return_value={'transferring': [{'name': 'file.jpg', 'bytes': 100, 'size': 1000, 'speed': 50}]})
        live = {'state': 'connected', 'pid': os.getpid(), 'sessionID': 'session'}
        result = activity.snapshot(self.paths, self.connection, live, post)
        event = result['events'][0]
        self.assertTrue(event['observed'])
        self.assertEqual(event['state'], 'running')
        self.assertEqual(event['bytes'], 100)
        post.assert_called_once_with(live, 'core/stats', {}, timeout=1, max_response_bytes=1024 * 1024)
        post.return_value = {'transferring': []}
        result = activity.snapshot(self.paths, self.connection, live, post)
        self.assertEqual(result['events'][0]['state'], 'unknown')
        self.assertNotIn('durationSeconds', result['events'][0])

    def test_unavailable_counters_never_claim_running_or_completed(self):
        post = Mock(return_value={'transferring': [{'name': 'file'}]})
        live = {'state': 'connected', 'pid': os.getpid()}
        activity.snapshot(self.paths, self.connection, live, post)
        post.side_effect = OSError('timeout')
        result = activity.snapshot(self.paths, self.connection, live, post)
        self.assertEqual(result['events'][0]['state'], 'unknown')
        self.assertIn('unavailable', result['message'])

    def test_disconnected_status_never_calls_remote_control(self):
        post = Mock()
        activity.snapshot(self.paths, self.connection, {'state': 'disconnected'}, post)
        post.assert_not_called()

class ListingMonitorTests(unittest.TestCase):
    setUp = FailureReportTests.setUp

    def test_listing_job_records_completion_without_tree_statistics(self):
        def thread(**kwargs):
            worker = Mock()
            worker.start.side_effect = kwargs['target']
            return worker
        with patch.object(turtle, 'start_directory_refresh', return_value={'jobid': 7}) as refresh, \
             patch.object(turtle, 'remote_control_post', return_value={'finished': True, 'success': True, 'output': {'result': {'photos': 'OK'}}}) as post, \
             patch.object(turtle.threading, 'Thread', side_effect=thread):
            turtle.observed_directory_refresh(self.paths, self.connection, {'rcPort': 1}, 'photos', False)
        refresh.assert_called_once_with({'rcPort': 1}, 'photos', recursive=False)
        post.assert_called_once_with({'rcPort': 1}, 'job/status', {'jobid': 7}, timeout=1)
        event = activity.rows(self.paths, self.connection['id'])[0]
        self.assertEqual(event['state'], 'complete')
        self.assertGreaterEqual(event['durationSeconds'], 0)

    def test_failed_listing_result_is_not_mislabeled_complete(self):
        def thread(**kwargs):
            worker = Mock(); worker.start.side_effect = kwargs['target']; return worker
        with patch.object(turtle, 'start_directory_refresh', return_value={'jobid': 7}), \
             patch.object(turtle, 'remote_control_post', return_value={'finished': True, 'success': True, 'output': {'result': {'photos': 'permission denied'}}}), \
             patch.object(turtle.threading, 'Thread', side_effect=thread):
            turtle.observed_directory_refresh(self.paths, self.connection, {}, 'photos', False)
        self.assertEqual(activity.rows(self.paths, self.connection['id'])[0]['state'], 'failed')
