"""Exercise the app's actual Foundation-only date display and ordering code."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "Sources/PhotoDateSorting.swift"
HARNESS = r'''
import Foundation

func photo(_ key: String, _ name: String) -> PhotoItem {
    PhotoItem(key: key, name: name, size: 100, etag: "version-one")
}

let old = photo("old", "Photo10.jpg")
let same2 = photo("same2", "Photo2.jpg")
let same1 = photo("same1", "Photo2.jpg")
let middle = photo("middle", "Photo10.jpg")
let unknownZ = photo("unknown-z", "Photo20.jpg")
let unknownA = photo("unknown-a", "Photo3.jpg")
let invalid = photo("invalid", "Photo4.jpg")
let photos = [unknownZ, same2, old, invalid, middle, unknownA, same1]
let dates: [PhotoIdentity: PhotoTakenDate] = [
    old.identity: PhotoTakenDate("2025-12-31T23:59:59"),
    same2.identity: PhotoTakenDate("2026-09-11T13:34:00"),
    same1.identity: PhotoTakenDate("2026-09-11T13:34:00"),
    middle.identity: PhotoTakenDate("2026-09-11T13:34:00"),
    unknownZ.identity: PhotoTakenDate(nil),
    invalid.identity: PhotoTakenDate("2026-02-30T12:00:00"),
]
let invalidValues: [String?] = [nil, "", "2026-02-30T01:02:03", "2026-13-01T00:00:00",
    "2026-09-11T25:34:00", "2026-09-11T13:34:00Z", "2026:09:11 13:34:00", "2026-9-1T13:34:00"]
let sample = PhotoTakenDate("2026-09-11T13:34:00")
let output: [String: Any] = [
    "newest": PhotoDateOrdering.sorted(photos, by: .newest, dates: dates).map(\.key),
    "oldest": PhotoDateOrdering.sorted(photos, by: .oldest, dates: dates).map(\.key),
    "name": PhotoDateOrdering.sorted(photos, by: .name, dates: dates).map(\.key),
    "invalidLabels": invalidValues.map { PhotoTakenDate($0).label(locale: Locale(identifier: "en_US")) },
    "canonical": sample.value ?? "missing",
    "label": sample.label(locale: Locale(identifier: "en_US")),
    "leapDate": PhotoTakenDate("2024-02-29T00:15:00").value ?? "missing",
    "versionIdentityDiffers": old.identity != PhotoIdentity(key: old.key, etag: "version-two", size: old.size),
    "sizeIdentityDiffers": old.identity != PhotoIdentity(key: old.key, etag: old.etag, size: 101),
    "keyIdentityDiffers": old.identity != PhotoIdentity(key: "other", etag: old.etag, size: old.size),
    "changedVersionHasNoDate": dates[PhotoIdentity(key: old.key, etag: "version-two", size: old.size)] == nil,
]
let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
print(String(decoding: data, as: UTF8.self))
'''


class PhotoDateSortingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            raise unittest.SkipTest("Swift is required for the native date sorting tests")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        harness = directory / "main.swift"
        harness.write_text(HARNESS)
        cls.executable = directory / "photo-date-tests"
        subprocess.run(
            [swiftc, "-swift-version", "5", str(SOURCE), str(harness), "-o", str(cls.executable)],
            check=True, capture_output=True, text=True, timeout=90,
        )
        cls.result = cls.run_harness("UTC")

    @classmethod
    def run_harness(cls, timezone):
        environment = dict(os.environ, TZ=timezone)
        output = subprocess.run(
            [str(cls.executable)], env=environment, check=True, capture_output=True, text=True, timeout=10,
        )
        return json.loads(output.stdout)

    def test_newest_keeps_unknown_last_and_breaks_date_ties_by_name_then_key(self):
        self.assertEqual(self.result["newest"], ["same1", "same2", "middle", "old", "unknown-a", "invalid", "unknown-z"])

    def test_oldest_keeps_unknown_last_without_reversing_tie_breaks(self):
        self.assertEqual(self.result["oldest"], ["old", "same1", "same2", "middle", "unknown-a", "invalid", "unknown-z"])

    def test_name_sort_ignores_dates_and_uses_natural_filename_order(self):
        self.assertEqual(self.result["name"], ["same1", "same2", "unknown-a", "invalid", "middle", "old", "unknown-z"])

    def test_missing_invalid_or_noncanonical_dates_remain_unknown(self):
        self.assertEqual(self.result["invalidLabels"], ["Date taken: Unknown"] * 8)
        self.assertEqual(self.result["canonical"], "2026-09-11T13:34:00")
        self.assertEqual(self.result["leapDate"], "2024-02-29T00:15:00")

    def test_display_keeps_camera_wall_time_in_different_system_timezones(self):
        label = self.result["label"].replace("\u202f", " ").replace("\u00a0", " ")
        self.assertIn("Sep 11, 2026", label)
        self.assertIn("1:34 PM", label)
        for timezone in ("America/Denver", "Pacific/Kiritimati", "Pacific/Honolulu"):
            with self.subTest(timezone=timezone):
                self.assertEqual(self.run_harness(timezone)["label"], self.result["label"])

    def test_metadata_identity_does_not_cross_object_versions_keys_or_sizes(self):
        for field in ("versionIdentityDiffers", "sizeIdentityDiffers", "keyIdentityDiffers", "changedVersionHasNoDate"):
            with self.subTest(field=field):
                self.assertTrue(self.result[field])


if __name__ == "__main__":
    unittest.main()
