import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
import turtle_service as turtle


class ConnectionTransferCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = turtle.Paths(home=temporary.name)
        self.store = turtle.Store(self.paths)
        self.connection = {
            "id": "d1b777a8-a734-496d-8fd4-a6e7dd0b3c96", "name": "Photo archive",
            "backend": "s3", "bucket": "photo-archive", "profile": "archive-reader",
            "region": "us-west-2", "readOnly": True, "autoConnect": True,
            "desiredConnected": True, "revision": 9, "updatedAt": 123,
            "cacheMaxSizeMiB": 1024, "cacheMaxAgeHours": 12, "fastBrowsing": True,
        }

    def save(self, connection=None):
        with self.store.update() as state:
            state["connections"].append(connection or self.connection)
        turtle.write_json(self.paths.base / "runtime.json", {
            "connections": {self.connection["id"]: {"state": "connected", "rcPass": "runtime-secret"}}
        })
        cache = self.paths.cache / "keep-me"
        cache.write_bytes(b"cached content")

    def snapshot(self):
        return {str(path.relative_to(self.paths.home)): path.read_bytes() if path.is_file() else None
                for path in self.paths.home.rglob("*")}

    @contextlib.contextmanager
    def forbid_side_effects(self):
        with contextlib.ExitStack() as stack:
            for owner, name in ((turtle.Paths, "prepare"), (turtle.Store, "update"),
                                (turtle, "mount_table"), (turtle, "credential"),
                                (turtle, "ensure_service"), (turtle, "set_autostart"),
                                (turtle.subprocess, "run"), (turtle.subprocess, "Popen")):
                stack.enter_context(patch.object(owner, name, side_effect=AssertionError(name + " called")))
            yield

    def run_main(self, *arguments, data=b""):
        stdout = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
        code = 0
        with patch.object(sys, "argv", [str(SERVICE / "turtle_service.py"), *arguments]), \
             patch.object(sys, "stdin", stdin), patch.object(turtle.Path, "home", return_value=self.paths.home), \
             patch.object(turtle.os, "umask"), contextlib.redirect_stdout(stdout):
            try:
                turtle.main()
            except SystemExit as error:
                code = error.code
        output = stdout.getvalue()
        self.assertEqual(len(output.splitlines()), 1)
        return code, json.loads(output)

    def test_export_stdout_is_portable_document_without_mutating_active_connection(self):
        self.save()
        before = self.snapshot()
        with self.forbid_side_effects():
            code, document = self.run_main("export-connection", self.connection["id"])
        self.assertEqual(code, 0)
        self.assertEqual(document["format"], "io.mountainturtle.connection")
        self.assertEqual(document["version"], 1)
        self.assertNotIn("ok", document)
        self.assertEqual(document["connection"]["bucket"], self.connection["bucket"])
        self.assertFalse(document["connection"]["autoConnect"])
        for field in ("id", "revision", "updatedAt", "desiredConnected", "rcPass"):
            self.assertNotIn(field, document["connection"])
        self.assertEqual(self.snapshot(), before)

    def test_sftp_export_omits_credentials_local_paths_and_runtime_fields(self):
        sftp = dict(self.connection, backend="sftp", host="files.example.test", user="photos", port=22,
                    remotePath="images", authMode="password", passwordConfigured=True,
                    password="never-export-this-secret", keyFile="/private/id_ed25519",
                    knownHostsFile="/private/known_hosts", mountPath="/private/drive")
        self.save(sftp)
        before = self.snapshot()
        with self.forbid_side_effects():
            code, document = self.run_main("export-connection", sftp["id"])
        self.assertEqual(code, 0)
        connection = document["connection"]
        self.assertEqual(connection["host"], sftp["host"])
        self.assertEqual(connection["authMode"], "password")
        for field in ("password", "passwordConfigured", "keyFile", "knownHostsFile", "mountPath", "bucket", "profile", "region"):
            self.assertNotIn(field, connection)
        self.assertNotIn("never-export-this-secret", json.dumps(document))
        self.assertEqual(self.snapshot(), before)

    def test_inspection_does_not_read_or_create_a_store(self):
        document = {"format": "io.mountainturtle.connection", "version": 1,
                    "connection": {key: self.connection[key] for key in (
                        "name", "backend", "bucket", "profile", "region", "readOnly", "autoConnect",
                        "cacheMaxSizeMiB", "cacheMaxAgeHours", "fastBrowsing")}}
        with self.forbid_side_effects(), patch.object(turtle, "Store", side_effect=AssertionError("Store called")):
            code, result = self.run_main("inspect-connection", data=json.dumps(document).encode())
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["connection"]["name"], self.connection["name"])
        self.assertTrue(result["connection"]["fastBrowsing"])
        self.assertFalse(result["connection"].get("autoConnect", False))
        self.assertFalse(result["connection"].get("desiredConnected", False))
        self.assertEqual(self.snapshot(), {})

    def test_inspection_keeps_existing_state_and_duplicate_names_unchanged(self):
        self.save()
        _, document = self.run_main("export-connection", self.connection["id"])
        before = self.snapshot()
        with self.forbid_side_effects():
            code, result = self.run_main("inspect-connection", data=json.dumps(document).encode())
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.snapshot(), before)

    def test_missing_export_returns_error_without_creating_state(self):
        with self.forbid_side_effects():
            code, result = self.run_main("export-connection", "missing")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("no longer exists", result["error"])
        self.assertEqual(self.snapshot(), {})

    def test_invalid_documents_return_json_errors_without_state_changes(self):
        self.save()
        before = self.snapshot()
        for data in (b"", b"not-json-private-marker", b"\xff", b"[]", b"{}", b"x" * 65537,
                     b'{"format":"io.mountainturtle.connection","version":999,"connection":{}}'):
            with self.subTest(data=data[:80]), self.forbid_side_effects():
                code, result = self.run_main("inspect-connection", data=data)
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertTrue(result["error"])
            self.assertNotIn("private-marker", result["error"])
            self.assertEqual(self.snapshot(), before)

    def test_inspection_reads_at_most_one_byte_beyond_limit(self):
        class BoundedInput(io.BytesIO):
            def read(self, size=-1):
                self.requested = size
                if size != 65537:
                    raise AssertionError("Unbounded connection input")
                return super().read(size)

        stream = BoundedInput(b"x" * 100000)
        stdin = io.TextIOWrapper(stream)
        with self.forbid_side_effects(), patch.object(sys, "stdin", stdin):
            with self.assertRaises(ValueError):
                turtle.action(turtle.parser().parse_args(["inspect-connection"]), self.paths)
        self.assertEqual(stream.requested, 65537)
        self.assertEqual(stream.tell(), 65537)
        self.assertEqual(self.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
