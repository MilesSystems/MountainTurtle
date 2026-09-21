import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
import setup_bundle
import turtle_service as turtle


class SetupBundleCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.keys.cleanup)
        private = Path(cls.keys.name) / "identity"
        subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "synthetic-setup-test",
                        "-f", str(private)], check=True, capture_output=True)
        cls.private_key = private.read_bytes()
        cls.public_key = private.with_suffix(".pub").read_bytes().split(b" ")[:2]
        cls.host_key = b" ".join(cls.public_key)

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.paths = turtle.Paths(home=home.name)
        self.connection = {"id": "original-connection", "name": "Family Photos", "backend": "sftp",
                           "host": "files.example.test", "user": "family", "port": 30224,
                           "remotePath": "/", "authMode": "keyFile", "readOnly": False,
                           "autoConnect": True, "desiredConnected": True, "revision": 8,
                           "cacheMaxSizeMiB": 1024, "cacheMaxAgeHours": 12,
                           "fastBrowsing": False}
        self.known_hosts = b"[files.example.test]:30224 " + self.host_key + b"\n"
        self.data = setup_bundle.encode(self.connection, self.private_key, self.known_hosts)

    def run_action(self, *arguments, data=b""):
        stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
        with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", io.StringIO()):
            return turtle.action(turtle.parser().parse_args(arguments), self.paths)

    @contextlib.contextmanager
    def no_mount_or_keychain(self):
        with contextlib.ExitStack() as stack:
            for name in ("credential", "forget_credential", "ensure_service", "mount_table", "set_autostart"):
                stack.enter_context(patch.object(turtle, name, side_effect=AssertionError(name + " called")))
            yield

    def save_source(self):
        source = self.paths.home / "selected-ssh-files"
        source.mkdir(mode=0o700)
        key = source / "private-key"
        known_hosts = source / "known_hosts"
        key.write_bytes(self.private_key)
        known_hosts.write_bytes(b"unrelated.example.test " + self.host_key + b"\n" + self.known_hosts)
        key.chmod(0o600)
        known_hosts.chmod(0o600)
        connection = dict(self.connection, keyFile=str(key), knownHostsFile=str(known_hosts))
        with turtle.Store(self.paths).update() as state:
            state["connections"].append(connection)
        return connection

    def test_export_and_import_between_homes_installs_private_keys_without_ssh_config(self):
        source = self.save_source()
        with self.no_mount_or_keychain():
            exported = self.run_action("export-setup", source["id"])
        exported_bytes = json.dumps(exported).encode()
        decoded = setup_bundle.decode(exported_bytes)
        self.assertEqual(decoded["privateKey"], self.private_key)
        self.assertEqual(decoded["knownHosts"], self.known_hosts)
        self.assertNotIn(str(self.paths.home), exported_bytes.decode())
        target = tempfile.TemporaryDirectory()
        self.addCleanup(target.cleanup)
        self.paths = turtle.Paths(home=target.name)
        with self.no_mount_or_keychain():
            result = self.run_action("import-setup", "--name", "My Photos", data=exported_bytes)
        self.assertTrue(result["ok"])
        self.assertNotEqual(result["id"], source["id"])
        saved = turtle.Store(self.paths).read()["connections"][0]
        self.assertEqual(saved["id"], result["id"])
        self.assertEqual(saved["name"], "My Photos")
        self.assertFalse(saved["autoConnect"])
        self.assertFalse(saved["desiredConnected"])
        self.assertFalse(saved["passwordConfigured"])
        self.assertEqual(saved["authMode"], "keyFile")
        self.assertEqual(saved["remotePath"], "/")
        self.assertEqual(saved["cacheMaxSizeMiB"], 1024)
        directory = self.paths.base / "credentials" / result["id"]
        self.assertEqual(Path(saved["keyFile"]), directory / "identity")
        self.assertEqual(Path(saved["knownHostsFile"]), directory / "known_hosts")
        for path in (directory, directory.parent):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path, content in ((Path(saved["keyFile"]), self.private_key),
                              (Path(saved["knownHostsFile"]), self.known_hosts)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.paths.home / ".ssh").exists())
        store_data = (self.paths.base / "connections.json").read_bytes()
        self.assertNotIn(b"PRIVATE KEY", store_data)
        self.assertNotIn(self.host_key, store_data)
        self.assertNotIn(b"privateKey", store_data)
        self.assertFalse(list(directory.parent.glob(".import-*")))

    def test_inspection_returns_only_portable_settings_and_has_no_persistent_side_effects(self):
        with self.no_mount_or_keychain(), patch.object(turtle, "Store", side_effect=AssertionError("Store called")):
            result = self.run_action("inspect-setup", data=self.data)
        self.assertEqual(set(result), {"ok", "connection"})
        self.assertEqual(result["connection"]["name"], "Family Photos")
        self.assertFalse(result["connection"]["autoConnect"])
        self.assertNotIn("privateKey", json.dumps(result))
        self.assertNotIn("keyFile", result["connection"])
        self.assertNotIn("knownHostsFile", result["connection"])
        self.assertEqual(list(self.paths.home.iterdir()), [])

    def test_bad_documents_and_names_write_no_files(self):
        for data in (b"{}", b"not-a-private-key-marker", b"x" * 65537):
            for command in ("inspect-setup", "import-setup"):
                arguments = [command] + (["--name", "Photos"] if command == "import-setup" else [])
                with self.subTest(command=command, data=data[:20]), self.no_mount_or_keychain():
                    with self.assertRaises(ValueError):
                        self.run_action(*arguments, data=data)
                self.assertEqual(list(self.paths.home.iterdir()), [])
        with self.assertRaises(ValueError):
            self.run_action("import-setup", "--name", "../not-valid", data=self.data)
        self.assertEqual(list(self.paths.home.iterdir()), [])

    def test_invalid_key_or_trust_is_rejected_before_managed_write(self):
        for field, data in (("privateKey", b"not-a-private-key"),
                            ("knownHosts", b"wrong.example.test " + self.host_key + b"\n")):
            document = json.loads(self.data)
            document[field] = base64.b64encode(data).decode()
            with self.subTest(field=field), \
                 patch.object(turtle.Paths, "prepare", side_effect=AssertionError("prepare called")):
                with self.assertRaises(ValueError):
                    self.run_action("import-setup", "--name", "Photos", data=json.dumps(document).encode())
            self.assertEqual(list(self.paths.home.iterdir()), [])

    def test_duplicate_names_leave_existing_record_and_keys_untouched(self):
        result = self.run_action("import-setup", "--name", "Family Photos", data=self.data)
        original = (self.paths.base / "connections.json").read_bytes()
        key = self.paths.base / "credentials" / result["id"] / "identity"
        with self.assertRaisesRegex(ValueError, "already uses this name"):
            self.run_action("import-setup", "--name", "family PHOTOS", data=self.data)
        self.assertEqual((self.paths.base / "connections.json").read_bytes(), original)
        self.assertEqual(key.read_bytes(), self.private_key)
        self.assertEqual(len(list(key.parent.parent.iterdir())), 1)

    def test_import_can_use_reviewed_name_to_resolve_duplicate(self):
        first = self.run_action("import-setup", "--name", "Family Photos", data=self.data)
        second = self.run_action("import-setup", "--name", "Family Photos 2", data=self.data)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(turtle.Store(self.paths).read()["connections"]), 2)

    def test_store_failure_removes_installed_credentials(self):
        with patch.object(turtle, "write_json", side_effect=OSError("synthetic save failure")):
            with self.assertRaises(ValueError):
                self.run_action("import-setup", "--name", "Photos", data=self.data)
        self.assertEqual(list((self.paths.base / "credentials").iterdir()), [])
        self.assertFalse((self.paths.base / "connections.json").exists())

    def test_rename_failure_cleans_staging_and_reservation(self):
        with patch.object(turtle.os, "rename", side_effect=OSError("synthetic rename failure")):
            with self.assertRaises(ValueError):
                self.run_action("import-setup", "--name", "Photos", data=self.data)
        self.assertEqual(list((self.paths.base / "credentials").iterdir()), [])
        self.assertFalse((self.paths.base / "connections.json").exists())

    def test_partial_file_write_removes_staging_credentials(self):
        with patch.object(turtle.os, "fsync", side_effect=OSError("synthetic partial write")):
            with self.assertRaises(ValueError):
                self.run_action("import-setup", "--name", "Photos", data=self.data)
        self.assertEqual(list((self.paths.base / "credentials").iterdir()), [])
        self.assertFalse((self.paths.base / "connections.json").exists())

    def test_existing_empty_credential_destination_is_preserved(self):
        directory = self.paths.base / "credentials" / "collision"
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)
        before = directory.stat().st_ino
        with patch.object(turtle.uuid, "uuid4", return_value="collision"):
            with self.assertRaisesRegex(ValueError, "already exists"):
                self.run_action("import-setup", "--name", "Photos", data=self.data)
        self.assertEqual(directory.stat().st_ino, before)
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(list(directory.parent.iterdir()), [directory])
        self.assertFalse((self.paths.base / "connections.json").exists())

    def test_existing_credential_destination_is_never_overwritten(self):
        turtle.Paths.prepare(self.paths)
        directory = self.paths.base / "credentials" / "collision"
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)
        original = directory / "identity"
        original.write_bytes(b"existing private key must survive")
        with patch.object(turtle.uuid, "uuid4", return_value="collision"):
            with self.assertRaisesRegex(ValueError, "already exists"):
                self.run_action("import-setup", "--name", "Photos", data=self.data)
        self.assertEqual(original.read_bytes(), b"existing private key must survive")
        self.assertEqual(list(directory.parent.iterdir()), [directory])
        self.assertFalse((self.paths.base / "connections.json").exists())

    def test_symlink_ancestor_and_store_are_rejected(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_path = Path(outside.name)
        target = outside_path / "target"
        target.mkdir()
        for relative in ("Library", "Library/Application Support", "Library/Application Support/Mountain Turtle",
                         "Library/Application Support/Mountain Turtle/credentials"):
            home = tempfile.TemporaryDirectory()
            self.addCleanup(home.cleanup)
            self.paths = turtle.Paths(home=home.name)
            link = self.paths.home / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=True)
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.run_action("import-setup", "--name", "Photos", data=self.data)
            self.assertEqual(list(target.iterdir()), [])
        for filename in ("state.lock", "connections.json", f"connections.json.{os.getpid()}.tmp"):
            home = tempfile.TemporaryDirectory()
            self.addCleanup(home.cleanup)
            self.paths = turtle.Paths(home=home.name)
            turtle.Paths.prepare(self.paths)
            file_target = outside_path / filename
            file_target.write_text('{"connections": []}')
            (self.paths.base / filename).symlink_to(file_target)
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.run_action("import-setup", "--name", "Photos", data=self.data)
            self.assertEqual(file_target.read_text(), '{"connections": []}')

    def test_export_refuses_terminal_display_and_incompatible_connections(self):
        self.save_source()
        output = io.StringIO()
        output.isatty = lambda: True
        with patch.object(sys, "stdout", output):
            with self.assertRaisesRegex(ValueError, "private key is not displayed"):
                turtle.action(turtle.parser().parse_args(["export-setup", self.connection["id"]]), self.paths)
        with turtle.Store(self.paths).update() as state:
            state["connections"][0]["authMode"] = "password"
        with self.assertRaisesRegex(ValueError, "private key file"):
            self.run_action("export-setup", self.connection["id"])

    def test_export_rejects_symlink_private_key_and_oversized_source(self):
        saved = self.save_source()
        key = Path(saved["keyFile"])
        real_key = key.with_name("actual-key")
        key.rename(real_key)
        key.symlink_to(real_key)
        with self.assertRaisesRegex(ValueError, "original file"):
            self.run_action("export-setup", self.connection["id"])
        key.unlink()
        key.write_bytes(b"x" * (setup_bundle.MAX_PRIVATE_KEY_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "too large"):
            self.run_action("export-setup", self.connection["id"])

    def test_inspection_and_import_bound_stdin(self):
        class BoundedInput(io.BytesIO):
            def read(self, size=-1):
                if size != 65537:
                    raise AssertionError("Unbounded setup input")
                return super().read(size)
        for command in ("inspect-setup", "import-setup"):
            stream = BoundedInput(b"x" * 100000)
            arguments = [command] + (["--name", "Photos"] if command == "import-setup" else [])
            with patch.object(sys, "stdin", io.TextIOWrapper(stream)):
                with self.assertRaises(ValueError):
                    turtle.action(turtle.parser().parse_args(arguments), self.paths)
                self.assertEqual(stream.tell(), 65537)
            self.assertEqual(list(self.paths.home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
