"""Regression checks for saved SFTP passwords crossing a server boundary."""
import io
import unittest
from unittest.mock import call, patch

import test_sftp as fixture


class SFTPCredentialBoundaryTests(unittest.TestCase):
    args = fixture.SFTPTests.args

    def setUp(self):
        fixture.SFTPTests.setUp(self)
        with self.store.update() as state:
            state["connections"][0].update(authMode="password", passwordConfigured=True)

    def test_changed_host_user_or_port_requires_fresh_password_without_mutating_profile(self):
        before = self.store.read()
        for field, value in (("--host", "another.example.com"), ("--user", "someone-else"), ("--port", "2222")):
            with self.subTest(field=field), patch.object(fixture.turtle, "credential") as credentials:
                with self.assertRaisesRegex(ValueError, "password"):
                    fixture.turtle.action(self.args("edit", "--auth-mode", "password", field, value), self.paths)
                self.assertEqual(self.store.read(), before)
                credentials.assert_not_called()

    def test_same_server_folder_edit_preserves_saved_password(self):
        with patch.object(fixture.turtle, "credential") as credentials:
            fixture.turtle.action(self.args("edit", "--auth-mode", "password", "--remote-path", "/another-folder"), self.paths)
        saved = self.store.read()["connections"][0]
        self.assertTrue(saved["passwordConfigured"])
        self.assertEqual(saved["remotePath"], "/another-folder")
        credentials.assert_not_called()

    def test_fresh_password_allows_explicit_new_server(self):
        with patch.object(fixture.turtle, "credential", return_value="old-server-secret") as credentials, \
             patch.object(fixture.turtle.sys, "stdin", io.StringIO("new-server-secret")):
            fixture.turtle.action(self.args("edit", "--auth-mode", "password", "--host", "another.example.com", "--password-stdin"), self.paths)
        saved = self.store.read()["connections"][0]
        self.assertEqual(saved["host"], "another.example.com")
        self.assertEqual(credentials.call_args_list, [call(self.paths, "get", saved["id"]),
            call(self.paths, "set", saved["id"], "new-server-secret")])
        self.assertNotIn("new-server-secret", str(saved))

    def test_failed_settings_write_restores_previous_password_and_server(self):
        before = self.store.read()
        with patch.object(fixture.turtle, "credential", return_value="old-server-secret") as credentials, \
             patch.object(fixture.turtle, "write_json", side_effect=OSError("disk full")), \
             patch.object(fixture.turtle.sys, "stdin", io.StringIO("new-server-secret")), self.assertRaises(OSError):
            fixture.turtle.action(self.args("edit", "--auth-mode", "password", "--host", "another.example.com", "--password-stdin"), self.paths)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(credentials.call_args_list, [call(self.paths, "get", self.connection["id"]),
            call(self.paths, "set", self.connection["id"], "new-server-secret"),
            call(self.paths, "set", self.connection["id"], "old-server-secret")])

    def test_failed_new_settings_write_removes_unreferenced_password(self):
        before = self.store.read()
        with patch.object(fixture.turtle, "credential", return_value="") as credentials, \
             patch.object(fixture.turtle, "write_json", side_effect=OSError("disk full")), \
             patch.object(fixture.turtle.sys, "stdin", io.StringIO("new-server-secret")), self.assertRaises(OSError):
            fixture.turtle.action(self.args("add", "--auth-mode", "password", "--password-stdin"), self.paths)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(credentials.call_count, 2)
        identity = credentials.call_args_list[0].args[2]
        self.assertEqual(credentials.call_args_list, [call(self.paths, "set", identity, "new-server-secret"),
                                                     call(self.paths, "delete", identity)])


if __name__ == "__main__":
    unittest.main()
