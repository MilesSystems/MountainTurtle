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

    def test_network_recovery_only_retries_requested_unmounted_drives(self):
        with patch.object(turtle, 'mount_table', return_value=set()), patch.object(turtle, 'ensure_service') as ensure:
            turtle.action(turtle.parser().parse_args(['recover-connections']), self.paths)
        current = self.store.read()['connections']
        self.assertEqual(current[0]['revision'], 1)
        self.assertNotIn('revision', current[1])
        self.assertFalse(current[1]['desiredConnected'])
        ensure.assert_called_once()

    def test_network_recovery_preserves_live_mounts_and_update_handoff(self):
        for mounted, handoff in (({str(self.paths.mounts / 'Manual drive')}, None), (set(), {'phase': 'prepared'})):
            with self.store.update() as state:
                if handoff:
                    state['updateHandoff'] = handoff
            before = self.store.read()
            with patch.object(turtle, 'mount_table', return_value=mounted), patch.object(turtle, 'ensure_service') as ensure:
                turtle.action(turtle.parser().parse_args(['recover-connections']), self.paths)
            self.assertEqual(self.store.read(), before)
            ensure.assert_not_called()

    def test_wake_clears_backoff_but_does_not_restore_ejected_drives(self):
        supervisor = turtle.Supervisor(self.paths)
        supervisor.last_tick_at = 100
        supervisor.retry['manual-drive'] = 1000
        supervisor.revisions['manual-drive'] = 0
        with patch.object(turtle.time, 'time', return_value=200), \
             patch.object(turtle, 'mount_table', return_value=set()), \
             patch.object(supervisor, 'start_mount') as start:
            supervisor.tick()
        start.assert_called_once()
        self.assertEqual(start.call_args.args[0]['id'], 'manual-drive')
        self.assertFalse(self.store.read()['connections'][1]['desiredConnected'])


if __name__ == "__main__":
    unittest.main()
