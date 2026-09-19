import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zlib

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
import offline_photos as offline
import turtle_service as turtle


class Reader:
    def __init__(self, data=b"photo-original-content", checksum="sha256"):
        self.data, self.checksum = data, checksum
        self.gets, self.heads = [], []
        self.before_get = None
        self.after_get = None

    def head(self, key, etag):
        self.heads.append((key, etag))
        result = {"ContentLength": len(self.data), "ETag": '"version-a"', "VersionId": "version-id"}
        if self.checksum == "sha256":
            result.update(ChecksumType="FULL_OBJECT", ChecksumSHA256=base64.b64encode(hashlib.sha256(self.data).digest()).decode())
        elif self.checksum == "crc32":
            result.update(ChecksumType="FULL_OBJECT", ChecksumCRC32=base64.b64encode(zlib.crc32(self.data).to_bytes(4, "big")).decode())
        elif self.checksum == "crc32c":
            crc = offline.crc32c_update(0xffffffff, self.data) ^ 0xffffffff
            result.update(ChecksumType="FULL_OBJECT", ChecksumCRC32C=base64.b64encode(crc.to_bytes(4, "big")).decode())
        elif self.checksum == "composite":
            result.update(ChecksumType="COMPOSITE", ChecksumSHA256="not-a-full-object-hash-3")
        elif self.checksum == "bad":
            result.update(ChecksumType="FULL_OBJECT", ChecksumSHA256=base64.b64encode(b"invalid").decode())
        return result

    def get(self, key, etag, destination, byte_range):
        self.gets.append((key, etag, byte_range))
        if self.before_get:
            self.before_get()
        start, end = map(int, byte_range.removeprefix("bytes=").split("-"))
        data = self.data[start:end + 1]
        destination.write_bytes(data)
        metadata = {"ContentLength": len(data), "ContentRange": f"bytes {start}-{end}/{len(self.data)}", "ETag": '"version-a"'}
        if self.after_get:
            self.after_get(destination, metadata)
        return metadata


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.connection = {"id": "photos", "name": "Photos", "bucket": "photo-bucket", "profile": "photos-profile", "region": "us-east-1"}
        self.paths.prepare()
        with turtle.Store(self.paths).update() as state:
            state["connections"] = [self.connection]
        self.reader = Reader()
        self.queue = offline.OfflineQueue(self.connection, self.paths, self.reader, chunk_bytes=8)
        self.identity = {"key": "shoot/photo.jpg", "etag": '"version-a"', "size": len(self.reader.data)}
        offline._stop_requested = False
        self.addCleanup(setattr, offline, "_stop_requested", False)

    def enqueue(self):
        result = self.queue.enqueue([self.identity], start=False)
        self.id = result["items"][0]["id"]
        return self.id

    def download(self):
        self.enqueue()
        self.queue.worker()
        return self.queue.status()["items"][0]

    def test_multi_photo_queue_is_durable_private_and_deduplicates_versions(self):
        first = self.identity
        second = dict(first, key="other/photo.jpg")
        result = self.queue.enqueue([first, second, first], start=False)
        self.assertEqual(len(result["items"]), 2)
        self.assertFalse(self.reader.gets)
        again = offline.OfflineQueue(self.connection, self.paths, self.reader).status()
        self.assertEqual(again["items"], result["items"])
        self.assertEqual(self.queue.queue_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.queue.originals.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.queue.originals.is_relative_to(self.paths.cache))

    def test_verified_completion_requires_remote_checksum_and_local_reread(self):
        item = self.download()
        self.assertEqual(item["state"], "verified")
        self.assertEqual(item["bytesDownloaded"], len(self.reader.data))
        self.assertEqual(Path(item["path"]).read_bytes(), self.reader.data)
        self.assertEqual(item["sha256"], hashlib.sha256(self.reader.data).hexdigest())
        self.assertEqual([entry[2] for entry in self.reader.gets], ["bytes=0-7", "bytes=8-15", "bytes=16-21"])
        self.assertEqual(len(self.reader.heads), 2)
        self.assertEqual(self.queue.open(item["id"])["path"], item["path"])
        self.assertEqual(len(self.reader.heads), 2, "Open is entirely local")

    def test_unsupported_or_composite_checksums_are_not_claimed_verified(self):
        for kind in (None, "composite"):
            with self.subTest(kind=kind):
                self.reader.checksum = kind
                self.identity["key"] = str(kind) + "/photo.jpg"
                self.queue.enqueue([self.identity], start=False)
                self.queue.worker()
                item = self.queue.status()["items"][-1]
                self.assertEqual(item["state"], "downloaded")
                self.assertIn("did not provide", item["verification"])
                self.assertEqual(self.queue.verify(item["id"])["items"][-1]["state"], "downloaded")

    def test_full_crc32_and_crc32c_checksums_are_supported(self):
        self.assertEqual(offline.crc32c_update(0xffffffff, b"123456789") ^ 0xffffffff, 0xe3069283)
        for kind in ("crc32", "crc32c"):
            self.reader.checksum = kind
            self.identity["key"] = kind + ".jpg"
            self.queue.enqueue([self.identity], start=False)
            self.queue.worker()
            self.assertEqual(self.queue.status()["items"][-1]["state"], "verified")

    def test_bad_remote_checksum_is_hard_failure_and_never_published(self):
        self.reader.checksum = "bad"
        item = self.download()
        self.assertEqual(item["state"], "error")
        self.assertIn("checksum", item["error"])
        self.assertIsNone(item["path"])
        self.assertFalse(self.queue.target(item).exists())
        self.assertTrue(self.queue.target(item, partial=True).exists())

    def test_pause_cancels_uncommitted_chunk_and_resume_uses_verified_prefix(self):
        self.enqueue()
        def pause_second():
            if len(self.reader.gets) == 2:
                self.queue.pause()
        self.reader.before_get = pause_second
        self.queue.worker()
        item = self.queue.status()["items"][0]
        self.assertTrue(self.queue.status()["paused"])
        self.assertEqual((item["state"], item["bytesDownloaded"]), ("paused", 8))
        self.reader.before_get = None
        resumed = offline.OfflineQueue(self.connection, self.paths, self.reader, chunk_bytes=8)
        resumed.resume(start=False)
        resumed.worker()
        self.assertEqual(resumed.status()["items"][0]["state"], "verified")
        self.assertEqual([r[2] for r in self.reader.gets], ["bytes=0-7", "bytes=8-15", "bytes=8-15", "bytes=16-21"])

    def test_cancel_signal_commits_current_prefix_before_safe_stop(self):
        self.enqueue()
        self.reader.after_get = lambda *_: offline.cancel()
        self.queue.worker()
        item = self.queue.status()["items"][0]
        self.assertEqual((item["state"], item["bytesDownloaded"]), ("paused", 0))
        self.assertEqual(self.queue.target(item, partial=True).stat().st_size, 0)

    def test_changed_partial_preserved_and_not_resumed(self):
        self.enqueue()
        self.reader.before_get = lambda: self.queue.pause() if len(self.reader.gets) == 2 else None
        self.queue.worker()
        item = self.queue.read()["items"][0]
        partial = self.queue.target(item, partial=True)
        partial.write_bytes(b"tampered")
        self.reader.before_get = None
        self.queue.resume(start=False)
        self.queue.worker()
        item = self.queue.status()["items"][0]
        self.assertEqual(item["state"], "error")
        self.assertIn("changed locally", item["error"])
        self.assertEqual(partial.read_bytes(), b"tampered")
        self.assertEqual(len(self.reader.gets), 2)

    def test_same_size_original_edit_invalidates_status_date_lookup_and_open(self):
        item = self.download()
        path = Path(item["path"])
        self.assertEqual(offline.offline_original(self.connection, self.paths, **self.identity), path)
        path.write_bytes(b"x" * len(self.reader.data))
        self.assertEqual(self.queue.status()["items"][0]["state"], "error")
        self.assertIsNone(offline.offline_original(self.connection, self.paths, **self.identity))
        with self.assertRaisesRegex(offline.OfflineError, "changed"):
            self.queue.open(item["id"])
        self.assertEqual(path.read_bytes(), b"x" * len(self.reader.data))

    def test_status_rechecks_touched_original_once_without_remote_reads(self):
        item = self.download()
        path = Path(item["path"])
        os.utime(path, (1, 1))
        with patch.object(offline, "hash_file", wraps=offline.hash_file) as digest, \
                patch.object(self.reader, "head", side_effect=AssertionError("remote read")), \
                patch.object(self.reader, "get", side_effect=AssertionError("remote read")):
            self.assertEqual(self.queue.status()["items"][0]["state"], "verified")
            self.assertEqual(self.queue.status()["items"][0]["state"], "verified")
            digest.assert_called_once_with(path, cancelled=offline.check_local_cancelled)
        self.assertEqual(self.queue.read()["items"][0]["localMtimeNs"], path.stat().st_mtime_ns)
        self.assertEqual(len(self.reader.gets), 3)

    @unittest.skipUnless(Path("/usr/bin/xattr").is_file(), "macOS extended attributes")
    def test_preview_style_extended_attribute_keeps_verified_copy_without_attention(self):
        item = self.download()
        path = Path(item["path"])
        before = path.stat()
        subprocess.run(["/usr/bin/xattr", "-w", "com.mountainturtle.offline-test", "metadata-only", str(path)],
                       check=True, capture_output=True)
        after = path.stat()
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
        self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
        with patch.object(offline, "hash_file", wraps=offline.hash_file) as digest, \
                patch.object(self.reader, "head", side_effect=AssertionError("remote read")), \
                patch.object(self.reader, "get", side_effect=AssertionError("remote read")):
            status = self.queue.status()
            self.assertEqual(status["items"][0]["state"], "verified")
            self.assertEqual(status["totals"]["errors"], 0)
            self.assertEqual(status["items"][0]["path"], str(path))
            self.assertEqual(self.queue.status()["totals"]["errors"], 0)
            digest.assert_called_once_with(path, cancelled=offline.check_local_cancelled)
        self.assertEqual(offline.offline_original(self.connection, self.paths, **self.identity), path)

    def test_status_recheck_preserves_unverified_classification(self):
        self.reader.checksum = None
        item = self.download()
        os.utime(item["path"], (1, 1))
        status = self.queue.status()
        self.assertEqual(status["items"][0]["state"], "downloaded")
        self.assertEqual(status["totals"]["verified"], 0)
        self.assertEqual(status["totals"]["errors"], 0)

    def test_status_recheck_is_allowed_while_download_queue_is_paused(self):
        item = self.download()
        self.queue.pause()
        os.utime(item["path"], (1, 1))
        status = self.queue.status()
        self.assertTrue(status["paused"])
        self.assertEqual(status["items"][0]["state"], "verified")

    def test_cancelled_status_recheck_preserves_completion_for_next_poll(self):
        item = self.download()
        os.utime(item["path"], (1, 1))
        offline.cancel()
        with self.assertRaises(offline.Paused):
            self.queue.status()
        self.assertEqual(self.queue.read()["items"][0]["state"], "verified")
        offline._stop_requested = False
        self.assertEqual(self.queue.status()["items"][0]["state"], "verified")

    def test_status_detects_and_persists_content_change_without_repeated_hashing(self):
        item = self.download()
        path = Path(item["path"])
        path.write_bytes(b"x" * len(self.reader.data))
        with patch.object(offline, "hash_file", wraps=offline.hash_file) as digest:
            self.assertEqual(self.queue.status()["items"][0]["state"], "error")
            self.assertEqual(self.queue.status()["items"][0]["state"], "error")
            digest.assert_called_once_with(path, cancelled=offline.check_local_cancelled)
        self.assertEqual(path.read_bytes(), b"x" * len(self.reader.data))

    def test_concurrent_status_polls_share_one_recheck(self):
        item = self.download()
        path = Path(item["path"])
        os.utime(path, (1, 1))
        started, release = threading.Event(), threading.Event()
        original_hash = offline.hash_file
        results, failures = [], []
        def delayed_hash(*args, **kwargs):
            started.set()
            if not release.wait(2):
                raise AssertionError("test hash was not released")
            return original_hash(*args, **kwargs)
        def poll():
            try:
                results.append(self.queue.status())
            except Exception as error:
                failures.append(error)
        with patch.object(offline, "hash_file", side_effect=delayed_hash) as digest:
            first = threading.Thread(target=poll)
            second = threading.Thread(target=poll)
            first.start()
            self.assertTrue(started.wait(2))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual([result["items"][0]["state"] for result in results], ["verified", "verified"])
            digest.assert_called_once_with(path, cancelled=offline.check_local_cancelled)

    def test_changed_s3_identity_fails_before_download(self):
        self.identity["etag"] = '"old-version"'
        item = self.download()
        self.assertEqual(item["state"], "error")
        self.assertIn("changed in S3", item["error"])
        self.assertFalse(self.reader.gets)

    def test_short_range_wrong_range_and_wrong_version_fail_before_append(self):
        for key, replacement in (("ContentRange", "bytes 3-10/22"), ("ETag", '"changed"'), ("ContentLength", 7)):
            self.identity["key"] = key + ".jpg"
            self.reader.after_get = lambda path, response, key=key, replacement=replacement: response.update({key: replacement})
            self.queue.enqueue([self.identity], start=False)
            self.queue.worker()
            item = self.queue.status()["items"][-1]
            self.assertEqual((item["state"], item["bytesDownloaded"]), ("error", 0))
            self.assertEqual(self.queue.target(item, partial=True).stat().st_size, 0)

    def test_same_length_chunk_corruption_fails_remote_checksum(self):
        self.reader.after_get = lambda path, _: path.write_bytes(b"x" * path.stat().st_size)
        item = self.download()
        self.assertEqual(item["state"], "error")
        self.assertIn("checksum", item["error"])

    def test_no_remote_checksum_still_detects_disk_corruption_during_publication(self):
        self.reader.checksum = None
        original_link = os.link
        def corrupt_link(source, destination, **kwargs):
            original_link(source, destination, **kwargs)
            destination.write_bytes(b"x" * len(self.reader.data))
        with patch.object(offline.os, "link", side_effect=corrupt_link):
            item = self.download()
        self.assertEqual(item["state"], "error")
        self.assertIn("changed", item["error"])

    def test_existing_destination_is_never_overwritten(self):
        self.enqueue()
        item = self.queue.read()["items"][0]
        target = self.queue.target(item)
        target.write_bytes(b"personal edits")
        self.queue.worker()
        self.assertEqual(target.read_bytes(), b"personal edits")
        self.assertEqual(self.queue.status()["items"][0]["state"], "error")
        self.assertFalse(self.reader.gets)

    def test_racing_destination_creation_is_never_overwritten(self):
        self.enqueue()
        target = self.queue.target(self.queue.read()["items"][0])
        self.reader.after_get = lambda *_: target.write_bytes(b"personal edits")
        self.queue.worker()
        self.assertEqual(target.read_bytes(), b"personal edits")
        self.assertEqual(self.queue.status()["items"][0]["state"], "error")

    def test_low_disk_space_stops_without_committing_a_chunk(self):
        with patch.object(offline.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1})()):
            item = self.download()
        self.assertEqual(item["state"], "error")
        self.assertIn("free space", item["error"])
        self.assertFalse(self.reader.gets)

    def test_second_worker_cannot_download_while_first_owns_lock(self):
        self.enqueue()
        with self.queue.lock("worker.lock"):
            other = offline.OfflineQueue(self.connection, self.paths, self.reader)
            self.assertTrue(other.status()["workerRunning"])
            other.worker()
            self.assertFalse(self.reader.gets)
        self.queue.worker()
        self.assertEqual(self.queue.status()["items"][0]["state"], "verified")

    def test_pause_survives_enqueue_retry_and_worker_launch(self):
        self.enqueue()
        self.queue.pause()
        with patch.object(offline.subprocess, "Popen") as launch:
            self.queue.enqueue([dict(self.identity, key="new.jpg")])
            self.queue.retry(self.id)
            self.queue.worker()
            launch.assert_not_called()
        self.assertFalse(self.reader.gets)

    def test_retry_resumes_valid_prefix_after_transient_failure(self):
        self.enqueue()
        def unavailable():
            if len(self.reader.gets) == 2:
                raise offline.OfflineError("Network unavailable")
        self.reader.before_get = unavailable
        self.queue.worker()
        self.assertEqual(self.queue.status()["items"][0]["bytesDownloaded"], 8)
        self.reader.before_get = None
        self.queue.retry(self.id, start=False)
        self.queue.worker()
        self.assertEqual(self.queue.status()["items"][0]["state"], "verified")
        self.assertEqual(self.reader.gets[2][2], "bytes=8-15")

    def test_symlink_partial_and_hardlink_original_are_rejected(self):
        self.enqueue()
        item = self.queue.read()["items"][0]
        outside = Path(self.temp.name) / "personal.jpg"
        outside.write_bytes(b"personal")
        self.queue.target(item, partial=True).symlink_to(outside)
        self.queue.worker()
        self.assertEqual(self.queue.status()["items"][0]["state"], "error")
        self.assertEqual(outside.read_bytes(), b"personal")
        with self.assertRaises(offline.OfflineError):
            with offline.regular_file(self.queue.target(item, partial=True)):
                pass

    def test_symlink_parent_rejected_before_queue_or_cloud_access(self):
        self.queue.base.parent.mkdir(parents=True, exist_ok=True)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.queue.base.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(offline.OfflineError, "unsafe"):
            self.enqueue()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.reader.gets)

    def test_malicious_keys_are_not_used_as_paths(self):
        self.identity["key"] = "../../personal/secret.jpg"
        item = self.download()
        self.assertEqual(Path(item["path"]).parent, self.queue.originals)
        self.assertEqual(Path(item["path"]).read_bytes(), self.reader.data)

    def test_invalid_identities_fail_atomically_before_queue_creation(self):
        for fields in ({"etag": ""}, {"size": True}, {"size": -1}, {"key": "bad\x00.jpg"}):
            with self.assertRaises(offline.OfflineError):
                self.queue.enqueue([self.identity, dict(self.identity, **fields)], start=False)
        self.assertFalse(self.queue.queue_path.exists())

    def test_empty_object_has_verified_checksum_without_invalid_range(self):
        self.reader.data = b""
        self.identity["size"] = 0
        item = self.download()
        self.assertEqual(item["state"], "verified")
        self.assertEqual(Path(item["path"]).read_bytes(), b"")
        self.assertFalse(self.reader.gets)

    def test_date_lookup_is_local_only_and_version_scoped(self):
        self.assertIsNone(offline.offline_original(self.connection, self.paths, **self.identity))
        self.assertFalse(self.queue.base.exists())
        item = self.download()
        with patch.object(self.reader, "head", side_effect=AssertionError("network")):
            self.assertEqual(offline.offline_original(self.connection, self.paths, **self.identity), Path(item["path"]))
        self.assertIsNone(offline.offline_original(self.connection, self.paths, **dict(self.identity, etag='"other"')))
        self.assertIsNone(offline.offline_original(dict(self.connection, bucket="different"), self.paths, **self.identity))

    def test_update_pauses_workers_and_restores_only_running_intent(self):
        self.enqueue()
        offline.pause_for_update(self.paths, time.monotonic() + 2)
        self.assertTrue(self.queue.status()["paused"])
        self.queue.worker()
        self.assertFalse(self.reader.gets)
        with patch.object(offline.OfflineQueue, "start_worker") as launch:
            offline.resume_after_update(self.paths)
            launch.assert_called_once()
        self.assertFalse(self.queue.status()["paused"])
        self.queue.pause()
        offline.pause_for_update(self.paths, time.monotonic() + 2)
        with patch.object(offline.subprocess, "Popen") as launch:
            offline.resume_after_update(self.paths)
            launch.assert_not_called()
        self.assertTrue(self.queue.status()["paused"])

    def test_update_waits_for_worker_lock_and_blocks_resume(self):
        self.enqueue()
        with turtle.Store(self.paths).update() as state:
            state["updateHandoff"] = {"any": "active"}
        with self.assertRaisesRegex(RuntimeError, "update"):
            self.queue.resume(start=False)
        with self.queue.lock("worker.lock"):
            with self.assertRaisesRegex(RuntimeError, "still stopping"):
                offline.pause_for_update(self.paths, time.monotonic() - 1)

    def test_worker_spawn_disables_bytecode_and_is_detached(self):
        self.enqueue()
        with patch.object(offline.subprocess, "Popen") as launch:
            self.queue.start_worker()
        command = launch.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, "-B"])
        self.assertTrue(launch.call_args.kwargs["start_new_session"])

    def test_open_completed_photo_does_not_require_pausing_other_downloads(self):
        item = self.download()
        with self.queue.lock("worker.lock"), self.queue.lock("a" * 64 + ".lock"):
            result = self.queue.open(item["id"])
        self.assertEqual(result["path"], item["path"])

    def test_verify_currently_downloading_photo_refuses_item_lock(self):
        item = self.download()
        with self.queue.lock(item["id"] + ".lock"):
            with self.assertRaisesRegex(offline.OfflineError, "still downloading"):
                self.queue.verify(item["id"])

    def test_crash_after_publication_link_recovers_without_network_redownload(self):
        self.enqueue()
        original_unlink = Path.unlink
        def crash_before_unlink(path, *args, **kwargs):
            if path.name.endswith(".part"):
                raise KeyboardInterrupt()
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", new=crash_before_unlink):
            with self.assertRaises(KeyboardInterrupt):
                self.queue.worker()
        item = self.queue.read()["items"][0]
        self.assertEqual(self.queue.target(item).stat().st_nlink, 2)
        gets = len(self.reader.gets)
        self.queue.worker()
        self.assertEqual(self.queue.status()["items"][0]["state"], "verified")
        self.assertEqual(len(self.reader.gets), gets)
        self.assertEqual(self.queue.target(item).stat().st_nlink, 1)

    def test_crash_after_chunk_append_recovers_recorded_prefix_and_preserves_uncommitted_bytes(self):
        self.enqueue()
        original_change = self.queue.change
        def crash_commit(identity, **fields):
            if fields.get("bytesDownloaded") == 16:
                raise KeyboardInterrupt()
            return original_change(identity, **fields)
        with patch.object(self.queue, "change", side_effect=crash_commit):
            with self.assertRaises(KeyboardInterrupt):
                self.queue.worker()
        item = self.queue.read()["items"][0]
        self.assertEqual(item["bytesDownloaded"], 8)
        self.assertEqual(self.queue.target(item, partial=True).stat().st_size, 16)
        self.queue.worker()
        item = self.queue.read()["items"][0]
        self.assertEqual(item["state"], "verified")
        self.assertEqual(Path(item["recoveredCopy"]).read_bytes(), self.reader.data[:16])
        self.assertEqual(self.reader.gets[2][2], "bytes=8-15")

    def test_crash_recovery_refuses_locally_changed_recorded_prefix(self):
        self.enqueue()
        item = self.queue.read()["items"][0]
        partial = self.queue.target(item, partial=True)
        partial.write_bytes(b"notoriginal-tail")
        self.queue.change(self.id, bytesDownloaded=8, partialSHA256=hashlib.sha256(self.reader.data[:8]).hexdigest())
        self.queue.worker()
        self.assertEqual(self.queue.status()["items"][0]["state"], "error")
        self.assertEqual(partial.read_bytes(), b"notoriginal-tail")
        self.assertFalse(self.reader.gets)

    def test_retry_missing_completed_photo_requeues_but_never_replaces_changed_copy(self):
        item = self.download()
        path = Path(item["path"])
        path.unlink()
        self.queue.retry(item["id"], start=False)
        self.assertEqual(self.queue.status()["items"][0]["state"], "queued")
        self.queue.worker()
        self.assertEqual(self.queue.status()["items"][0]["state"], "verified")
        path.write_bytes(b"edited")
        self.queue.retry(item["id"], start=False)
        self.queue.worker()
        self.assertEqual(self.queue.status()["items"][0]["state"], "error")
        self.assertEqual(path.read_bytes(), b"edited")

    def test_profile_rename_retains_offline_library_and_date_lookup(self):
        item = self.download()
        changed = dict(self.connection, profile="new-profile")
        second = offline.OfflineQueue(changed, self.paths, self.reader)
        self.assertEqual(second.status()["items"][0]["path"], item["path"])
        self.assertEqual(offline.offline_original(changed, self.paths, **self.identity), Path(item["path"]))

    def test_metadata_limit_failure_preserves_readable_previous_queue(self):
        self.enqueue()
        before = self.queue.queue_path.read_bytes()
        with patch.object(offline, "MAX_QUEUE_BYTES", len(before) + 1):
            with self.assertRaisesRegex(offline.OfflineError, "metadata limit"):
                self.queue.enqueue([dict(self.identity, key="second.jpg")], start=False)
        self.assertEqual(self.queue.queue_path.read_bytes(), before)
        self.assertEqual(len(self.queue.status()["items"]), 1)

    def test_ordinary_launch_recovers_queue_hold_after_global_update_marker_was_cleared(self):
        self.enqueue()
        offline.pause_for_update(self.paths, time.monotonic() + 1)
        with patch.object(offline.OfflineQueue, "start_worker") as launch:
            result = turtle.resume_update(self.paths)
            self.assertFalse(result["resumed"])
            launch.assert_called_once()
        self.assertFalse(self.queue.status()["paused"])

    def test_write_after_hash_before_manifest_never_advertises_verified(self):
        item = self.download()
        path = Path(item["path"])
        original_hash = offline.hash_file
        def hash_then_edit(*args, **kwargs):
            hashes = original_hash(*args, **kwargs)
            path.write_bytes(b"x" * len(self.reader.data))
            return hashes
        with patch.object(offline, "hash_file", side_effect=hash_then_edit):
            result = self.queue.verify(item["id"])
        self.assertEqual(result["items"][0]["state"], "error")
        self.assertIsNone(result["items"][0]["path"])

    def test_complete_file_with_external_hardlink_is_not_treated_as_unchanged(self):
        item = self.download()
        path = Path(item["path"])
        os.link(path, Path(self.temp.name) / "linked.jpg")
        self.assertEqual(self.queue.status()["items"][0]["state"], "error")
        self.assertIsNone(offline.offline_original(self.connection, self.paths, **self.identity))
        with self.assertRaises(offline.OfflineError):
            self.queue.open(item["id"])


