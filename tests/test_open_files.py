import os
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import open_files
import turtle_service as turtle


def fields(*items):
    return b'\0'.join(item.encode() for item in items) + b'\0\n'


class OpenFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.connection = {'id': 'drive', 'name': 'Photos'}
        self.root = str(self.paths.mounts / 'Photos')
        self.lock = str(self.paths.base / 'service.lock')

    def test_paths_are_scoped_to_exact_drive_and_access_is_preserved(self):
        data = fields('p100', 'cFinder', 'u501')
        data += fields('f14', 'ar', 'tDIR', 'n' + self.root + '/Raw')
        data += fields('f15', 'au', 'tREG', 'n' + self.root + '/private file.jpg')
        data += fields('f16', 'ar', 'tREG', 'n' + self.root + '-other/not-this-drive')
        entries, truncated = open_files.parse(data, self.root, self.lock)
        self.assertFalse(truncated)
        self.assertEqual(len(entries), 2)
        self.assertEqual({e['access'] for e in entries}, {'Read only', 'Read & write'})
        self.assertEqual({e['relativePath'] for e in entries}, {'Raw', 'private file.jpg'})

    def test_background_service_is_identified_without_reading_arguments_or_credentials(self):
        data = fields('p123', 'cPython', 'u501')
        data += fields('f7', 'ar', 'tDIR', 'n' + self.root + '/Raw')
        # Lock can occur after the held file in lsof output.
        data += fields('f8', 'au', 'tREG', 'n' + self.lock)
        entries, _ = open_files.parse(data, self.root, self.lock)
        self.assertEqual(entries[0]['owner'], 'Mountain Turtle background service')

    def test_repeated_handles_cwd_and_newlines_are_parsed_as_fields(self):
        data = fields('p10', 'cEditor')
        data += fields('fcwd', 'tDIR', 'n' + self.root)
        data += fields('f1', 'ar', 'tREG', 'n' + self.root + '/a\nname')
        data += fields('f2', 'ar', 'tREG', 'n' + self.root + '/a\nname')
        entries, _ = open_files.parse(data, self.root, self.lock)
        self.assertEqual(len(entries), 2)
        self.assertTrue(any(e['descriptor'] == 'cwd' and e['relativePath'] == '/' for e in entries))
        self.assertTrue(any(e['handleCount'] == 2 and e['relativePath'] == 'a name' for e in entries))

    def test_scan_limits_and_missing_visibility_do_not_claim_drive_is_safe_to_eject(self):
        result = open_files.snapshot(self.paths, self.connection, scanner=lambda: (b'', True))
        self.assertTrue(result['partial'])
        self.assertEqual(result['entries'], [])
        self.assertIn('missing', result['message'])
        data = fields('p1', 'cFinder') + b''.join(fields('f'+str(i), 'ar', 'tREG', 'n'+self.root+'/'+str(i)) for i in range(220))
        entries, truncated = open_files.parse(data, self.root, self.lock)
        self.assertTrue(truncated)
        self.assertEqual(len(entries), open_files.MAX_ROWS)

    def test_unavailable_inspection_is_reported_without_changing_files(self):
        def unavailable():
            raise OSError('unavailable')
        result = open_files.snapshot(self.paths, self.connection, scanner=unavailable)
        self.assertTrue(result['partial'])
        self.assertIn('unavailable', result['message'])
        self.assertFalse(self.paths.base.exists())

    def test_scanner_bounds_output_and_terminates_its_own_timed_out_process(self):
        original = subprocess.Popen
        children = []
        def spawn(command, **kwargs):
            self.assertIn('-b', command)
            self.assertNotIn('+D', command)
            self.assertNotIn(self.root, command)
            child = original([sys.executable, '-c', 'import sys,time;sys.stdout.write("x"*200);sys.stdout.flush();time.sleep(60)'], **kwargs)
            children.append(child)
            return child
        with patch.object(open_files.subprocess, 'Popen', side_effect=spawn), patch.object(open_files, 'MAX_BYTES', 32):
            data, partial = open_files.scan(seconds=1)
        self.assertTrue(partial)
        self.assertLessEqual(len(data), 32)
        self.assertIsNotNone(children[0].poll())
        with patch.object(open_files.subprocess, 'Popen', side_effect=spawn):
            began = time.monotonic()
            data, partial = open_files.scan(seconds=.1)
        self.assertTrue(partial)
        self.assertLess(time.monotonic() - began, 3)
        self.assertIsNotNone(children[-1].poll())
