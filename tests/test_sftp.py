import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "service/turtle_service.py"
spec = importlib.util.spec_from_file_location("sftp_turtle", SOURCE)
turtle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turtle)


class SFTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = turtle.Paths(home=temporary.name, resources=Path(temporary.name) / "App/Contents/Resources")
        self.paths.prepare()
        self.hosts = self.paths.home / ".ssh/known_hosts"
        self.hosts.parent.mkdir()
        self.hosts.write_text("files.example.com ssh-ed25519 fixture\n")
        self.key = self.paths.home / ".ssh/id_ed25519"
        self.key.write_text("not a private key; test subprocesses are mocked")
        self.store = turtle.Store(self.paths)
        self.connection = dict(id="23833745-6c18-40f4-8ad0-11cf6ae04e50", name="Server files", backend="sftp",
            host="files.example.com", user="tester", port=22, remotePath="", authMode="agent", keyFile="",
            knownHostsFile=str(self.hosts), bucket="", profile="", region="", readOnly=True,
            autoConnect=False, desiredConnected=False, revision=1, updatedAt=time.time())
        with self.store.update() as state:
            state["connections"].append(dict(self.connection))
        self.patch_mounts = patch.object(turtle, "mount_table", return_value=set())
        self.patch_mounts.start()
        self.addCleanup(self.patch_mounts.stop)

    def args(self, operation="add", *extra):
        arguments = [operation]
        if operation == "edit":
            arguments.append(self.connection["id"])
        arguments += ["--backend", "sftp", "--name", "New server" if operation == "add" else self.connection["name"],
                      "--host", self.connection["host"], "--user", self.connection["user"]]
        return turtle.parser().parse_args(arguments + list(extra))

    def cache(self, dirty=False):
        root = self.paths.cache / self.connection["id"]
        metadata = root / "vfsMeta/volume/file.txt"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({"Dirty": dirty}))
        return root

    def icon_assets(self, backend):
        suffix = "-sftp" if backend == "sftp" else ""
        assets = self.paths.resources / ("icon-overlay-assets" + suffix)
        assets.mkdir(parents=True)
        for name in turtle.ICON_ASSETS:
            (assets / name).write_bytes(f"{backend}:{name}".encode())
        return assets

    def test_add_sftp_does_not_require_aws_and_defaults_to_read_only(self):
        with patch.object(turtle, "credential") as keychain:
            result = turtle.action(self.args(), self.paths)
        saved = turtle.find_connection(self.store.read(), result["id"])
        self.assertEqual(saved["backend"], "sftp")
        self.assertTrue(saved["readOnly"])
        self.assertFalse(saved["desiredConnected"])
        self.assertEqual(saved["knownHostsFile"], str(self.hosts))
        self.assertEqual([saved[k] for k in ("bucket", "profile", "region")], ["", "", ""])
        keychain.assert_not_called()

    def test_writing_requires_explicit_flag(self):
        result = turtle.action(self.args("add", "--read-write"), self.paths)
        self.assertFalse(turtle.find_connection(self.store.read(), result["id"])["readOnly"])
        with self.assertRaises(ValueError):
            self.args("add", "--read-only", "--read-write")

    def test_legacy_s3_status_and_config_still_work(self):
        old = dict(self.connection, bucket="photos-bucket", profile="production", region="us-east-1")
        old.pop("backend")
        with self.store.update() as state:
            state["connections"] = [old]
        with patch.object(turtle, "dependencies", return_value={}):
            result = turtle.status(self.paths)["connections"][0]
        self.assertEqual(result["backend"], "s3")
        config, remote = turtle.connection_config(old, self.paths)
        self.assertEqual(remote, "s3:photos-bucket")
        self.assertIn("profile = production", config)

    def test_host_user_port_and_path_reject_config_injection(self):
        invalid = [("host", "server\npass=secret"), ("host", "::1%scope\npass=secret"), ("host", "sftp://example.com"), ("host", "host:22"),
                   ("host", ""), ("user", "foo\nkey_file=/other"), ("user", "a b"),
                   ("port", 0), ("port", 65536), ("port", True),
                   ("remotePath", "/a\n[remote]"), ("remotePath", "../other"), ("remotePath", "~/photos")]
        for key, value in invalid:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                turtle.validate_sftp(dict(self.connection, **{key: value}), self.paths)
        self.assertEqual(turtle.validate_sftp(dict(self.connection, host="2001:db8::1"), self.paths)["host"], "2001:db8::1")

    def test_known_hosts_is_required_and_cannot_disable_verification(self):
        for path in ("none", "/does/not/exist", "${HOME}/.ssh/known_hosts", str(self.hosts.parent), "/tmp/a\npass=x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                turtle.validate_sftp(dict(self.connection, knownHostsFile=path), self.paths)
        self.hosts.unlink()
        with self.assertRaisesRegex(ValueError, "known hosts"):
            turtle.connection_config(self.connection, self.paths)

    def test_private_key_file_is_required_only_in_key_mode(self):
        config, _ = turtle.connection_config(dict(self.connection, authMode="keyFile", keyFile=str(self.key)), self.paths)
        self.assertIn("key_file = " + str(self.key), config)
        self.assertIn("key_use_agent = false", config)
        with self.assertRaisesRegex(ValueError, "private key"):
            turtle.connection_config(dict(self.connection, authMode="keyFile", keyFile=""), self.paths)

    def test_config_prevents_remote_shell_commands_and_never_contains_password(self):
        for mode in ("agent", "password"):
            with self.subTest(mode=mode):
                config, remote = turtle.connection_config(dict(self.connection, authMode=mode, password="never-copy-me"), self.paths)
                self.assertEqual(remote, "sftp:")
                self.assertIn("shell_type = none", config)
                self.assertIn("disable_hashcheck = true", config)
                self.assertIn("use_insecure_cipher = false", config)
                self.assertIn("known_hosts_file = " + str(self.hosts), config)
                self.assertNotIn("never-copy-me", config)
                self.assertNotIn("pass =", config)

    def test_overlay_quotes_remote_folder_and_escapes_union_option_suffix(self):
        overlay = self.paths.base / "icon-overlay-sftp"
        for path in ('/Family Photos', '/Family "photos"', '/folder:ro'):
            with self.subTest(path=path), patch.object(turtle, "materialized_icon_overlay", return_value=overlay):
                config, remote = turtle.connection_config(dict(self.connection, remotePath=path), self.paths)
            self.assertEqual(remote, "volume:")
            escaped = path.replace('"', '""')
            self.assertIn('"sftp:' + escaped + '/"', config)

    def test_protocol_icons_have_distinct_upstreams_and_do_not_overwrite_each_other(self):
        self.icon_assets("s3")
        self.icon_assets("sftp")
        s3 = dict(self.connection, bucket="photos-bucket", profile="production", region="us-east-1")
        s3.pop("backend")
        for connection, backend, folder in ((s3, "s3", "icon-overlay"),
                                            (self.connection, "sftp", "icon-overlay-sftp"),
                                            (s3, "s3", "icon-overlay")):
            with self.subTest(backend=backend):
                config, remote = turtle.connection_config(connection, self.paths)
                overlay = self.paths.base / folder
                self.assertEqual(remote, "volume:")
                self.assertIn(f'upstreams = "{overlay}:ro"', config)
                for source, target in turtle.ICON_ASSETS.items():
                    self.assertEqual((overlay / target).read_bytes(), f"{backend}:{source}".encode())
        self.assertEqual((self.paths.base / "icon-overlay/.VolumeIcon.icns").read_bytes(), b"s3:VolumeIcon.icns")
        self.assertEqual((self.paths.base / "icon-overlay-sftp/.VolumeIcon.icns").read_bytes(), b"sftp:VolumeIcon.icns")

    def test_updated_sftp_artwork_refreshes_only_its_own_overlay(self):
        self.icon_assets("s3")
        assets = self.icon_assets("sftp")
        s3_overlay = turtle.materialized_icon_overlay(self.paths)
        sftp_overlay = turtle.materialized_icon_overlay(self.paths, "sftp")
        (assets / "VolumeIcon.icns").write_bytes(b"updated-sftp-icon")
        turtle.connection_config(self.connection, self.paths)
        self.assertEqual((sftp_overlay / ".VolumeIcon.icns").read_bytes(), b"updated-sftp-icon")
        self.assertEqual((s3_overlay / ".VolumeIcon.icns").read_bytes(), b"s3:VolumeIcon.icns")

    def test_missing_sftp_artwork_never_falls_back_to_s3(self):
        self.icon_assets("s3")
        s3_overlay = turtle.materialized_icon_overlay(self.paths)
        for missing in ("directory", "icon"):
            with self.subTest(missing=missing):
                if missing == "icon":
                    assets = self.icon_assets("sftp")
                    (assets / "VolumeIcon.icns").unlink()
                config, remote = turtle.connection_config(self.connection, self.paths)
                self.assertEqual(remote, "sftp:")
                self.assertNotIn("[volume]", config)
                self.assertNotIn(str(s3_overlay), config)
                self.assertFalse((self.paths.base / "icon-overlay-sftp").exists())
                self.assertEqual((s3_overlay / ".VolumeIcon.icns").read_bytes(), b"s3:VolumeIcon.icns")

    def test_password_is_saved_by_pipe_without_entering_state_or_arguments(self):
        secret = "test-only password @$ with spaces"
        with patch.object(turtle, "credential", return_value="") as keychain, patch.object(turtle.sys, "stdin", io.StringIO(secret)):
            result = turtle.action(self.args("add", "--auth-mode", "password", "--password-stdin"), self.paths)
        keychain.assert_called_once_with(self.paths, "set", result["id"], secret)
        saved = turtle.find_connection(self.store.read(), result["id"])
        self.assertTrue(saved["passwordConfigured"])
        self.assertNotIn(secret, (self.paths.base / "connections.json").read_text())
        self.assertNotIn("password", saved)

    def test_password_is_required_for_new_password_connection(self):
        with self.assertRaisesRegex(ValueError, "Enter an SFTP password"):
            turtle.action(self.args("add", "--auth-mode", "password"), self.paths)
        self.assertEqual(len(self.store.read()["connections"]), 1)

    def test_blank_edit_preserves_password_and_other_auth_clears_it_after_save(self):
        with self.store.update() as state:
            state["connections"][0].update(authMode="password", passwordConfigured=True)
        with patch.object(turtle, "credential") as keychain, patch.object(turtle.sys, "stdin", io.StringIO("")):
            turtle.action(self.args("edit", "--auth-mode", "password", "--password-stdin"), self.paths)
            keychain.assert_not_called()
            self.assertTrue(self.store.read()["connections"][0]["passwordConfigured"])
            turtle.action(self.args("edit", "--auth-mode", "agent"), self.paths)
        keychain.assert_called_once_with(self.paths, "delete", self.connection["id"])
        self.assertNotIn("passwordConfigured", self.store.read()["connections"][0])

    def test_keychain_save_failure_preserves_connection(self):
        before = self.store.read()
        with patch.object(turtle.sys, "stdin", io.StringIO("test-password")), \
             patch.object(turtle, "credential", side_effect=ValueError("Keychain unavailable")), self.assertRaises(ValueError):
            turtle.action(self.args("edit", "--auth-mode", "password", "--password-stdin"), self.paths)
        self.assertEqual(self.store.read(), before)

    def test_invalid_cache_settings_do_not_save_password(self):
        with patch.object(turtle, "credential") as keychain, patch.object(turtle.sys, "stdin", io.StringIO("secret")), self.assertRaises(ValueError):
            turtle.action(self.args("add", "--auth-mode", "password", "--password-stdin", "--cache-max-size-mib", "1"), self.paths)
        keychain.assert_not_called()

    def test_password_input_does_not_silently_truncate_multiline_passwords(self):
        for secret in ("secret\nignored", "secret\r", "secret\0", "a" * 16385):
            with self.subTest(secret_length=len(secret)), patch.object(turtle.sys, "stdin", io.StringIO(secret)), self.assertRaises(ValueError):
                turtle.password_input()

    def test_native_credential_helper_uses_stdin_and_sanitizes_errors(self):
        helper = self.paths.resources.parent / "Helpers/Mountain Turtle Credentials"
        helper.parent.mkdir(parents=True)
        helper.write_text("test helper")
        helper.chmod(0o700)
        with patch.object(turtle.subprocess, "run", return_value=Mock(returncode=0, stdout="")) as run:
            turtle.credential(self.paths, "set", self.connection["id"], "private")
        self.assertEqual(run.call_args.args[0], [str(helper), "set", self.connection["id"]])
        self.assertEqual(run.call_args.kwargs["input"], "private")
        with patch.object(turtle.subprocess, "run", return_value=Mock(returncode=1, stdout="secret", stderr="private")), self.assertRaises(ValueError) as error:
            turtle.credential(self.paths, "get", self.connection["id"])
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("private", str(error.exception))

    def test_password_mount_uses_obscure_stdin_and_private_child_environment(self):
        connection = dict(self.connection, authMode="password")
        with patch.dict(os.environ, {"RCLONE_CONFIG_SFTP_PASS": "stale", "RCLONE_SFTP_KNOWN_HOSTS_FILE": "none",
                                     "RCLONE_SFTP_SSH": "bad command", "AWS_ACCESS_KEY_ID": "unrelated"}), \
             patch.object(turtle, "credential", return_value="private") as keychain, \
             patch.object(turtle.subprocess, "run", return_value=Mock(returncode=0, stdout="obscured_token\n")) as run:
            env = turtle.mount_environment(connection, self.paths, "/rclone")
        self.assertEqual(run.call_args.args[0], ["/rclone", "obscure", "-"])
        self.assertEqual(run.call_args.kwargs["input"], "private\n")
        self.assertEqual(env["RCLONE_CONFIG_SFTP_PASS"], "obscured_token")
        self.assertNotIn("private", env.values())
        self.assertNotIn("RCLONE_SFTP_KNOWN_HOSTS_FILE", env)
        self.assertNotIn("RCLONE_SFTP_SSH", env)
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)
        self.assertNotIn("AWS_PROFILE", env)
        self.assertEqual(keychain.call_args.args, (self.paths, "get", self.connection["id"]))

    def test_failed_password_obscuring_never_echoes_provider_output(self):
        with patch.object(turtle, "credential", return_value="private"), \
             patch.object(turtle.subprocess, "run", return_value=Mock(returncode=1, stdout="private", stderr="private")), self.assertRaises(ValueError) as error:
            turtle.mount_environment(dict(self.connection, authMode="password"), self.paths, "/rclone")
        self.assertNotIn("private", str(error.exception))

    def test_sftp_login_never_invokes_aws(self):
        args = turtle.parser().parse_args(["login", self.connection["id"]])
        with patch.object(turtle.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "SFTP uses"):
            turtle.action(args, self.paths)
        run.assert_not_called()

    def test_sftp_rename_preserves_remote_and_cache(self):
        root = self.cache()
        turtle.action(turtle.parser().parse_args(["rename", self.connection["id"], "--name", "Renamed"]), self.paths)
        saved = self.store.read()["connections"][0]
        self.assertEqual(saved["name"], "Renamed")
        self.assertEqual(saved["host"], self.connection["host"])
        self.assertTrue(root.exists())

    def test_changing_server_clears_clean_cache_without_changing_identity(self):
        root = self.cache()
        turtle.action(self.args("edit", "--host", "other.example.com"), self.paths)
        saved = self.store.read()["connections"][0]
        self.assertFalse(root.exists())
        self.assertEqual(saved["id"], self.connection["id"])
        self.assertEqual(saved["host"], "other.example.com")

    def test_pending_writes_block_remote_edits_and_cache_clear(self):
        root = self.cache(dirty=True)
        with self.assertRaisesRegex(ValueError, "pending cached changes"):
            turtle.action(self.args("edit", "--host", "other.example.com"), self.paths)
        self.assertTrue(root.exists())
        self.assertEqual(self.store.read()["connections"][0], self.connection)

    def test_keychain_delete_failure_does_not_undo_local_removal(self):
        with self.store.update() as state:
            state["connections"][0]["passwordConfigured"] = True
        with patch.object(turtle, "credential", side_effect=ValueError("locked")), self.assertLogs(level="WARNING"):
            turtle.action(turtle.parser().parse_args(["remove", self.connection["id"]]), self.paths)
        self.assertEqual(self.store.read()["connections"], [])

    def test_host_key_and_authentication_errors_are_actionable_and_sanitized(self):
        log = self.paths.logs / (self.connection["id"] + ".log")
        for raw, expected in (("ERROR : knownhosts: key mismatch private", "server identity"),
                              ("ERROR : ssh: unable to authenticate private", "authentication failed")):
            log.write_text(raw + "\n")
            error = turtle.tail_error(self.connection, self.paths)
            self.assertIn(expected, error)
            self.assertNotIn("private", error)

    def test_rc_authentication_is_private_and_survives_status_recording(self):
        with self.store.update() as state:
            state["connections"][0]["desiredConnected"] = True
        connection = self.store.read()["connections"][0]
        rc = {"rcPort": 42500, "rcUser": "metrics-private", "rcPass": "rc-secret", "sessionID": "mount-generation"}
        process = Mock(pid=12345)
        supervisor = turtle.Supervisor(self.paths)
        with patch.object(turtle, "dependencies", return_value={"rclone": "/rclone"}), \
             patch.object(turtle, "remote_control_settings", return_value=rc), \
             patch.object(turtle.subprocess, "Popen", return_value=process) as start:
            supervisor.start_mount(connection)
        command = start.call_args.args[0]
        env = start.call_args.kwargs["env"]
        self.assertTrue(start.call_args.kwargs["start_new_session"])
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--rc-addr") + 1], "127.0.0.1:42500")
        self.assertEqual(env["RCLONE_RC_USER"], rc["rcUser"])
        self.assertEqual(env["RCLONE_RC_PASS"], rc["rcPass"])
        self.assertNotIn(rc["rcPass"], command)
        self.assertNotIn(rc["rcPass"], (self.paths.remotes / (connection["id"] + ".conf")).read_text())
        supervisor.record(connection, "connected", pid=process.pid)
        supervisor.publish()
        self.assertEqual(self.store.runtime()["connections"][connection["id"]]["rcPass"], rc["rcPass"])
        with patch.object(turtle, "dependencies", return_value={}), patch.object(turtle, "service_running", return_value=True):
            public = turtle.status(self.paths)
        for secret in (rc["rcPass"], rc["rcUser"], rc["sessionID"]):
            self.assertNotIn(secret, json.dumps(public))


if __name__ == "__main__":
    unittest.main()
