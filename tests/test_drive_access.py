import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "service/turtle_service.py"
spec = importlib.util.spec_from_file_location("access_turtle", SOURCE)
turtle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turtle)


class DriveAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = turtle.Paths(home=temporary.name)
        self.paths.prepare()
        self.store = turtle.Store(self.paths)
        self.connection = dict(id="bcb0102c-753e-425a-9391-f0e9936247a1", name="Archive",
            bucket="sample-photo-archive", profile="archive-reader", region="us-east-1",
            readOnly=False, autoConnect=True, desiredConnected=False, revision=4, updatedAt=1,
            cacheMaxSizeMiB=512, cacheMaxAgeHours=6)
        self.save(self.connection)
        mounts = patch.object(turtle, "mount_table", return_value=set())
        self.mounts = mounts.start()
        self.addCleanup(mounts.stop)

    def save(self, connection):
        with self.store.update() as state:
            state["connections"] = [dict(connection)]

    def saved(self):
        return self.store.read()["connections"][0]

    def run_access(self, read_only):
        arguments = turtle.parser().parse_args([
            "access", self.connection["id"], "--read-only" if read_only else "--read-write"])
        return turtle.action(arguments, self.paths)

    def cache_metadata(self, content):
        path = self.paths.cache / self.connection["id"] / "vfsMeta/volume/photo.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def backend_connection(self, backend, **fields):
        connection = dict(self.connection, backend=backend, **fields)
        if backend == "sftp":
            connection.update(bucket="", profile="", region="", host="files.example.com",
                user="archivist", port=2222, remotePath="/photos", authMode="password",
                passwordConfigured=True, keyFile="", knownHostsFile="/saved/known_hosts")
        return connection

    def test_access_requires_one_explicit_mode(self):
        for flags in ([], ["--read-only", "--read-write"]):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                turtle.parser().parse_args(["access", self.connection["id"], *flags])
        self.assertEqual(self.saved(), self.connection)

    def test_both_modes_preserve_identity_destination_authentication_and_cache_for_both_backends(self):
        cached = self.cache_metadata('{"Dirty":false}')
        for backend in ("s3", "sftp"):
            for requested in (True, False):
                with self.subTest(backend=backend, requested=requested):
                    original = self.backend_connection(backend, readOnly=not requested)
                    self.save(original)
                    with patch.object(turtle, "credential") as credential, \
                         patch.object(turtle, "ensure_service") as service:
                        self.assertTrue(self.run_access(requested)["ok"])
                    credential.assert_not_called()
                    service.assert_not_called()
                    saved = self.saved()
                    self.assertEqual(saved, dict(original, readOnly=requested,
                        revision=original["revision"] + 1, updatedAt=saved["updatedAt"]))
                    self.assertEqual(cached.read_text(), '{"Dirty":false}')
                    command = turtle.mount_command(saved, self.paths, "/rclone", "volume:")
                    self.assertEqual("--read-only" in command, requested)

    def test_unchanged_mode_does_not_disconnect_or_modify_active_dirty_drive(self):
        self.cache_metadata('{"Dirty":true}')
        for backend in ("s3", "sftp"):
            original = self.backend_connection(backend, desiredConnected=True)
            self.save(original)
            self.mounts.return_value = {str(self.paths.mounts / original["name"])}
            with self.subTest(backend=backend), patch.object(turtle, "ensure_service") as service:
                self.assertFalse(self.run_access(original["readOnly"])["changed"])
                service.assert_not_called()
                self.assertEqual(self.saved(), original)

    def test_active_mount_desired_connection_and_live_server_each_block_access_changes(self):
        for backend in ("s3", "sftp"):
            for active in ("mounted", "desired", "server"):
                with self.subTest(backend=backend, active=active):
                    original = self.backend_connection(backend, desiredConnected=active == "desired")
                    self.save(original)
                    self.mounts.return_value = ({str(self.paths.mounts / original["name"])}
                                               if active == "mounted" else set())
                    turtle.write_json(self.paths.base / "runtime.json", {
                        "connections": {original["id"]: {"pid": 4242}}})
                    with patch.object(turtle, "process_alive", return_value=active == "server"), \
                         self.assertRaisesRegex(ValueError, "Disconnect this drive"):
                        self.run_access(True)
                    self.assertEqual(self.saved(), original)

    def test_pending_or_ambiguous_cached_writes_keep_original_mode_even_if_previously_readonly(self):
        for backend in ("s3", "sftp"):
            for original_mode in (True, False):
                for metadata in ('{"Dirty":true}', '{"Dirty":'):
                    with self.subTest(backend=backend, mode=original_mode, metadata=metadata):
                        original = self.backend_connection(backend, readOnly=original_mode)
                        self.save(original)
                        cached = self.cache_metadata(metadata)
                        with self.assertRaisesRegex(ValueError, "pending cached changes"):
                            self.run_access(not original_mode)
                        self.assertEqual(self.saved(), original)
                        self.assertEqual(cached.read_text(), metadata)

    def test_failed_save_preserves_original_record_and_cache(self):
        cached = self.cache_metadata('{"Dirty":false}')
        for backend in ("s3", "sftp"):
            original = self.backend_connection(backend)
            self.save(original)
            with self.subTest(backend=backend), \
                 patch.object(turtle, "write_json", side_effect=OSError("Disk full")), \
                 self.assertRaises(OSError):
                self.run_access(True)
            self.assertEqual(self.saved(), original)
            self.assertEqual(cached.read_text(), '{"Dirty":false}')

    def test_unrelated_settings_and_edits_preserve_writable_mode(self):
        for command in ("settings", "rename", "edit"):
            with self.subTest(command=command):
                self.save(self.connection)
                extra = {"settings": ["--cache-max-size-mib", "1024"],
                         "rename": ["--name", "Renamed archive"],
                         "edit": ["--name", "Archive", "--bucket", self.connection["bucket"],
                                  "--profile", self.connection["profile"],
                                  "--region", self.connection["region"]]}[command]
                turtle.action(turtle.parser().parse_args([command, self.connection["id"], *extra]), self.paths)
                self.assertFalse(self.saved()["readOnly"])


if __name__ == "__main__":
    unittest.main()
