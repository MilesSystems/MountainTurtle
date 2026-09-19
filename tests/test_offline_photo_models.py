"""Exercise native queue presentation against service payloads, without the app."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
import Foundation

func item(_ state: String, bytes: Int64 = 50, path: String? = nil, etag: String = "v1") throws -> OfflinePhotoItem {
    var object: [String: Any] = ["id": state, "key": "Photos/a.jpg", "name": "a.jpg", "etag": etag,
        "size": 100, "state": state, "bytesDownloaded": bytes, "verifiedAt": 12345.5]
    if let path { object["path"] = path }
    let data = try JSONSerialization.data(withJSONObject: object)
    return try JSONDecoder().decode(OfflinePhotoItem.self, from: data)
}
let verified = try item("verified", bytes: 100, path: "/offline/a.jpg")
let downloaded = try item("downloaded", bytes: 100, path: "/offline/a.jpg")
let missing = try item("verified", bytes: 100)
let downloading = try item("downloading", bytes: 100)
let queue = OfflinePhotoQueue(paused: true, workerRunning: false,
    items: [verified, downloaded, try item("queued"), try item("paused"), try item("error")])
let photo = PhotoItem(key: "Photos/a.jpg", name: "a.jpg", size: 100, etag: "v2")
let output: [String: Any] = [
    "verified": verified.isVerified, "downloadedVerified": downloaded.isVerified,
    "downloadedRetained": downloaded.hasOfflineCopy, "downloadedLabel": downloaded.stateLabel,
    "bytesAloneVerified": downloading.isVerified, "missingPathRetained": missing.hasOfflineCopy,
    "missingPathLabel": missing.stateLabel, "summary": queue.summary,
    "counts": [queue.pendingCount, queue.verifiedCount, queue.downloadedCount, queue.failedCount],
    "changedVersionMatched": queue.item(for: photo) != nil,
    "progressBounds": [try item("downloading", bytes: -1).progress, try item("downloading", bytes: 200).progress],
    "unknownStateVerified": try item("future", bytes: 100, path: "/offline/a.jpg").isVerified,
]
print(String(decoding: try JSONSerialization.data(withJSONObject: output), as: UTF8.self))
'''


class OfflinePhotoModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            raise unittest.SkipTest("Swift is required for native model tests")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        harness = directory / "main.swift"
        harness.write_text(HARNESS)
        executable = directory / "offline-model-tests"
        subprocess.run([swiftc, "-swift-version", "5", str(ROOT / "Sources/PhotoDateSorting.swift"),
                        str(ROOT / "Sources/OfflinePhotos.swift"), str(harness), "-o", str(executable)],
                       check=True, capture_output=True, text=True, timeout=90)
        cls.result = json.loads(subprocess.run([str(executable)], check=True, capture_output=True,
                                             text=True, timeout=10).stdout)

    def test_bytes_complete_or_local_download_does_not_claim_remote_verification(self):
        self.assertTrue(self.result["verified"])
        for key in ("downloadedVerified", "bytesAloneVerified", "unknownStateVerified"):
            self.assertFalse(self.result[key])
        self.assertTrue(self.result["downloadedRetained"])
        self.assertEqual(self.result["downloadedLabel"], "Kept offline · Not verified")

    def test_offline_claim_requires_a_retained_path(self):
        self.assertFalse(self.result["missingPathRetained"])
        self.assertEqual(self.result["missingPathLabel"], "Offline copy unavailable")

    def test_resume_summary_and_counts_include_only_pending_and_intact_copies(self):
        self.assertEqual(self.result["counts"], [2, 1, 2, 1])
        self.assertEqual(self.result["summary"], "2 kept offline · 2 paused · 1 need attention")

    def test_remote_replacement_does_not_inherit_old_offline_badge(self):
        self.assertFalse(self.result["changedVersionMatched"])

    def test_progress_is_bounded_for_untrusted_or_old_status_values(self):
        self.assertEqual(self.result["progressBounds"], [0, 1])


if __name__ == "__main__":
    unittest.main()
