"""A slow directory must not create a queue of parent-locking refresh jobs."""
import threading
import unittest
from unittest.mock import Mock, patch

import test_service

turtle = test_service.turtle


class ListingBackpressureTests(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp

    def child(self):
        return {"remoteControl": {"sessionID": "mount-generation", "rcPort": 42000}}

    def test_slow_child_coalesces_parent_duplicate_and_manual_refresh(self):
        supervisor, child = turtle.Supervisor(self.paths), self.child()
        with patch.object(turtle, "observed_directory_refresh", return_value={"jobid": 7}) as launch, \
             patch.object(turtle, "remote_control_post", return_value={"finished": False}) as status:
            supervisor.refresh_directory(self.connection, child, "2025/session/Raw", False)
            for path in ["2025/session/Raw", "2025/session", "2025", None]:
                self.assertTrue(supervisor.refresh_directory(self.connection, child, path)["throttled"])
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(status.call_count, 4)
            self.assertTrue(all(c.args[1] == "job/status" for c in status.call_args_list))
            status.return_value = {"finished": True, "success": False}
            supervisor.refresh_directory(self.connection, child, "2025", False)
            self.assertEqual(launch.call_count, 2)

    def test_completed_monitor_allows_refresh_after_rclone_expires_job(self):
        supervisor, child = turtle.Supervisor(self.paths), self.child()
        def completed(*args, **kwargs):
            kwargs["finished"].set()
            return {"jobid": 7}
        with patch.object(turtle, "observed_directory_refresh", side_effect=completed) as launch, \
             patch.object(turtle, "remote_control_post") as status:
            supervisor.refresh_directory(self.connection, child, "Raw", False)
            supervisor.refresh_directory(self.connection, child, "Other", False)
            self.assertEqual(launch.call_count, 2)
            status.assert_not_called()

    def test_monitor_loss_does_not_admit_more_work_but_other_mount_can_refresh(self):
        supervisor, child = turtle.Supervisor(self.paths), self.child()
        with patch.object(turtle, "observed_directory_refresh", return_value={"jobid": 7}) as launch, \
             patch.object(turtle, "remote_control_post", side_effect=OSError("timed out")):
            supervisor.refresh_directory(self.connection, child, "Raw", False)
            self.assertTrue(supervisor.refresh_directory(self.connection, child)["throttled"])
            supervisor.refresh_directory(self.connection, self.child(), "Other", False)
            self.assertEqual(launch.call_count, 2)

    def test_submission_timeout_is_not_proof_job_did_not_start(self):
        supervisor, child = turtle.Supervisor(self.paths), self.child()
        with patch.object(turtle, "observed_directory_refresh", side_effect=OSError("timed out")) as launch:
            with self.assertRaises(OSError):
                supervisor.refresh_directory(self.connection, child, "Raw", False)
            self.assertTrue(supervisor.refresh_directory(self.connection, child)["throttled"])
            launch.assert_called_once()

    def test_concurrent_finder_callbacks_cannot_both_dispatch(self):
        supervisor, child = turtle.Supervisor(self.paths), self.child()
        entered, release = threading.Event(), threading.Event()
        def pending(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"jobid": 7}
        with patch.object(turtle, "observed_directory_refresh", side_effect=pending) as launch:
            worker = threading.Thread(target=supervisor.refresh_directory,
                                      args=(self.connection, child, "Raw", False))
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(supervisor.refresh_directory(self.connection, child)["throttled"])
                launch.assert_called_once()
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_saturated_monitor_pool_never_starts_untracked_job(self):
        with patch.object(turtle, "LISTING_MONITORS", threading.BoundedSemaphore(0)), \
             patch.object(turtle, "start_directory_refresh") as launch:
            result = turtle.observed_directory_refresh(self.paths, self.connection, {})
        self.assertTrue(result["throttled"])
        launch.assert_not_called()

    def test_capacity_rejection_can_retry_without_reconnecting(self):
        supervisor, child = turtle.Supervisor(self.paths), self.child()
        with patch.object(turtle, "observed_directory_refresh", side_effect=[{"throttled": True}, {"jobid": 7}]) as launch:
            self.assertTrue(supervisor.refresh_directory(self.connection, child)["throttled"])
            self.assertEqual(supervisor.refresh_directory(self.connection, child)["jobid"], 7)
            self.assertEqual(launch.call_count, 2)

    def test_failed_submission_releases_monitor_capacity(self):
        slots = threading.BoundedSemaphore(1)
        with patch.object(turtle, "LISTING_MONITORS", slots), \
             patch.object(turtle, "start_directory_refresh", side_effect=OSError("timeout")):
            with self.assertRaises(OSError):
                turtle.observed_directory_refresh(self.paths, self.connection, {})
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_monitor_confirms_failed_job_finished_and_releases_slot(self):
        slots, finished = threading.BoundedSemaphore(1), threading.Event()
        def thread(**kwargs):
            result = Mock()
            result.start.side_effect = kwargs["target"]
            return result
        with patch.object(turtle, "LISTING_MONITORS", slots), \
             patch.object(turtle, "start_directory_refresh", return_value={"jobid": 7}), \
             patch.object(turtle, "remote_control_post", return_value={"finished": True, "success": False}), \
             patch.object(turtle.threading, "Thread", side_effect=thread):
            turtle.observed_directory_refresh(self.paths, self.connection, {}, finished=finished)
        self.assertTrue(finished.is_set())
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()
