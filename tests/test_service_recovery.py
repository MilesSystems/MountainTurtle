import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "service/turtle_service.py"
spec = importlib.util.spec_from_file_location("recovery_turtle", SOURCE)
turtle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turtle)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = turtle.Paths(home=temporary.name, resources=Path(temporary.name) / "Resources")
        self.paths.prepare()
        self.store = turtle.Store(self.paths)
        self.connections = [
            dict(id="manual-drive", name="Manual drive", autoConnect=False, desiredConnected=True),
            dict(id="ejected-drive", name="Ejected drive", autoConnect=True, desiredConnected=False),
        ]
        with self.store.update() as state:
            state["connections"] = self.connections

    def test_crash_recovery_preserves_manual_connection_and_user_eject(self):
        supervisor = turtle.Supervisor(self.paths)
        supervisor.children["manual-drive"] = {"seenMounted": True, "process": Mock()}
        before = self.store.read()
        supervisor.restore_login_intent()
        self.assertEqual(self.store.read(), before)

    def test_recovery_preserves_a_pending_graceful_shutdown(self):
        with self.store.update() as state:
            state["shutdown"] = True
            state["connections"][0]["desiredConnected"] = False
        supervisor = turtle.Supervisor(self.paths)
        supervisor.children["manual-drive"] = {"seenMounted": True, "process": Mock()}
        before = self.store.read()
        supervisor.restore_login_intent()
        self.assertEqual(self.store.read(), before)

    def test_new_login_without_owned_mounts_applies_startup_preferences(self):
        with self.store.update() as state:
            state["shutdown"] = True
        supervisor = turtle.Supervisor(self.paths)
        supervisor.restore_login_intent()
        state = self.store.read()
        self.assertFalse(state["shutdown"])
        self.assertEqual([c["desiredConnected"] for c in state["connections"]], [False, True])

    def test_matching_config_and_live_mount_are_required_for_recovery(self):
        turtle.write_json(self.paths.base / "runtime.json", {"connections": {"manual-drive": {"pid": 12345}}})
        expected_config = str(self.paths.remotes / "manual-drive.conf")
        owned = "/opt/homebrew/bin/rclone nfsmount volume: --config " + expected_config
        for command, mounted, keeps_intent in (
            (owned, {str(self.paths.mounts / "Manual drive")}, True),
            (owned, set(), False),
            ("/opt/homebrew/bin/rclone nfsmount other: --config /some/other.conf", {str(self.paths.mounts / "Manual drive")}, False),
        ):
            with self.subTest(command=command, mounted=mounted):
                with self.store.update() as state:
                    state["connections"][0]["desiredConnected"] = True
                supervisor = turtle.Supervisor(self.paths)
                with patch.object(turtle, "process_alive", return_value=True), \
                     patch.object(turtle, "mount_table", return_value=mounted), \
                     patch.object(turtle.subprocess, "run", return_value=Mock(stdout=command)):
                    supervisor.recover(self.store.read()["connections"])
                supervisor.restore_login_intent()
                self.assertEqual(self.store.read()["connections"][0]["desiredConnected"], keeps_intent)


if __name__ == "__main__":
    unittest.main()
