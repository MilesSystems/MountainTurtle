import contextlib
import fcntl
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "service/turtle_service.py"
spec = importlib.util.spec_from_file_location("update_turtle", SOURCE)
turtle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turtle)


class UpdateLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = turtle.Paths(home=temporary.name)
        self.paths.prepare()
        self.store = turtle.Store(self.paths)
        self.connections = [
            dict(id="manual", name="Manual", desiredConnected=True, autoConnect=False,
                 readOnly=False, revision=3),
            dict(id="ejected", name="Ejected", desiredConnected=False, autoConnect=True,
                 readOnly=True, revision=7),
        ]
        with self.store.update() as state:
            state["connections"] = self.connections
        self.attached = set()
        self.running = False
        self.alive = set()
        self.runtime = {"connections": {}}
        self.clock = 0
        self.on_sleep = None
        self.on_ensure = None
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, replacement in (
            ("mount_table", lambda: set(self.attached)),
            ("service_running", lambda paths: self.running),
            ("process_alive", lambda pid: pid in self.alive),
        ):
            stack.enter_context(patch.object(turtle, name, side_effect=replacement))
        stack.enter_context(patch.object(turtle.Store, "runtime", side_effect=lambda: self.runtime))
        stack.enter_context(patch.object(turtle.time, "monotonic", side_effect=lambda: self.clock))
        stack.enter_context(patch.object(turtle.time, "sleep", side_effect=self.sleep))
        self.ensure = stack.enter_context(patch.object(turtle, "ensure_service", side_effect=self.ensure_service))
        # These tests operate only on a temporary local state store. Any process
        # launch, forceful signal or cache deletion is an unexpected side effect.
        self.spawn = stack.enter_context(patch.object(turtle.subprocess, "Popen", side_effect=AssertionError("process launch")))
        self.signal = stack.enter_context(patch.object(turtle.os, "kill", side_effect=AssertionError("process signal")))
        self.clear_cache = stack.enter_context(patch.object(turtle, "clear_cache", side_effect=AssertionError("cache deletion")))

    def sleep(self, interval):
        self.clock += interval
        if self.on_sleep:
            self.on_sleep()

    def ensure_service(self, paths):
        if self.on_ensure:
            return self.on_ensure()
        self.running = True
        self.alive.add(501)
        marker = self.store.read().get("updateHandoff", {})
        self.runtime = dict(self.runtime, pid=501, updateHandoffToken=marker.get("token"))

    def attach(self):
        self.attached = {str(self.paths.mounts / "Manual")}
        self.running = True
        self.alive = {301, 302}
        self.runtime = {"pid": 301, "connections": {"manual": {"pid": 302, "state": "connected"}}}

    def handoff(self, phase="prepared"):
        with self.store.update() as state:
            state["updateHandoff"] = {"version": 1, "phase": phase, "token": "test-handoff",
                                      "createdAt": time.time(), "intent": {"manual": True, "ejected": False}}
            state["shutdown"] = True
            for connection in state["connections"]:
                connection["desiredConnected"] = False

    def assert_intent_restored(self):
        state = self.store.read()
        self.assertFalse(state["shutdown"])
        self.assertEqual([c["desiredConnected"] for c in state["connections"][:2]], [True, False])
        return state

    def test_resume_without_marker_is_a_no_op(self):
        before = self.store.read()
        self.assertEqual(turtle.resume_update(self.paths), {"ok": True, "resumed": False})
        self.assertEqual(self.store.read(), before)
        self.ensure.assert_not_called()

    def test_prepare_saves_manual_choices_until_relaunch(self):
        self.assertTrue(turtle.prepare_update(self.paths)["prepared"])
        state = self.store.read()
        self.assertTrue(state["shutdown"])
        self.assertEqual(state["updateHandoff"]["phase"], "prepared")
        self.assertEqual(state["updateHandoff"]["intent"], {"manual": True, "ejected": False})
        self.assertEqual([c["desiredConnected"] for c in state["connections"]], [False, False])
        self.ensure.assert_not_called()

    def test_previously_stopped_service_can_still_prepare_an_update(self):
        with self.store.update() as state:
            state["shutdown"] = True
            for connection in state["connections"]:
                connection["desiredConnected"] = False
        self.assertTrue(turtle.prepare_update(self.paths)["prepared"])
        self.assertEqual(self.store.read()["updateHandoff"]["intent"], {"manual": False, "ejected": False})

    def test_prepare_waits_for_ordinary_ejection_processes_and_supervisor(self):
        self.attach()
        cache = self.paths.cache / "pending-data"
        cache.write_bytes(b"keep these bytes")
        stages = []

        def progress():
            state = self.store.read()
            self.assertTrue(state["shutdown"])
            self.assertEqual(state["updateHandoff"]["intent"]["manual"], True)
            stages.append(1)
            self.attached.clear()
            if len(stages) >= 2:
                self.alive.clear()
                self.running = False

        self.on_sleep = progress
        self.assertTrue(turtle.prepare_update(self.paths)["prepared"])
        self.assertEqual(len(stages), 2)
        self.assertEqual(cache.read_bytes(), b"keep these bytes")
        self.spawn.assert_not_called()
        self.signal.assert_not_called()
        self.clear_cache.assert_not_called()

    def test_busy_drive_aborts_and_restores_intent(self):
        self.attach()

        def busy():
            self.runtime["connections"]["manual"].update(message="Drive is busy.", updatedAt=time.time() + 1)

        self.on_sleep = busy
        with self.assertRaisesRegex(RuntimeError, "drive is busy"):
            turtle.prepare_update(self.paths)
        self.assertNotIn("updateHandoff", self.assert_intent_restored())
        self.assertTrue(self.attached)
        self.signal.assert_not_called()

    def test_stale_busy_message_does_not_abort_new_attempt(self):
        self.runtime["connections"] = {"manual": {"message": "Drive is busy.", "updatedAt": 1}}
        self.assertTrue(turtle.prepare_update(self.paths)["prepared"])

    def test_timeout_preserves_attached_drive_and_recovers_service(self):
        self.attach()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            turtle.prepare_update(self.paths, timeout=5)
        self.assertGreaterEqual(self.clock, 5)
        self.assertLess(self.clock, 6)
        self.assertNotIn("updateHandoff", self.assert_intent_restored())
        self.assertTrue(self.attached)
        self.signal.assert_not_called()

    def test_resume_preserves_new_settings_and_does_not_recreate_removed_drives(self):
        self.handoff()
        with self.store.update() as state:
            state["connections"][0].update(name="Renamed", host="new-server", revision=30)
            state["connections"].append(dict(id="later", name="Added later", desiredConnected=False))
            state["updateHandoff"]["intent"]["removed"] = True
        self.assertTrue(turtle.resume_update(self.paths)["resumed"])
        state = self.assert_intent_restored()
        self.assertEqual(state["connections"][0]["host"], "new-server")
        self.assertEqual(state["connections"][0]["revision"], 31)
        self.assertEqual(state["connections"][2], dict(id="later", name="Added later", desiredConnected=False))
        self.assertEqual(len(state["connections"]), 3)
        self.assertNotIn("updateHandoff", state)

    def test_interrupted_preparation_and_cancel_both_recover(self):
        for phase in ("preparing", "prepared", "resuming"):
            with self.subTest(phase=phase):
                self.handoff(phase)
                self.assertTrue(turtle.resume_update(self.paths)["resumed"])
                self.assertNotIn("updateHandoff", self.assert_intent_restored())

    def test_launch_failure_leaves_recovery_marker_for_next_launch(self):
        self.handoff()
        self.ensure.side_effect = RuntimeError("launch failed")
        with self.assertRaisesRegex(RuntimeError, "launch failed"):
            turtle.resume_update(self.paths)
        state = self.assert_intent_restored()
        self.assertEqual(state["updateHandoff"]["phase"], "resuming")

    def test_stale_live_runtime_cannot_acknowledge_recovery(self):
        self.handoff()
        self.attach()
        self.runtime["updateHandoffToken"] = None
        calls = []

        def restart():
            calls.append(1)
            if len(calls) == 2:
                self.runtime.update(pid=501, updateHandoffToken="test-handoff")
                self.alive.add(501)
            else:
                self.assertIn("updateHandoff", self.store.read())

        self.on_ensure = restart
        self.assertTrue(turtle.resume_update(self.paths)["resumed"])
        self.assertEqual(len(calls), 2)

    def test_missing_acknowledgment_is_bounded_and_retains_marker(self):
        self.handoff()
        self.attach()
        self.runtime["updateHandoffToken"] = None
        self.on_ensure = lambda: None
        with self.assertRaisesRegex(RuntimeError, "has not restarted"):
            turtle.resume_update(self.paths)
        self.assertGreaterEqual(self.clock, 8)
        self.assertLess(self.clock, 9)
        self.assertIn("updateHandoff", self.assert_intent_restored())

    def test_busy_legacy_supervisor_recovers_without_a_token(self):
        self.handoff("preparing")
        self.attach()
        self.on_ensure = lambda: None
        self.assertTrue(turtle.resume_update(self.paths)["resumed"])
        self.assertNotIn("updateHandoff", self.assert_intent_restored())
        self.assertTrue(self.attached)
        self.signal.assert_not_called()

    def test_detached_legacy_supervisor_must_exit_or_acknowledge(self):
        self.handoff()
        self.attach()
        self.attached.clear()
        self.on_ensure = lambda: None
        with self.assertRaisesRegex(RuntimeError, "has not restarted"):
            turtle.resume_update(self.paths)
        self.assertIn("updateHandoff", self.assert_intent_restored())

    def test_update_lock_rejects_concurrent_operation(self):
        before = self.store.read()
        with (self.paths.base / "update.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "Another update"):
                turtle.prepare_update(self.paths)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.clock, 0)

    def test_state_lock_timeout_makes_no_connection_change(self):
        before = self.store.read()
        with (self.paths.base / "state.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "settings are busy"):
                turtle.prepare_update(self.paths)
        self.assertEqual(self.store.read(), before)
        self.assertGreaterEqual(self.clock, 5)
        self.assertLess(self.clock, 6)
        self.ensure.assert_not_called()

    def test_existing_marker_is_not_overwritten_by_another_preparation(self):
        self.handoff()
        before = self.store.read()
        with self.assertRaisesRegex(RuntimeError, "previous update"):
            turtle.prepare_update(self.paths)
        self.assertEqual(self.store.read(), before)
        self.ensure.assert_not_called()

    def test_login_defaults_cannot_override_update_intent(self):
        self.handoff()
        before = self.store.read()
        turtle.Supervisor(self.paths).restore_login_intent()
        self.assertEqual(self.store.read(), before)

    def test_supervisor_acknowledges_only_a_resumed_non_shutdown_tick(self):
        self.handoff("resuming")
        with self.store.update() as state:
            state["connections"] = []
            state["shutdown"] = False
        supervisor = turtle.Supervisor(self.paths)
        self.assertTrue(supervisor.tick())
        published = json.loads((self.paths.base / "runtime.json").read_text())
        self.assertEqual(published["updateHandoffToken"], "test-handoff")
        supervisor.stop_requested = True
        self.assertFalse(supervisor.tick())
        published = json.loads((self.paths.base / "runtime.json").read_text())
        self.assertIsNone(published["updateHandoffToken"])

    def test_connection_mutation_is_rejected_during_update(self):
        self.handoff()
        before = self.store.read()
        for command in ("connect", "disconnect", "reconnect", "remove"):
            with self.subTest(command=command):
                args = turtle.parser().parse_args([command, "manual"])
                with self.assertRaisesRegex(RuntimeError, "update is in progress"):
                    turtle.action(args, self.paths)
                self.assertEqual(self.store.read(), before)

    def test_invalid_marker_is_retained_without_starting_service(self):
        with self.store.update() as state:
            state["updateHandoff"] = {"version": 1, "intent": "invalid"}
        before = self.store.read()
        with self.assertRaisesRegex(RuntimeError, "recovery information is invalid"):
            turtle.resume_update(self.paths)
        self.assertEqual(self.store.read(), before)
        self.ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