class AWSReaderTests(unittest.TestCase):
    def setUp(self):
        self.connection = {"bucket": "photos", "profile": "named-profile", "region": "us-west-2"}
        self.paths = turtle.Paths(home="/unused")
        self.reader = offline.AWSReader(self.connection, self.paths, lambda: None)

    def test_explicit_options_preserve_special_keys_and_use_conditional_ranges(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (b'{"ContentLength": 8}', b"")
        with patch.object(turtle, "executable", return_value="/usr/local/bin/aws"), \
                patch.object(turtle, "environment", return_value={}), \
                patch.object(offline.subprocess, "Popen", return_value=process) as spawn:
            self.reader.get("--unsafe +name.jpg", '"version"', Path("/tmp/range"), "bytes=0-7")
        command = spawn.call_args.args[0]
        self.assertIn("--key=--unsafe +name.jpg", command)
        self.assertIn('--if-match="version"', command)
        self.assertIn("--range=bytes=0-7", command)
        self.assertNotIn("--checksum-mode", command)
        self.assertEqual(command[-1], "/tmp/range")

    def test_checksum_permission_failure_retries_head_without_checksum_only(self):
        denied, allowed = Mock(returncode=1), Mock(returncode=0)
        denied.communicate.return_value = (b"", b"AccessDenied: KMS permission denied")
        allowed.communicate.return_value = (b'{"ContentLength": 3,"ETag":"version"}', b"")
        with patch.object(turtle, "executable", return_value="/usr/local/bin/aws"), \
                patch.object(turtle, "environment", return_value={}), \
                patch.object(offline.subprocess, "Popen", side_effect=[denied, allowed]) as spawn:
            self.reader.head("photo.jpg", "version")
        self.assertIn("--checksum-mode", spawn.call_args_list[0].args[0])
        self.assertNotIn("--checksum-mode", spawn.call_args_list[1].args[0])
        self.assertIn("--if-match=version", spawn.call_args_list[1].args[0])

    def test_pause_terminates_owned_process_group(self):
        process = Mock(pid=123, returncode=0)
        self.reader.cancelled = Mock(side_effect=offline.Paused("paused"))
        with patch.object(turtle, "executable", return_value="/usr/local/bin/aws"), \
                patch.object(turtle, "environment", return_value={}), \
                patch.object(offline.subprocess, "Popen", return_value=process), \
                patch.object(offline.os, "killpg") as kill:
            with self.assertRaises(offline.Paused):
                self.reader.head("photo.jpg", "version")
        kill.assert_called_once_with(123, offline.signal.SIGTERM)
        process.communicate.assert_called_once_with(timeout=2)


if __name__ == "__main__":
    unittest.main()
