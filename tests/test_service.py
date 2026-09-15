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

    def sidebar_fixture(self):
        helper = self.paths.resources.parent / "Helpers/Mountain Turtle Sidebar"
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("local test fixture; subprocess is mocked")
        helper.chmod(0o700)
        with self.store.update() as state:
            state["connections"][0]["desiredConnected"] = True
        supervisor, child, process = turtle.Supervisor(self.paths), Mock(), Mock()
        child.pid, child.poll.return_value = 12345, None
        process.poll.return_value = None
        process.returncode = 0
        process.communicate.return_value = ('{"ok":true,"itemID":42,"replacedIDs":[]}', "")
        supervisor.children[self.connection["id"]] = {"process": child, "started": time.time()}
        return supervisor, child, process

    def test_sidebar_runs_once_after_mount_confirmation_and_records_native_item(self):
        supervisor, child, process = self.sidebar_fixture()
        mounted = {str(self.paths.mounts / self.connection["name"])}
        with patch.object(turtle, "mount_table", return_value=set()) as mounts, \
             patch.object(turtle.subprocess, "Popen", return_value=process) as start:
            supervisor.tick()
            start.assert_not_called()
            mounts.return_value = mounted
            supervisor.tick()
            supervisor.tick()
            start.assert_called_once()
            self.assertEqual(start.call_args.args[0][1:],
                             ["ensure", self.connection["id"], str(self.paths.mounts / self.connection["name"])])
            process.poll.return_value = 0
            supervisor.tick()
            supervisor.tick()
            start.assert_called_once()  # Removing the sidebar item now must stay respected.
        self.assertEqual(supervisor.runtime[self.connection["id"]]["sidebarItemID"], 42)
        child.terminate.assert_not_called()

    def test_sidebar_runs_again_for_a_new_mount_generation(self):
        supervisor, child, process = self.sidebar_fixture()
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen", return_value=process) as start:
            supervisor.tick()
            process.poll.return_value = 0
            supervisor.tick()
            replacement = Mock()
            replacement.pid, replacement.poll.return_value = 12346, None
            supervisor.children[self.connection["id"]] = {"process": replacement, "started": time.time()}
            supervisor.tick()
            self.assertEqual(start.call_count, 2)

    def test_sidebar_helpers_are_serialized_across_connections(self):
        supervisor, _, process = self.sidebar_fixture()
        second = dict(self.connection, id="a44478b5-0c81-448f-a21e-883fe4a52ff8", name="Other Photos", desiredConnected=True)
        with self.store.update() as state:
            state["connections"].append(second)
        child = Mock()
        child.pid, child.poll.return_value = 12346, None
        supervisor.children[second["id"]] = {"process": child, "started": time.time()}
        mounted = {str(self.paths.mounts / item["name"]) for item in (self.connection, second)}
        with patch.object(turtle, "mount_table", return_value=mounted), \
             patch.object(turtle.subprocess, "Popen", return_value=process) as start:
            supervisor.tick()
            supervisor.tick()
            start.assert_called_once()
            self.assertFalse(supervisor.children[second["id"]].get("sidebarAttempted", False))
            process.poll.return_value = 0
            supervisor.tick()
            self.assertEqual(start.call_count, 2)
            self.assertEqual(start.call_args.args[0][2], second["id"])

    def test_adopted_mount_gets_one_sidebar_refresh(self):
        supervisor, _, process = self.sidebar_fixture()
        supervisor.children.clear()
        turtle.write_json(self.paths.base / "runtime.json", {
            "connections": {self.connection["id"]: {"pid": 12345}}})
        command = "/opt/homebrew/bin/rclone nfsmount volume: --config " + str(self.paths.remotes / (self.connection["id"] + ".conf"))
        with patch.object(turtle, "process_alive", return_value=True), \
             patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "run", return_value=Mock(stdout=command)), \
             patch.object(turtle.subprocess, "Popen", return_value=process) as start:
            supervisor.recover(self.store.read()["connections"])
            supervisor.tick()
            supervisor.tick()
            start.assert_called_once()

    def test_sidebar_timeout_cannot_eject_or_retry_a_healthy_mount(self):
        supervisor, child, process = self.sidebar_fixture()
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen", return_value=process) as start:
            supervisor.tick()
            supervisor.sidebar_processes[self.connection["id"]]["started"] = time.monotonic() - 61
            with self.assertLogs(level="WARNING"):
                supervisor.tick()
            supervisor.tick()
            start.assert_called_once()
        process.terminate.assert_called_once()
        child.terminate.assert_not_called()
        self.assertEqual(supervisor.runtime[self.connection["id"]]["state"], "connected")
        self.assertIn("timed out", supervisor.runtime[self.connection["id"]]["sidebarError"])
        for setting in ("Privacy & Security", "Files and Folders", "Mountain Turtle", "Network Volumes", "Add to Sidebar"):
            self.assertIn(setting, supervisor.runtime[self.connection["id"]]["sidebarError"])
        self.assertTrue(self.store.read()["connections"][0]["desiredConnected"])

    def test_sidebar_permission_prompt_has_a_minute_without_blocking_drive_supervision(self):
        supervisor, child, process = self.sidebar_fixture()
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen", return_value=process) as start:
            supervisor.tick()
            supervisor.sidebar_processes[self.connection["id"]]["started"] = time.monotonic() - 45
            supervisor.tick()
            self.assertEqual(supervisor.runtime[self.connection["id"]]["state"], "connected")
            self.assertNotIn("sidebarError", supervisor.runtime[self.connection["id"]])
            process.communicate.assert_not_called()
            process.terminate.assert_not_called()
            child.terminate.assert_not_called()
            start.assert_called_once()
            process.poll.return_value = 0
            supervisor.tick()
        self.assertEqual(supervisor.runtime[self.connection["id"]]["sidebarItemID"], 42)

    def test_sidebar_denial_keeps_finder_fallback_and_connected_drive(self):
        supervisor, child, process = self.sidebar_fixture()
        process.communicate.return_value = ('{"ok":false,"error":"Finder did not accept this drive"}', "")
        process.returncode = 1
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen", return_value=process):
            supervisor.tick()
            process.poll.return_value = 1
            with self.assertLogs(level="WARNING"):
                supervisor.tick()
        self.assertEqual(supervisor.runtime[self.connection["id"]]["state"], "connected")
        self.assertIn("Add to Sidebar", supervisor.runtime[self.connection["id"]]["sidebarError"])
        child.terminate.assert_not_called()

    def test_late_poll_keeps_already_completed_sidebar_success(self):
        supervisor, child, process = self.sidebar_fixture()
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen", return_value=process):
            supervisor.tick()
            supervisor.sidebar_processes[self.connection["id"]]["started"] = time.monotonic() - 80
            process.poll.return_value = 0
            supervisor.tick()
        runtime = supervisor.runtime[self.connection["id"]]
        self.assertEqual(runtime["sidebarItemID"], 42)
        self.assertNotIn("sidebarError", runtime)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        child.terminate.assert_not_called()

    def test_pending_upload_stop_cleans_helper_only(self):
        supervisor, child, process = self.sidebar_fixture()
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen", return_value=process) as start:
            supervisor.tick()
            self.cache_metadata('{"Dirty":true}')
            supervisor.stop_requested = True
            supervisor.tick()
            start.assert_called_once()
        process.terminate.assert_called_once()
        child.terminate.assert_not_called()
        self.assertFalse(supervisor.sidebar_processes)
        self.assertIn("pending S3 uploads", supervisor.runtime[self.connection["id"]]["message"])

    def test_sidebar_partial_success_preserves_item_and_reports_warning(self):
        supervisor, child, process = self.sidebar_fixture()
        process.communicate.return_value = ('{"ok":true,"itemID":42,"warnings":["The previous entry could not be removed"]}', "")
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen", return_value=process):
            supervisor.tick()
            process.poll.return_value = 0
            with self.assertLogs(level="WARNING") as messages:
                supervisor.tick()
        runtime = supervisor.runtime[self.connection["id"]]
        self.assertEqual(runtime["sidebarItemID"], 42)
        self.assertTrue(runtime["sidebarError"])
        self.assertIn("The previous entry could not be removed", " ".join(messages.output))
        self.assertEqual(runtime["state"], "connected")
        child.terminate.assert_not_called()

    def test_helper_refusing_to_stop_is_killed_and_reaped_without_touching_rclone(self):
        supervisor, child, process = self.sidebar_fixture()
        process.communicate.side_effect = [subprocess.TimeoutExpired("sidebar", 0.2), ("", "")]
        supervisor.sidebar_processes[self.connection["id"]] = {"process": process}
        supervisor.stop_sidebars()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)
        child.terminate.assert_not_called()
        self.assertFalse(supervisor.sidebar_processes)

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

    def test_icon_overlay_is_materialized_from_package_safe_assets(self):
        assets = self.paths.resources / "icon-overlay-assets"
        assets.mkdir(parents=True)
        for source_name in turtle.ICON_ASSETS:
            (assets / source_name).write_text(source_name)
        overlay = turtle.materialized_icon_overlay(self.paths)
        self.assertEqual(overlay, self.paths.base / "icon-overlay")
        for source_name, target_name in turtle.ICON_ASSETS.items():
            self.assertEqual((overlay / target_name).read_text(), source_name)
        config, remote = turtle.connection_config(self.connection, self.paths)
        self.assertEqual(remote, "volume:")
        self.assertIn(str(overlay), config)

    def test_conservative_reads_disable_both_layers_of_prefetch(self):
        command = turtle.mount_command(self.connection, self.paths, "/rclone", "s3:photos")
        self.assertIn("readahead=0", command)
        for option in ("--buffer-size", "--vfs-read-ahead", "--vfs-read-chunk-streams"):
            self.assertEqual(command[command.index(option) + 1], "0")
        self.assertEqual(command[command.index("--vfs-read-chunk-size") + 1], "1Mi")
        self.assertEqual(command[command.index("--vfs-read-chunk-size-limit") + 1], "1Mi")

    def test_settings_are_saved_and_used_on_next_mount(self):
        with patch.object(turtle, "mount_table", return_value=set()):
            turtle.action(self.args("settings", self.connection["id"], "--cache-max-size-mib", "512",
                                    "--cache-max-age-hours", "6"), self.paths)
        connection = self.store.read()["connections"][0]
        command = turtle.mount_command(connection, self.paths, "/rclone", "s3:photos")
        self.assertEqual(command[command.index("--vfs-cache-max-size") + 1], "512Mi")
        self.assertEqual(command[command.index("--vfs-cache-max-age") + 1], "6h")
        self.assertEqual(connection["autoConnect"], self.connection["autoConnect"])

    def test_bad_settings_do_not_change_saved_connection(self):
        with patch.object(turtle, "mount_table", return_value=set()), self.assertRaises(ValueError):
            turtle.action(self.args("settings", self.connection["id"], "--cache-max-size-mib", "0"), self.paths)
        self.assertEqual(self.store.read()["connections"][0], self.connection)

    def test_omitted_settings_preserve_custom_values_when_editing(self):
        with self.store.update() as state:
            state["connections"][0].update(cacheMaxSizeMiB=512, cacheMaxAgeHours=6)
        with patch.object(turtle, "mount_table", return_value=set()):
            turtle.action(self.args("edit", self.connection["id"], "--name", self.connection["name"],
                                    "--bucket", self.connection["bucket"], "--profile", self.connection["profile"],
                                    "--region", self.connection["region"], "--auto-connect"), self.paths)
        saved = self.store.read()["connections"][0]
        self.assertEqual(saved["cacheMaxSizeMiB"], 512)
        self.assertEqual(saved["cacheMaxAgeHours"], 6)

    def test_settings_and_rename_refuse_attached_drives(self):
        mounted = {str(self.paths.mounts / self.connection["name"])}
        for arguments in (("settings", "--cache-max-age-hours", "6"), ("rename", "--name", "New Name")):
            with self.subTest(command=arguments[0]), patch.object(turtle, "mount_table", return_value=mounted):
                with self.assertRaises(ValueError):
                    turtle.action(self.args(arguments[0], self.connection["id"], *arguments[1:]), self.paths)
        self.assertEqual(self.store.read()["connections"][0], self.connection)

    def test_rename_preserves_identity_bucket_cache_and_startup_preference(self):
        metadata = self.cache_metadata('{"Dirty":false}')
        with patch.object(turtle, "mount_table", return_value=set()):
            turtle.action(self.args("rename", self.connection["id"], "--name", "Renamed Photos"), self.paths)
        connection = self.store.read()["connections"][0]
        for key in ("id", "bucket", "profile", "region", "readOnly", "autoConnect", "desiredConnected"):
            self.assertEqual(connection[key], self.connection[key])
        self.assertEqual(connection["name"], "Renamed Photos")
        self.assertTrue(metadata.exists())

    def test_cache_info_reports_allocated_not_sparse_logical_bytes_and_is_bounded(self):
        root = self.paths.cache / self.connection["id"]
        path = root / "vfs/volume/large.jpg"
        path.parent.mkdir(parents=True)
        with path.open("wb") as handle:
            handle.seek(1024**3)
            handle.write(b"x")
        result = turtle.cache_info(self.connection, self.paths)
        self.assertEqual(result["files"], 1)
        self.assertLess(result["usedBytes"], 1024**3)
        self.assertFalse(result["partial"])
        self.assertTrue(turtle.cache_info(self.connection, self.paths, max_entries=1)["partial"])

    def test_clear_cache_refuses_dirty_data_even_for_a_readonly_connection(self):
        metadata = self.cache_metadata('{"Dirty":true}')
        with self.store.update() as state:
            state["connections"][0]["readOnly"] = True
        with patch.object(turtle, "mount_table", return_value=set()), self.assertRaises(ValueError):
            turtle.action(self.args("clear-cache", self.connection["id"]), self.paths)
        self.assertTrue(metadata.exists())

    def test_clear_cache_keeps_saved_connection_and_never_invokes_rclone(self):
        self.cache_metadata('{"Dirty":false}')
        with patch.object(turtle, "mount_table", return_value=set()), patch.object(turtle.subprocess, "Popen") as start:
            turtle.action(self.args("clear-cache", self.connection["id"]), self.paths)
        start.assert_not_called()
        self.assertFalse((self.paths.cache / self.connection["id"]).exists())
        self.assertEqual(self.store.read()["connections"][0]["bucket"], self.connection["bucket"])

    def test_refresh_invalidates_directory_cache_without_remount_or_preload(self):
        with self.store.update() as state:
            state["connections"][0].update(desiredConnected=True, refreshRequested=True)
        supervisor, child = turtle.Supervisor(self.paths), Mock()
        child.pid, child.poll.return_value = 12345, None
        supervisor.children[self.connection["id"]] = {"process": child, "started": time.time() - 10}
        with patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}), \
             patch.object(turtle.subprocess, "Popen") as start:
            supervisor.tick()
            supervisor.tick()
        child.send_signal.assert_called_once_with(turtle.signal.SIGHUP)
        child.terminate.assert_not_called()
        start.assert_not_called()
        self.assertFalse(self.store.read()["connections"][0]["refreshRequested"])

    def test_reconnect_waits_for_safe_detach_and_previous_process_exit(self):
        with self.store.update() as state:
            state["connections"][0].update(desiredConnected=True, reconnectRequested=True)
        supervisor, child, unmount = turtle.Supervisor(self.paths), Mock(), Mock()
        child.pid, child.poll.return_value = 12345, None
        unmount.poll.return_value = None
        supervisor.children[self.connection["id"]] = {"process": child, "started": time.time() - 10}
        mounted = {str(self.paths.mounts / self.connection["name"])}
        with patch.object(turtle, "mount_table", return_value=mounted), \
             patch.object(turtle.subprocess, "Popen", return_value=unmount):
            supervisor.tick()
        child.terminate.assert_not_called()
        unmount.poll.return_value = 0
        with patch.object(turtle, "mount_table", return_value=set()), patch.object(supervisor, "start_mount") as start:
            supervisor.tick()
            start.assert_not_called()
            child.terminate.assert_called()
            child.poll.return_value = 0
            supervisor.tick()
            start.assert_not_called()
            supervisor.tick()
            start.assert_called_once()
        self.assertTrue(self.store.read()["connections"][0]["desiredConnected"])

    def test_disconnect_cancels_a_pending_reconnect(self):
        with self.store.update() as state:
            state["connections"][0].update(desiredConnected=True, reconnectRequested=True)
        with patch.object(turtle, "mount_table", return_value=set()):
            turtle.action(self.args("disconnect", self.connection["id"]), self.paths)
        supervisor = turtle.Supervisor(self.paths)
        with patch.object(turtle, "mount_table", return_value=set()), patch.object(supervisor, "start_mount") as start:
            supervisor.tick()
        start.assert_not_called()
        self.assertFalse(self.store.read()["connections"][0]["desiredConnected"])

    def test_connect_during_eject_survives_service_requested_child_exit(self):
        supervisor, child, unmount = turtle.Supervisor(self.paths), Mock(), Mock()
        child.pid, child.poll.return_value = 12345, None
        unmount.poll.return_value = None
        supervisor.children[self.connection["id"]] = {
            "process": child, "started": time.time() - 10, "seenMounted": True}
        supervisor.ejections[self.connection["id"]] = {
            "process": unmount, "started": time.time(), "revision": 1}
        mounted = {str(self.paths.mounts / self.connection["name"])}
        with patch.object(turtle, "mount_table", return_value=mounted), \
             patch.object(turtle, "ensure_service"):
            turtle.action(self.args("connect", self.connection["id"]), self.paths)
        # The prior non-forced unmount wins its race with Connect. The service
        # must finish that process without mistaking its clean exit for Finder eject.
        unmount.poll.return_value = 0
        with patch.object(turtle, "mount_table", return_value=set()), patch.object(supervisor, "start_mount") as start:
            supervisor.tick()
            child.terminate.assert_called()
            child.poll.return_value = 0
            supervisor.tick()
            self.assertTrue(self.store.read()["connections"][0]["desiredConnected"])
            supervisor.retry[self.connection["id"]] = 0
            supervisor.tick()
            start.assert_called_once()

    def test_new_mount_records_error_cutoff_without_changing_connection_intent(self):
        with self.store.update() as state:
            state["connections"][0]["desiredConnected"] = True
        connection = dict(self.store.read()["connections"][0])
        before = dict(connection)
        started = 1789370400.5
        process = Mock(pid=12345)
        supervisor = turtle.Supervisor(self.paths)
        with patch.object(turtle, "dependencies", return_value={"rclone": "/rclone"}), \
             patch.object(turtle.subprocess, "Popen", return_value=process), \
             patch.object(turtle.time, "time", return_value=started):
            supervisor.start_mount(connection)
        expected = dict(before, lastMountAt=started)
        self.assertEqual(self.store.read()["connections"][0], expected)
        self.assertEqual(connection, expected)
        self.assertEqual(supervisor.children[connection["id"]]["started"], started)

    def test_tail_error_ignores_errors_before_latest_mount_or_login(self):
        now = int(time.time())
        path = self.paths.logs / (self.connection["id"] + ".log")
        old_stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(now - 30))
        recent_stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(now))
        path.write_text(f"{old_stamp} ERROR : previous mount failed\n"
                        f"{old_stamp} ERROR : ExpiredToken\n"
                        f"{recent_stamp} NOTICE : current mount ready\n")
        for last_login, last_mount in ((now - 60, now - 10), (now - 10, now - 60)):
            with self.subTest(lastLoginAt=last_login, lastMountAt=last_mount):
                connection = dict(self.connection, lastLoginAt=last_login, lastMountAt=last_mount)
                self.assertEqual(turtle.tail_error(connection, self.paths), "")

    def test_tail_error_retains_errors_from_current_mount(self):
        now = int(time.time())
        path = self.paths.logs / (self.connection["id"] + ".log")
        stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(now))
        connection = dict(self.connection, lastLoginAt=now - 60, lastMountAt=now)
        for text, expected in (("current mount failed", "The drive reported an error"),
                               ("ExpiredToken", "AWS sign-in expired")):
            with self.subTest(error=text):
                path.write_text(f"{stamp} ERROR : {text}\n")
                self.assertIn(expected, turtle.tail_error(connection, self.paths))

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

    def test_dependencies_verify_aws_v2_rclone_nfsmount_and_privacy_state(self):
        def fake_executable(name):
            return {"aws": "/aws", "rclone": "/rclone", "brew": "/brew"}.get(name)

        def fake_checked(command, timeout=5):
            if command == ["/aws", "--version"]:
                return True, "aws-cli/2.31.35 Python/3.13.9 Darwin/25.2.0 exe/arm64"
            if command == ["/rclone", "version"]:
                return True, "rclone v1.75.1\n- os/type: darwin"
            if command == ["/rclone", "nfsmount", "--help"]:
                return True, "Rclone nfsmount allows macOS to mount remotes."
            return False, ""

        with patch.object(turtle, "executable", side_effect=fake_executable), \
             patch.object(turtle, "checked_command", side_effect=fake_checked), \
             patch.object(turtle, "installed_application", return_value=True), \
             patch.object(turtle, "application_path", return_value=Path("/Users/example/Applications/Mountain Turtle.app")):
            result = turtle.dependencies(self.paths, {"a": {"sidebarItemID": 42}}, {"/Volumes/Nikki"})
        self.assertTrue(result["awsCliV2"])
        self.assertTrue(result["rcloneNfsmount"])
        self.assertTrue(result["appInstalled"])
        self.assertEqual(result["privacyState"], "approved")
        self.assertEqual(result["awsVersion"].split()[0], "aws-cli/2.31.35")
        self.assertEqual(result["rcloneVersion"], "rclone v1.75.1")

    def test_dependencies_reject_aws_v1_and_missing_nfsmount(self):
        def fake_executable(name):
            return {"aws": "/aws", "rclone": "/rclone"}.get(name)

        def fake_checked(command, timeout=5):
            if command == ["/aws", "--version"]:
                return True, "aws-cli/1.42.0 Python/3.11.0"
            if command == ["/rclone", "version"]:
                return True, "rclone v1.75.1"
            if command == ["/rclone", "nfsmount", "--help"]:
                return False, "unknown command"
            return False, ""

        with patch.object(turtle, "executable", side_effect=fake_executable), \
             patch.object(turtle, "checked_command", side_effect=fake_checked):
            result = turtle.dependencies(self.paths, {"a": {"sidebarError": "Allow Network Volumes"}}, set())
        self.assertFalse(result["awsCliV2"])
        self.assertFalse(result["rcloneNfsmount"])
        self.assertEqual(result["privacyState"], "needsApproval")

    def test_sidebar_status_disappears_immediately_when_kernel_mount_is_gone(self):
        turtle.write_json(self.paths.base / "runtime.json", {"connections": {
            self.connection["id"]: {"state": "needsLogin", "sidebarItemID": 42,
                                      "sidebarError": "Sidebar permission needed; drive stays connected."}}})
        with patch.object(turtle, "service_running", return_value=True), \
             patch.object(turtle, "mount_table", return_value={str(self.paths.mounts / self.connection["name"])}) as mounts, \
             patch.object(turtle, "dependencies", return_value={}):
            attached = turtle.status(self.paths)["connections"][0]
            self.assertEqual(attached["sidebarItemID"], 42)
            self.assertIn("permission needed", attached["sidebarError"])
            # The supervisor has not rewritten runtime.json yet after Finder eject.
            mounts.return_value = set()
            detached = turtle.status(self.paths)["connections"][0]
        self.assertFalse(detached["mounted"])
        self.assertNotIn("sidebarError", detached)
        self.assertNotIn("sidebarItemID", detached)

    def test_finder_eject_stays_disconnected_even_with_autoconnect(self):
        with self.store.update() as state:
            state["connections"][0]["desiredConnected"] = True
        supervisor, process = turtle.Supervisor(self.paths), Mock()
        supervisor.sidebar_results[self.connection["id"]] = {"sidebarError": "An old sidebar error", "sidebarItemID": 42}
        process.pid, process.poll.return_value = 12345, 0
        supervisor.children[self.connection["id"]] = {"process": process, "started": time.time(), "seenMounted": True}
        with patch.object(turtle, "mount_table", return_value=set()), patch.object(supervisor, "start_mount") as start:
            supervisor.tick()
        start.assert_not_called()
        self.assertFalse(self.store.read()["connections"][0]["desiredConnected"])
        self.assertTrue(self.store.read()["connections"][0]["autoConnect"])
        self.assertNotIn(self.connection["id"], supervisor.sidebar_results)
        self.assertNotIn("sidebarError", supervisor.runtime[self.connection["id"]])

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

    def test_login_uses_device_authorization_for_the_selected_profile(self):
        with patch.object(turtle, "executable", return_value="/aws"), \
             patch.object(turtle.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            result = turtle.action(self.args("login", self.connection["id"]), self.paths)
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0],
                         ["/aws", "sso", "login", "--use-device-code", "--profile", "production"])
        self.assertEqual(run.call_args.kwargs["env"]["AWS_PROFILE"], "production")
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(run.call_args.kwargs["timeout"], 300)

    def test_successful_login_records_refresh_without_changing_connection_intent(self):
        for desired in (False, True):
            with self.subTest(desiredConnected=desired):
                with self.store.update() as state:
                    state["connections"][0]["desiredConnected"] = desired
                before = dict(self.store.read()["connections"][0])
                completed_at = 1789370400.5
                with patch.object(turtle, "executable", return_value="/aws"), \
                     patch.object(turtle.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                     patch.object(turtle.time, "time", return_value=completed_at):
                    turtle.action(self.args("login", self.connection["id"]), self.paths)
                expected = dict(before, lastLoginAt=completed_at, revision=before["revision"] + 1)
                self.assertEqual(self.store.read()["connections"][0], expected)

    def test_failed_login_preserves_existing_success_and_connection_state(self):
        with self.store.update() as state:
            state["connections"][0].update(lastLoginAt=123.0, desiredConnected=True)
        before = self.store.read()
        with patch.object(turtle, "executable", return_value="/aws"), \
             patch.object(turtle.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stderr="private provider output")):
            with self.assertRaisesRegex(ValueError, "Close the previous sign-in tab") as error:
                turtle.action(self.args("login", self.connection["id"]), self.paths)
        self.assertNotIn("private provider output", str(error.exception))
        self.assertEqual(self.store.read(), before)

    def test_login_timeout_requests_a_fresh_signin_and_preserves_connection_state(self):
        before = self.store.read()
        with patch.object(turtle, "executable", return_value="/aws"), \
             patch.object(turtle.subprocess, "run", side_effect=subprocess.TimeoutExpired("aws", 300)) as run:
            with self.assertRaisesRegex(ValueError, "five minutes.*Close the previous sign-in tab.*fresh request"):
                turtle.action(self.args("login", self.connection["id"]), self.paths)
        self.assertEqual(run.call_args.kwargs["timeout"], 300)
        self.assertEqual(self.store.read(), before)


if __name__ == "__main__":
    unittest.main()
