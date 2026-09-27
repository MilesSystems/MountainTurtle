import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import file_browser
import turtle_service as turtle


class FileBrowserTests(unittest.TestCase):
    def test_paths_stay_inside_selected_drive(self):
        for path in ('/secret', '../secret', 'folder/../secret', 'a//b', './a', 'a/\0b'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                file_browser.relative_directory(path)
        for path in ('', 'photos/Raw', 'Family photos/日本語', 'a:ro'):
            self.assertEqual(file_browser.relative_directory(path), path)

    def test_listing_uses_private_config_without_touching_mount_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            resources = root / 'Contents/Resources'
            resources.mkdir(parents=True)
            helpers = resources.parent / 'Helpers'
            helpers.mkdir()
            (helpers / 'turtle-rclone').touch()
            paths = turtle.Paths(home=root, resources=resources)
            config = root / 'request.conf'
            connection = {'id': 'test'}
            with patch.object(turtle, 'connection_config', return_value=('[volume]\ntype=union\n', 'volume:')):
                command = file_browser.listing_command(paths, connection, 'photos/Raw', config)
            self.assertEqual(command[:3], [str(helpers / 'turtle-rclone'), 'tree-list', 'volume:photos/Raw'])
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertFalse(paths.remotes.exists())
            self.assertIn('--use-server-modtime', command)

    def test_browser_supports_direct_remote_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'Helpers').mkdir()
            (root / 'Helpers/turtle-rclone').touch()
            paths = turtle.Paths(home=root, resources=root / 'Resources')
            for remote, expected in [('sftp:/photos/', 'sftp:/photos/Raw'), ('s3:bucket', 's3:bucket/Raw')]:
                with patch.object(turtle, 'connection_config', return_value=('', remote)):
                    command = file_browser.listing_command(paths, {}, 'Raw', root / 'request.conf')
                    self.assertEqual(command[2], expected)

    def test_packaged_mount_engine_is_selected_without_changing_cache_or_auth_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            paths = turtle.Paths(home=root, resources=root / 'Resources')
            connection = dict(id='test', name='Test', backend='sftp', readOnly=False)
            previous = turtle.mount_command(connection, paths, '/external/rclone', 'volume:')
            (root / 'Helpers').mkdir()
            engine = root / 'Helpers/turtle-rclone'
            engine.touch()
            command = turtle.mount_command(connection, paths, '/external/rclone', 'volume:')
            self.assertEqual(command[0], str(engine))
            self.assertEqual(command[1:], previous[1:])
