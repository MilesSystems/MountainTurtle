import base64
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
# A synthetic one-pixel JPEG with no EXIF, encoded locally with ImageIO.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAASABIAAD/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA"
    "AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEI"
    "I0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlq"
    "c3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW"
    "19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL"
    "/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLR"
    "ChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE"
    "hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn"
    "6Onq8vP09fb3+Pn6/9sAQwACAgICAgIDAgIDBQMDAwUGBQUFBQYIBgYGBgYICggICAgICAoKCgoKCgoK"
    "DAwMDAwMDg4ODg4PDw8PDw8PDw8P/9sAQwECAgIEBAQHBAQHEAsJCxAQEBAQEBAQEBAQEBAQEBAQ"
    "EBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ/90ABAAB/9oADAMBAAIR"
    "AxEAPwD4vooor+Uz/fw//9k="
)


def exif_jpeg(original=None, digitized=None, modified=None, offset=None, endian="<"):
    """Build both TIFF byte orders without needing third-party image libraries."""
    pack = lambda fmt, *values: struct.pack(endian + fmt, *values)
    exif_values = [(tag, value.encode("ascii") + b"\0") for tag, value in
                   [(0x9003, original), (0x9004, digitized), (0x9011, offset)] if value is not None]
    main_count = 1 + (modified is not None)
    exif_start = 8 + 2 + 12 * main_count + 4
    strings_start = exif_start + 2 + 12 * len(exif_values) + 4
    strings = bytearray()

    def ascii_entry(tag, value):
        if len(value) <= 4:
            return pack("HHI", tag, 2, len(value)) + value.ljust(4, b"\0")
        position = strings_start + len(strings)
        strings.extend(value)
        return pack("HHII", tag, 2, len(value), position)

    main = []
    if modified is not None:
        main.append(ascii_entry(0x0132, modified.encode("ascii") + b"\0"))
    main.append(pack("HHII", 0x8769, 4, 1, exif_start))
    exif = [ascii_entry(tag, value) for tag, value in exif_values]
    tiff = ((b"II" if endian == "<" else b"MM") + pack("HI", 42, 8)
            + pack("H", len(main)) + b"".join(main) + pack("I", 0)
            + pack("H", len(exif)) + b"".join(exif) + pack("I", 0) + strings)
    payload = b"Exif\0\0" + tiff
    return JPEG[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + JPEG[2:]


def prefix_before_image_frame(photo):
    """Large editing metadata can put the JPEG frame past the bounded probe."""
    exif_end = 4 + struct.unpack_from(">H", photo, 4)[0]
    xmp = b"http://ns.adobe.com/xap/1.0/\0".ljust(65533, b"x")
    segment = b"\xff\xe1" + struct.pack(">H", len(xmp) + 2) + xmp
    return (photo[:exif_end] + segment * 3 + photo[exif_end:])[:128 * 1024]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "requires macOS ImageIO and Swift")
class PhotoDatesHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True, text=True)
        if compiler.returncode:
            raise unittest.SkipTest("requires Apple's Command Line Tools")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.helper = cls.directory / "Mountain Turtle Photo Dates"
        subprocess.run(["xcrun", "swiftc", "-O", "-swift-version", "5",
                        "-framework", "Foundation", "-framework", "ImageIO",
                        str(ROOT / "Sources/PhotoDates/main.swift"), "-o", str(cls.helper)],
                       check=True, capture_output=True, text=True, timeout=120)

    def extract(self, contents, name="photo.jpg", env=None, include_status=False):
        source = self.directory / name
        source.write_bytes(contents)
        # Distinct local dates must never become the capture date.
        os.utime(source, (946684800, 946684800))
        result = subprocess.run([str(self.helper), str(source)], capture_output=True,
                                text=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertIsInstance(response["metadataReadable"], bool)
        return response if include_status else {"dateTaken": response["dateTaken"]}

    def test_readable_photo_without_exif_is_distinct_from_unreadable_input(self):
        self.assertEqual(self.extract(JPEG, include_status=True),
                         {"dateTaken": None, "metadataReadable": True})
        for contents in (b"", b"not an image", JPEG[:4]):
            with self.subTest(contents=contents):
                self.assertEqual(self.extract(contents, include_status=True),
                                 {"dateTaken": None, "metadataReadable": False})

    def test_bounded_prefix_distinguishes_readable_metadata_from_valid_camera_date(self):
        prefix = prefix_before_image_frame(exif_jpeg(digitized="2026:09:11 13:34:50"))
        # ImageIO can inspect a partial metadata dictionary on some macOS versions.
        # The browser must still use its full-file flag before claiming no date.
        self.assertIsNone(self.extract(prefix, include_status=True)["dateTaken"])
        dated = prefix_before_image_frame(exif_jpeg("2026:09:11 13:34:50"))
        self.assertEqual(self.extract(dated, include_status=True),
                         {"dateTaken": "2026-09-11T13:34:50", "metadataReadable": True})

    def test_original_date_in_both_tiff_byte_orders(self):
        for endian in ("<", ">"):
            with self.subTest(endian=endian):
                self.assertEqual(self.extract(exif_jpeg("2026:09:11 13:34:00", endian=endian)),
                                 {"dateTaken": "2026-09-11T13:34:00"})

    def test_ignores_digitized_modified_and_filesystem_dates(self):
        self.assertEqual(self.extract(exif_jpeg(digitized="2026:09:18 20:16:00",
                                               modified="2026:09:18 20:16:00")), {"dateTaken": None})
        self.assertEqual(self.extract(JPEG), {"dateTaken": None})

    def test_original_takes_precedence_over_other_metadata(self):
        self.assertEqual(self.extract(exif_jpeg("2026:09:11 13:34:00",
                                               digitized="2026:09:18 20:16:00",
                                               modified="2026:09:18 20:16:00")),
                         {"dateTaken": "2026-09-11T13:34:00"})

    def test_preserves_camera_wall_time_across_system_timezones(self):
        for timezone in ("Pacific/Kiritimati", "America/Denver", "Pacific/Honolulu"):
            with self.subTest(timezone=timezone):
                self.assertEqual(self.extract(exif_jpeg("2026:09:11 00:34:00", offset="+14:00"),
                                              env=dict(os.environ, TZ=timezone)),
                                 {"dateTaken": "2026-09-11T00:34:00"})

    def test_valid_leap_days_and_year_boundaries(self):
        for original in ("2000:02:29 23:59:59", "2024:02:29 00:00:00", "0001:01:01 00:00:00",
                         "9999:12:31 23:59:59"):
            with self.subTest(original=original):
                expected = original[:10].replace(":", "-") + "T" + original[11:]
                self.assertEqual(self.extract(exif_jpeg(original)), {"dateTaken": expected})

    def test_rejects_impossible_dates_without_falling_back(self):
        for original in ("0000:00:00 00:00:00", "0000:01:01 00:00:00", "2026:00:11 13:34:00",
                         "2026:13:11 13:34:00", "2026:09:00 13:34:00", "2026:04:31 13:34:00",
                         "1900:02:29 13:34:00", "2026:02:29 13:34:00", "2026:09:11 24:00:00",
                         "2026:09:11 13:60:00", "2026:09:11 13:34:60"):
            with self.subTest(original=original):
                self.assertEqual(self.extract(exif_jpeg(original, digitized="2026:09:18 20:16:00")),
                                 {"dateTaken": None})

    def test_rejects_malformed_date_strings(self):
        for original in ("", " ", "2026-09-11 13:34:00",
                         "2026:09:11T13:34:00", "2026:09:11 13:34:00Z", "2026:09:11 13:34",
                         "xxxx:09:11 13:34:00", "2026:09:11 13:34:00 "):
            with self.subTest(original=original):
                self.assertEqual(self.extract(exif_jpeg(original)), {"dateTaken": None})

    def test_imageio_can_normalize_an_unpadded_real_date(self):
        # ImageIO pads a single-digit month before exposing DateTimeOriginal.
        # This preserves the real camera date; impossible dates still fail above.
        self.assertEqual(self.extract(exif_jpeg("2026:9:11 13:34:00")),
                         {"dateTaken": "2026-09-11T13:34:00"})

    def test_reads_a_128_kib_jpeg_prefix_without_decoding_pixels(self):
        photo = exif_jpeg("2026:09:11 13:34:00")
        # Extend the scan payload so the input ends before the end-of-image marker.
        prefix = (photo[:-2] + b"\0" * 200_000 + photo[-2:])[:128 * 1024]
        self.assertEqual(len(prefix), 128 * 1024)
        self.assertEqual(self.extract(prefix, name="prefix.part"), {"dateTaken": "2026-09-11T13:34:00"})

    def test_reads_exif_before_truncated_large_xmp_and_missing_image_frame(self):
        # Regression: real edited JPEGs can have >128 KiB of XMP before SOF.
        # ImageIO gives no properties without SOF, although EXIF is complete.
        for endian in ("<", ">"):
            with self.subTest(endian=endian):
                prefix = prefix_before_image_frame(exif_jpeg("2026:09:11 13:34:50", endian=endian))
                self.assertEqual(len(prefix), 128 * 1024)
                self.assertNotIn(b"\xff\xc0", prefix)
                self.assertEqual(self.extract(prefix), {"dateTaken": "2026-09-11T13:34:50"})

    def test_bounded_exif_fallback_rejects_offsets_counts_and_truncated_segments(self):
        for endian in ("<", ">"):
            original = exif_jpeg("2026:09:11 13:34:50", endian=endian)
            # Offsets and counts are relative to the TIFF that begins at byte 12.
            for location, kind, value in ((16, "I", 0xffffffff), (20, "H", 65535),
                                          (30, "I", 0xffffffff), (38, "H", 65535),
                                          (44, "I", 19), (44, "I", 0xffffffff),
                                          (48, "I", 0xffffffff), (48, "I", 0)):
                with self.subTest(endian=endian, location=location, value=value):
                    malformed = bytearray(original)
                    struct.pack_into(endian + kind, malformed, location, value)
                    self.assertEqual(self.extract(prefix_before_image_frame(malformed)), {"dateTaken": None})
            exif_end = 4 + struct.unpack_from(">H", original, 4)[0]
            self.assertEqual(self.extract(original[:exif_end - 1]), {"dateTaken": None})

    def test_bounded_exif_fallback_never_uses_other_or_invalid_dates(self):
        for photo in (exif_jpeg(digitized="2026:09:11 13:34:50", modified="2026:09:11 13:34:50"),
                      exif_jpeg("2026:02:30 13:34:50"), exif_jpeg("0000:00:00 00:00:00")):
            self.assertEqual(self.extract(prefix_before_image_frame(photo)), {"dateTaken": None})

    def test_invalid_and_truncated_inputs_return_unknown(self):
        photo = exif_jpeg("2026:09:11 13:34:00")
        for contents in (b"", b"not an image", b"\0" * 1024, photo[:4], photo[:30], photo[:55]):
            with self.subTest(length=len(contents)):
                self.assertEqual(self.extract(contents), {"dateTaken": None})

    def test_invocation_and_read_errors_exit_nonzero(self):
        for arguments in ([], [str(self.directory / "missing.jpg")], [str(self.directory)],
                          [str(self.directory / "photo.jpg"), "extra"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([str(self.helper), *arguments], capture_output=True,
                                        text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.strip())


if __name__ == "__main__":
    unittest.main()
