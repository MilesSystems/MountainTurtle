import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "service/turtle_service.py"
spec = importlib.util.spec_from_file_location("turtle", SOURCE)
turtle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turtle)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name, resources=Path(self.temp.name) / "Resources")
        self.paths.prepare()
        self.store = turtle.Store(self.paths)
        self.connection = {"id": "a24e79af-7ae2-46d0-b7b3-2bfc101002fd", "name": "Sample Photos",
                           "bucket": "real-photos-bucket", "profile": "production", "region": "us-east-1",
                           "readOnly": False, "autoConnect": True, "desiredConnected": False,
                           "revision": 1, "updatedAt": time.time()}
        with self.store.update() as state:
            state["connections"].append(dict(self.connection))

    def args(self, *arguments):
        return turtle.parser().parse_args(list(arguments))

    def cache_metadata(self, content):
        path = self.paths.cache / self.connection["id"] / "vfsMeta/volume/photo.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_explicit_disconnect_is_not_undone_by_autoconnect(self):
        supervisor = turtle.Supervisor(self.paths)
        with patch.object(turtle, "mount_table", return_value=set()), \
             patch.object(supervisor, "start_mount") as start:
            supervisor.tick()
        start.assert_not_called()
        self.assertFalse(self.store.read()["connections"][0]["desiredConnected"])
        self.assertEqual(supervisor.runtime[self.connection["id"]]["state"], "disconnected")

    def test_dirty_cached_uploads_block_ejection_and_process_termination(self):
        self.cache_metadata('{"Dirty":true}')
        supervisor = turtle.Supervisor(self.paths)
        child = Mock()
        child.pid, child.poll.return_value = 12345, None
        supervisor.children[self.connection["id"]] = {"process": child, "started": time.time() - 300}
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen") as start:
            supervisor.tick()
        start.assert_not_called()
        child.terminate.assert_not_called()
        self.assertIn("pending S3 uploads", supervisor.runtime[self.connection["id"]]["message"])

    def test_incomplete_cache_metadata_is_not_upload_completion_proof(self):
        path = self.cache_metadata('{"Dirty":')
        self.assertTrue(turtle.pending_writes(self.connection, self.paths))
        path.write_text('{"Dirty":false}')
        self.assertFalse(turtle.pending_writes(self.connection, self.paths))

    def test_native_unmount_never_forces_detach(self):
        supervisor = turtle.Supervisor(self.paths)
        with patch.object(turtle.subprocess, "Popen") as start:
            supervisor.eject(self.connection)
        self.assertEqual(start.call_args.args[0],
                         ["/sbin/umount", str(self.paths.mounts / self.connection["name"])])
        self.assertNotIn("-f", start.call_args.args[0])

    def test_busy_native_unmount_preserves_connection_until_next_request(self):
        supervisor = turtle.Supervisor(self.paths)
        child, eject = Mock(), Mock()
        child.pid, child.poll.return_value = 12345, None
        eject.poll.return_value = 1
        supervisor.children[self.connection["id"]] = {"process": child, "started": time.time()}
        supervisor.ejections[self.connection["id"]] = {"process": eject, "started": time.time(), "revision": 1}
        mounted = {str(self.paths.mounts / self.connection["name"])}
        with patch.object(turtle, "mount_table", return_value=mounted), \
             patch.object(turtle.subprocess, "Popen") as start:
            supervisor.tick()
            supervisor.tick()
        child.terminate.assert_not_called()
        start.assert_not_called()
        self.assertIn("busy", supervisor.runtime[self.connection["id"]]["message"])
        # A busy eject must not loop, but a fresh user request must be retryable.
        with self.store.update() as state:
            state["connections"][0]["revision"] += 1
        with patch.object(turtle, "mount_table", return_value=mounted), \
             patch.object(turtle.subprocess, "Popen") as retry:
            supervisor.tick()
        retry.assert_called_once()
        self.assertEqual(retry.call_args.args[0],
                         ["/sbin/umount", str(self.paths.mounts / self.connection["name"])])
        child.terminate.assert_not_called()

    def test_failed_process_waits_for_backoff_before_retry(self):
        with self.store.update() as state:
            state["connections"][0]["desiredConnected"] = True
        supervisor, failed = turtle.Supervisor(self.paths), Mock()
        failed.poll.return_value = 1
        supervisor.children[self.connection["id"]] = {"process": failed, "started": time.time()}
        with patch.object(turtle, "mount_table", return_value=set()), \
             patch.object(supervisor, "start_mount") as start:
            supervisor.tick()
            supervisor.tick()
        start.assert_not_called()
        self.assertGreater(supervisor.retry[self.connection["id"]], time.time())

    def test_connection_name_cannot_escape_mount_root(self):
        for name in ("../outside", "..", "/tmp/outside", "hidden\nname", "foo:bar", ".hidden"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                turtle.validate_fields(name, "valid-bucket", "production", "us-east-1")

    def test_duplicate_names_ignore_case(self):
        with patch.object(turtle, "mount_table", return_value=set()), self.assertRaises(ValueError):
            turtle.action(self.args("add", "--name", "sample photos", "--bucket", "another-bucket",
                                    "--profile", "production", "--region", "us-east-1"), self.paths)

    def test_edit_rejects_connection_that_is_still_desired(self):
        with self.store.update() as state:
            state["connections"][0]["desiredConnected"] = True
        with patch.object(turtle, "mount_table", return_value=set()), self.assertRaises(ValueError):
            turtle.action(self.args("edit", self.connection["id"], "--name", "New Name", "--bucket", "another-bucket",
                                    "--profile", "production", "--region", "us-east-1"), self.paths)

    def test_remove_only_removes_saved_connection_and_keeps_cache(self):
        cache = self.cache_metadata('{"Dirty":false}')
        with patch.object(turtle, "mount_table", return_value=set()):
            result = turtle.action(self.args("remove", self.connection["id"]), self.paths)
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.read()["connections"], [])
        self.assertTrue(cache.exists())

    def test_native_profile_config_has_no_copied_credentials(self):
        config, remote = turtle.connection_config(self.connection, self.paths)
        self.assertIn("env_auth = true", config)
        self.assertIn("profile = production", config)
        self.assertNotIn("access_key", config)
        self.assertNotIn("credential_process", config)
        command = turtle.mount_command({**self.connection, "readOnly": True}, self.paths, "/rclone", remote)
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--addr") + 1], "127.0.0.1:0")
        self.assertNotIn("--vfs-refresh", command)
        self.assertNotIn("--vfs-used-is-size", command)

    def test_exported_keys_cannot_override_the_selected_profile(self):
        with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "stale", "AWS_SECRET_ACCESS_KEY": "stale",
                                     "AWS_SESSION_TOKEN": "stale", "RCLONE_S3_ACCESS_KEY_ID": "stale"}):
            env = turtle.environment("production", self.paths)
        self.assertEqual(env["AWS_PROFILE"], "production")
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)
        self.assertNotIn("AWS_SESSION_TOKEN", env)
        self.assertNotIn("RCLONE_S3_ACCESS_KEY_ID", env)
        self.assertEqual(env.get("HOME"), os.environ.get("HOME"))

    def test_disabling_startup_does_not_bootout_or_terminate_service(self):
        with patch.object(turtle.subprocess, "run", return_value=Mock(returncode=0)) as run:
            turtle.set_autostart(self.paths, False)
        self.assertEqual(run.call_args.args[0][1], "disable")
        self.assertNotIn("bootout", str(run.call_args))

    def test_invalid_cli_command_returns_one_json_error(self):
        result = subprocess.run([sys.executable, str(SOURCE), "unknown-command"],
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(result.stderr, "")

    def test_status_omits_internal_runtime_pid_and_credentials(self):
        turtle.write_json(self.paths.base / "runtime.json", {"connections": {
            self.connection["id"]: {"state": "disconnected", "pid": 999, "secret": "not-a-real-token"}}})
        with patch.object(turtle, "mount_table", return_value=set()), \
             patch.object(turtle, "dependencies", return_value={"rclone": None, "aws": None, "python": sys.executable}):
            result = turtle.status(self.paths)
        self.assertNotIn("not-a-real-token", json.dumps(result))
        self.assertNotIn("pid", result["connections"][0])

    def test_finder_eject_stays_disconnected_even_with_autoconnect(self):
        with self.store.update() as state:
            state["connections"][0]["desiredConnected"] = True
        supervisor, process = turtle.Supervisor(self.paths), Mock()
        process.pid, process.poll.return_value = 12345, 0
        supervisor.children[self.connection["id"]] = {"process": process, "started": time.time(), "seenMounted": True}
        with patch.object(turtle, "mount_table", return_value=set()), patch.object(supervisor, "start_mount") as start:
            supervisor.tick()
        start.assert_not_called()
        self.assertFalse(self.store.read()["connections"][0]["desiredConnected"])
        self.assertTrue(self.store.read()["connections"][0]["autoConnect"])

    def test_disconnect_starts_supervision_for_an_orphan_mount(self):
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle, "ensure_service") as ensure:
            turtle.action(self.args("disconnect", self.connection["id"]), self.paths)
        ensure.assert_called_once_with(self.paths)

    def test_shutdown_starts_supervision_for_an_orphan_mount(self):
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle, "service_running", return_value=False), \
             patch.object(turtle, "ensure_service") as ensure:
            result = turtle.action(self.args("shutdown"), self.paths)
        ensure.assert_called_once_with(self.paths)
        self.assertIn(self.connection["name"], result["pendingMounts"])

    def test_upload_that_becomes_dirty_during_eject_is_not_terminated(self):
        self.cache_metadata('{"Dirty":true}')
        supervisor, child, eject = turtle.Supervisor(self.paths), Mock(), Mock()
        child.pid, child.poll.return_value = 12345, None
        eject.poll.return_value = 0
        supervisor.children[self.connection["id"]] = {"process": child, "started": time.time()}
        supervisor.ejections[self.connection["id"]] = {"process": eject, "started": time.time(), "revision": 1}
        with patch.object(turtle, "mount_table", return_value=set()):
            supervisor.tick()
        child.terminate.assert_not_called()
        self.assertIn("pending S3 uploads", supervisor.runtime[self.connection["id"]]["message"])

    def test_login_has_a_friendly_bounded_timeout(self):
        with patch.object(turtle, "executable", return_value="/aws"), \
             patch.object(turtle.subprocess, "run", side_effect=subprocess.TimeoutExpired("aws", 300)) as run:
            with self.assertRaisesRegex(ValueError, "five minutes"):
                turtle.action(self.args("login", self.connection["id"]), self.paths)
        self.assertEqual(run.call_args.kwargs["timeout"], 300)


if __name__ == "__main__":
    unittest.main()
