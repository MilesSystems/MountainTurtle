import base64
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
import photo_browser as photo
import turtle_service as turtle


def exif_image(embedded=b"\xff\xd8thumbnail\xff\xd9", order="<"):
    tiff = (b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, 8)
    tiff += struct.pack(order + "HIH", 0, 14, 2)
    tiff += struct.pack(order + "HHII", 0x0201, 4, 1, 44)
    tiff += struct.pack(order + "HHII", 0x0202, 4, 1, len(embedded))
    tiff += struct.pack(order + "I", 0) + embedded
    segment = b"Exif\x00\x00" + tiff
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(segment) + 2) + segment + b"\xff\xd9"


class Reader:
    def __init__(self, data=b"original-data"):
        self.data, self.gets, self.lists = data, [], []
        self.page = {}

    def list(self, prefix, cursor, limit):
        self.lists.append((prefix, cursor, limit))
        return self.page

    def get(self, key, etag, destination, byte_range=None):
        self.gets.append((key, etag, byte_range))
        data = self.data
        if byte_range:
            begin, end = map(int, byte_range[6:].split("-"))
            data = data[begin:end + 1]
        destination.write_bytes(data)
        return {"ContentLength": len(data)}


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = turtle.Paths(home=self.temp.name)
        self.connection = {"id": "test-connection", "name": "Photos", "bucket": "sample-bucket",
                           "profile": "development", "region": "us-east-1", "readOnly": True}
        self.reader = Reader()
        self.renderer = Mock(side_effect=lambda source, target, pixels: target.write_bytes(b"rendered-thumbnail"))
        self.extractor = Mock(return_value=None)
        self.browser = photo.PhotoBrowser(self.connection, self.paths, self.reader, self.renderer, self.extractor)

    def test_sftp_is_rejected_before_aws_or_file_access(self):
        with self.assertRaisesRegex(photo.BrowserError, "available for S3"):
            photo.PhotoBrowser({"id": "test", "backend": "sftp"}, self.paths)

    def cache(self, data, key="Portfolio/photo.jpg", etag='"abc"', blocks=None, dirty=False):
        root = self.paths.cache / self.connection["id"]
        original = root / "vfs/volume" / key
        metadata = root / "vfsMeta/volume" / key
        original.parent.mkdir(parents=True, exist_ok=True)
        metadata.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(data)
        metadata.write_text(json.dumps({"Size": len(data), "Dirty": dirty,
                                       "Fingerprint": f'{len(data)},date,{etag.strip(chr(34))}',
                                       "Rs": blocks if blocks is not None else [{"Pos": 0, "Size": len(data)}]}))
        return original

    def test_list_returns_one_bounded_page_without_following_cursor(self):
        self.reader.page = {"CommonPrefixes": [{"Prefix": "Portfolio%2FPortraits%2F"}],
                            "Contents": [{"Key": "Portfolio%2Fa%20b.jpg", "Size": 99, "ETag": '"abc"'},
                                         {"Key": "Portfolio%2Fnotes.txt", "Size": 2}],
                            "EncodingType": "url", "IsTruncated": True, "NextContinuationToken": "next-page"}
        result = self.browser.list("Portfolio/", "previous", 100)
        self.assertEqual(self.reader.lists, [("Portfolio/", "previous", 100)])
        self.assertEqual(result["folders"], [{"key": "Portfolio/Portraits/", "name": "Portraits"}])
        self.assertEqual(result["photos"], [{"key": "Portfolio/a b.jpg", "name": "a b.jpg", "size": 99, "etag": '"abc"'}])
        self.assertEqual((result["nextCursor"], result["hiddenFileCount"]), ("next-page", 1))
        self.assertEqual(self.reader.gets, [])

    def test_invalid_page_never_reaches_reader(self):
        for limit in (0, 201, 10000):
            with self.assertRaises(photo.BrowserError):
                self.browser.list(limit=limit)
        self.assertEqual(self.reader.lists, [])

    def test_url_encoded_spaces_allow_folder_navigation_and_photo_reads(self):
        self.reader.page = {"CommonPrefixes": [{"Prefix": "Portfolio%2FDogs+FINAL%2F"}],
                            "EncodingType": "url"}
        folder = self.browser.list("Portfolio/")["folders"][0]
        self.assertEqual(folder, {"key": "Portfolio/Dogs FINAL/", "name": "Dogs FINAL"})
        self.reader.page = {"Contents": [{"Key": "Portfolio%2FDogs+FINAL%2Fa+b.jpg",
                                          "Size": len(self.reader.data), "ETag": '"abc"'}],
                            "EncodingType": "url"}
        image = self.browser.list(folder["key"])["photos"][0]
        self.assertEqual(self.reader.lists[-1], ("Portfolio/Dogs FINAL/", None, 100))
        self.assertEqual(image["key"], "Portfolio/Dogs FINAL/a b.jpg")
        self.assertEqual(image["name"], "a b.jpg")
        self.browser.thumbnail(image["key"], image["etag"], image["size"])
        self.assertEqual(self.reader.gets[0][0], "Portfolio/Dogs FINAL/a b.jpg")

    def test_url_encoded_literal_plus_is_preserved_after_one_decode(self):
        self.reader.page = {"CommonPrefixes": [{"Prefix": "Portfolio%2FDogs%2BFINAL%2F"}],
                            "Contents": [{"Key": "Portfolio%2Fa%2Bb+%252B.jpg",
                                          "Size": 99, "ETag": '"abc"'}],
                            "EncodingType": "url"}
        result = self.browser.list("Portfolio/")
        self.assertEqual(result["folders"], [{"key": "Portfolio/Dogs+FINAL/", "name": "Dogs+FINAL"}])
        self.assertEqual(result["photos"], [{"key": "Portfolio/a+b %2B.jpg", "name": "a+b %2B.jpg",
                                            "size": 99, "etag": '"abc"'}])

    def test_unencoded_list_preserves_literal_plus_and_percent_sequences(self):
        self.reader.page = {"CommonPrefixes": [{"Prefix": "Portfolio/Dogs+FINAL/"}],
                            "Contents": [{"Key": "Portfolio/a+b %2B.jpg", "Size": 99, "ETag": '"abc"'}]}
        result = self.browser.list("Portfolio/")
        self.assertEqual(result["folders"], [{"key": "Portfolio/Dogs+FINAL/", "name": "Dogs+FINAL"}])
        self.assertEqual(result["photos"], [{"key": "Portfolio/a+b %2B.jpg", "name": "a+b %2B.jpg",
                                            "size": 99, "etag": '"abc"'}])

    def test_missing_version_never_reads_remote_or_reuses_cache(self):
        for etag in ("", '""', " "):
            with self.assertRaises(photo.BrowserError):
                self.browser.thumbnail("photo.jpg", etag, 1000)
            with self.assertRaises(photo.BrowserError):
                self.browser.open_original("photo.jpg", etag, 1000)
            with self.assertRaises(photo.BrowserError):
                self.browser.date_taken("photo.jpg", etag, 1000)
        self.assertEqual(self.reader.gets, [])

    def test_aws_request_uses_one_page_if_match_and_no_mutations(self):
        reader = photo.AWSReader(self.connection, self.paths)
        with patch.object(turtle, "executable", return_value="/fake/aws"), patch.object(photo, "run_process", return_value=b"{}") as run:
            reader.list("Portfolio/", "abc", 100)
            command = run.call_args.args[0]
            self.assertIn("--no-paginate", command)
            payload = json.loads(command[command.index("--cli-input-json") + 1])
            self.assertEqual(payload, {"Bucket": "sample-bucket", "Prefix": "Portfolio/", "Delimiter": "/", "MaxKeys": 100, "EncodingType": "url", "ContinuationToken": "abc"})
            reader.get("Portfolio/photo.jpg", '"version"', Path(self.temp.name) / "output", "bytes=0-99")
            command = run.call_args.args[0]
            self.assertNotIn("--cli-input-json", command)
            self.assertIn("--bucket=sample-bucket", command)
            self.assertIn("--key=Portfolio/photo.jpg", command)
            self.assertIn('--if-match="version"', command)
            self.assertIn("--range=bytes=0-99", command)
            self.assertEqual(command[-1], str(Path(self.temp.name) / "output"))
            with self.assertRaises(photo.BrowserError):
                reader.request("put-object", {})
            self.assertEqual(run.call_count, 2)

    def test_get_object_keeps_leading_dash_and_shell_characters_in_one_argument(self):
        reader = photo.AWSReader(self.connection, self.paths)
        key = "--photo $(never-execute) & name.jpg"
        with patch.object(turtle, "executable", return_value="/fake/aws"), patch.object(photo, "run_process", return_value=b"{}") as run:
            reader.get(key, '"etag"', Path(self.temp.name) / "output")
        command = run.call_args.args[0]
        self.assertIn("--key=" + key, command)
        self.assertNotIn("--key", command)
        self.assertNotIn("--cli-input-json", command)
        self.assertFalse(any(arg.startswith("--range=") for arg in command))

    def test_extracts_both_endian_exif_thumbnails_and_rejects_truncation(self):
        expected = b"\xff\xd8thumbnail\xff\xd9"
        for order in ("<", ">"):
            image = exif_image(expected, order)
            self.assertEqual(photo.jpeg_thumbnail(image), expected)
            for boundary in (0, 1, 5, 20, len(image) - 4):
                self.assertIsNone(photo.jpeg_thumbnail(image[:boundary]))
        self.assertIsNone(photo.jpeg_thumbnail(b"not-jpeg"))

    def test_embedded_thumbnail_only_fetches_bounded_header_and_reuses_derivative(self):
        self.reader.data = exif_image() + b"x" * 200000
        first = self.browser.thumbnail("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual((first["source"], first["downloadedBytes"]), ("embedded-jpeg", photo.HEADER_BYTES))
        self.assertEqual(self.reader.gets, [("photo.jpg", '"abc"', f"bytes=0-{photo.HEADER_BYTES - 1}")])
        self.assertTrue(Path(first["thumbnailPath"]).is_file())
        second = self.browser.thumbnail("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual((second["source"], second["downloadedBytes"]), ("thumbnail-cache", 0))
        self.assertEqual(len(self.reader.gets), 1)
        self.assertEqual(self.renderer.call_count, 1)

    def test_small_jpeg_probe_can_render_complete_bounded_object(self):
        self.reader.data = b"\xff\xd8small-jpeg\xff\xd9"
        result = self.browser.thumbnail("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual(result["source"], "bounded-jpeg")
        self.assertEqual(len(self.reader.gets), 1)

    def test_no_embedded_thumbnail_does_not_fetch_full_original_by_default(self):
        self.reader.data = b"\xff\xd8" + b"x" * 200000
        result = self.browser.thumbnail("photo.jpg", '"abc"', len(self.reader.data))
        self.assertTrue(result["needsOriginal"])
        self.assertIsNone(result["thumbnailPath"])
        self.assertEqual(len(self.reader.gets), 1)
        self.browser.thumbnail("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual(len(self.reader.gets), 1, "Negative cache prevents repeating header probes")

    def test_cache_only_never_reads_remote(self):
        result = self.browser.thumbnail("photo.jpg", '"abc"', 10000, cache_only=True)
        self.assertTrue(result["needsOriginal"])
        self.assertEqual(self.reader.gets, [])

    def test_complete_clean_matching_original_cache_is_preferred(self):
        source = self.cache(b"original-photo")
        result = self.browser.thumbnail("Portfolio/photo.jpg", '"abc"', len(b"original-photo"))
        self.assertEqual(result["source"], "original-cache")
        self.assertEqual(self.reader.gets, [])
        self.assertEqual(self.renderer.call_args.args[0], source)

    def test_partial_dirty_or_wrong_version_cache_is_not_reused(self):
        for blocks, dirty, etag in (([{"Pos": 0, "Size": 2}], False, '"abc"'),
                                   (None, True, '"abc"'), (None, False, '"old"')):
            self.cache(b"abcdef", blocks=blocks, dirty=dirty, etag=etag)
            self.assertIsNone(self.browser.cached_original("Portfolio/photo.jpg", '"abc"', 6))

    def test_oversized_cache_metadata_is_skipped(self):
        self.cache(b"abcdef")
        path = self.paths.cache / self.connection["id"] / "vfsMeta/volume/Portfolio/photo.jpg"
        path.write_bytes(b" " * (1024 * 1024 + 1))
        self.assertIsNone(self.browser.cached_original("Portfolio/photo.jpg", '"abc"', 6))
        marker = self.browser.artifacts / (self.browser.identity("other.jpg", '"abc"', 6) + ".original")
        marker.write_bytes(b" " * (64 * 1024 + 1))
        self.assertIsNone(self.browser.cached_original("other.jpg", '"abc"', 6))

    def test_remote_path_traversal_never_maps_to_local_source(self):
        for key in ("../../outside.jpg", "/absolute.jpg", "Portfolio/../outside.jpg", "a\\b.jpg"):
            self.assertIsNone(self.browser.cached_original(key, '"abc"', 10))

    def test_full_thumbnail_fetch_is_opt_in_and_size_bounded(self):
        self.extractor.return_value = "2026-09-11T13:34:02"
        result = self.browser.thumbnail("photo.png", '"abc"', len(self.reader.data), allow_original=True)
        self.assertEqual(result["source"], "downloaded-original")
        self.assertEqual(result["dateTaken"], "2026-09-11T13:34:02")
        self.assertEqual(self.reader.gets, [("photo.png", '"abc"', None)])
        self.reader.gets.clear()
        result = self.browser.thumbnail("large.png", '"abc"', photo.MAX_THUMB_ORIGINAL + 1, allow_original=True)
        self.assertTrue(result["needsOriginal"])
        self.assertEqual(self.reader.gets, [])

    def test_open_original_fetches_complete_file_and_enables_later_thumbnail(self):
        result = self.browser.open_original("../../photo.png", '"abc"', len(self.reader.data))
        target = Path(result["originalPath"])
        self.assertTrue(target.is_relative_to(self.paths.home / "Downloads/Mountain Turtle/sample-bucket"))
        self.assertEqual(target.read_bytes(), self.reader.data)
        self.assertEqual(self.reader.gets, [("../../photo.png", '"abc"', None)])
        again = self.browser.open_original("../../photo.png", '"abc"', len(self.reader.data))
        self.assertEqual(again["source"], "download-cache")
        preview = self.browser.thumbnail("../../photo.png", '"abc"', len(self.reader.data))
        self.assertEqual(preview["source"], "original-cache")
        self.assertEqual(len(self.reader.gets), 1)

    def test_open_original_preserves_local_edits(self):
        first = Path(self.browser.open_original("photo.png", '"abc"', len(self.reader.data))["originalPath"])
        first.write_bytes(b"edited" + b"!" * len(self.reader.data))
        second = Path(self.browser.open_original("photo.png", '"abc"', len(self.reader.data))["originalPath"])
        self.assertNotEqual(first, second)
        self.assertTrue(first.read_bytes().startswith(b"edited"))
        self.assertEqual(second.read_bytes(), self.reader.data)

    def test_incomplete_original_is_not_published(self):
        with self.assertRaises(photo.BrowserError):
            self.browser.open_original("photo.jpg", '"abc"', len(self.reader.data) + 1)
        directory = self.paths.home / "Downloads/Mountain Turtle/sample-bucket"
        self.assertEqual(list(directory.iterdir()), [])

    def test_thumbnail_cache_is_bounded_and_oldest_is_evicted(self):
        for number in range(4):
            path = self.browser.artifacts / f"{number}.jpg"
            path.write_bytes(b"12345")
            os.utime(path, (number + 1, number + 1))
        with patch.object(photo, "CACHE_BYTES", 10), patch.object(photo, "CACHE_FILES", 2):
            self.browser.trim()
        self.assertEqual(sorted(p.name for p in self.browser.artifacts.iterdir()), ["2.jpg", "3.jpg"])

    def test_same_thumbnail_concurrent_requests_are_coalesced(self):
        self.reader.data = exif_image() + b"x" * 200000
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.browser.thumbnail("photo.jpg", '"abc"', len(self.reader.data)), range(4)))
        self.assertEqual(len(self.reader.gets), 1)
        self.assertEqual(len({item["thumbnailPath"] for item in results}), 1)

    def test_symlink_artifact_never_touches_external_file(self):
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("preserve")
        digest = self.browser.identity("photo.jpg", '"abc"', 1000, 256)
        (self.browser.artifacts / (digest + ".jpg")).symlink_to(outside)
        before = outside.stat().st_mtime_ns
        with self.assertRaises(photo.BrowserError):
            self.browser.thumbnail("photo.jpg", '"abc"', 1000)
        self.assertEqual(outside.read_text(), "preserve")
        self.assertEqual(outside.stat().st_mtime_ns, before)
        self.assertEqual(self.reader.gets, [])

    def test_symlink_download_directory_never_writes_external_folder(self):
        outside = Path(self.temp.name) / "elsewhere"
        outside.mkdir()
        downloads = self.paths.home / "Downloads"
        downloads.mkdir()
        (downloads / "Mountain Turtle").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(photo.BrowserError):
            self.browser.open_original("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.reader.gets, [])

    def test_symlink_cache_directory_is_rejected(self):
        other = self.paths.home / "other-cache"
        other.mkdir()
        self.browser.artifacts.rmdir()
        self.browser.artifacts.symlink_to(other, target_is_directory=True)
        with self.assertRaises(photo.BrowserError):
            photo.PhotoBrowser(self.connection, self.paths, self.reader, self.renderer)
        self.assertEqual(list(other.iterdir()), [])

    def test_cancelled_original_cleans_partial_download(self):
        def interrupt(key, etag, destination, byte_range=None):
            destination.write_bytes(b"partial")
            raise photo.Cancelled("cancelled")
        self.reader.get = interrupt
        with self.assertRaises(photo.Cancelled):
            self.browser.open_original("photo.jpg", '"abc"', 10000)
        self.assertEqual(list((self.paths.home / "Downloads/Mountain Turtle/sample-bucket").iterdir()), [])

    def test_unicode_download_name_preserves_extension_within_name_limit(self):
        result = self.browser.open_original("照" * 150 + ".jpg", '"abc"', len(self.reader.data))
        target = Path(result["originalPath"])
        self.assertEqual(target.suffix, ".jpg")
        self.assertLess(len(target.name.encode()), 255)

    def test_date_taken_reads_one_conditional_bounded_jpeg_header_and_caches_result(self):
        self.reader.data = b"\xff\xd8" + b"x" * 200000
        observed = []
        def extract(source):
            observed.append(source.read_bytes())
            return "2026-09-11T13:34:02"
        self.extractor.side_effect = extract
        first = self.browser.date_taken("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual(first, {"ok": True, "dateTaken": "2026-09-11T13:34:02", "downloadedBytes": photo.HEADER_BYTES})
        second = self.browser.date_taken("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual(second["downloadedBytes"], 0)
        self.assertEqual(second["dateTaken"], first["dateTaken"])
        self.assertEqual(observed, [self.reader.data[:photo.HEADER_BYTES]])
        self.assertEqual(self.reader.gets, [("photo.jpg", '"abc"', f"bytes=0-{photo.HEADER_BYTES - 1}")])
        self.renderer.assert_not_called()

    def test_date_taken_negative_cache_upgrades_from_new_complete_original(self):
        self.reader.data = b"\xff\xd8" + b"x" * 200000
        size = len(self.reader.data)
        self.extractor.side_effect = lambda source: "2026-09-11T13:34:02" if source.stat().st_size == size else None
        first = self.browser.date_taken("Portfolio/photo.jpg", '"abc"', size)
        self.assertIsNone(first["dateTaken"])
        self.assertEqual(self.browser.date_taken("Portfolio/photo.jpg", '"abc"', size)["downloadedBytes"], 0)
        self.assertEqual(len(self.reader.gets), 1)
        self.cache(self.reader.data)
        upgraded = self.browser.date_taken("Portfolio/photo.jpg", '"abc"', size, cache_only=True)
        self.assertEqual(upgraded, {"ok": True, "dateTaken": "2026-09-11T13:34:02", "downloadedBytes": 0})
        self.assertEqual(self.extractor.call_count, 2)
        self.assertEqual(len(self.reader.gets), 1)

    def test_date_taken_non_jpeg_is_local_only_including_raw_formats(self):
        for extension in ("png", "heic", "nef"):
            key = "Portfolio/photo." + extension
            with self.subTest(extension=extension):
                result = self.browser.date_taken(key, '"abc"', len(self.reader.data))
                self.assertEqual(result, {"ok": True, "dateTaken": None, "downloadedBytes": 0})
                self.cache(self.reader.data, key=key)
                self.extractor.return_value = "2026-09-11T13:34:02"
                result = self.browser.date_taken(key, '"abc"', len(self.reader.data))
                self.assertEqual(result["dateTaken"], "2026-09-11T13:34:02")
        self.assertEqual(self.reader.gets, [])
        self.assertEqual(self.extractor.call_count, 3)

    def test_date_cache_only_does_not_poison_later_header_request(self):
        self.reader.data = b"\xff\xd8small-jpeg\xff\xd9"
        first = self.browser.date_taken("photo.jpg", '"abc"', len(self.reader.data), cache_only=True)
        self.assertEqual(first, {"ok": True, "dateTaken": None, "downloadedBytes": 0})
        self.assertEqual(self.reader.gets, [])
        self.extractor.assert_not_called()
        second = self.browser.date_taken("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual(second["downloadedBytes"], len(self.reader.data))
        self.assertEqual(self.reader.gets, [("photo.jpg", '"abc"', f"bytes=0-{len(self.reader.data) - 1}")])

    def test_date_identity_includes_every_connection_and_version_field(self):
        self.reader.data = b"x" * 200000
        self.extractor.return_value = "2026-09-11T13:34:02"
        size = len(self.reader.data)
        self.browser.date_taken("photo.jpg", '"abc"', size)
        self.browser.date_taken("other.jpg", '"abc"', size)
        self.browser.date_taken("photo.jpg", '"new"', size)
        self.browser.date_taken("photo.jpg", '"abc"', size - 1)
        for field in ("id", "bucket", "profile", "region"):
            connection = dict(self.connection, **{field: "changed"})
            browser = photo.PhotoBrowser(connection, self.paths, self.reader, self.renderer, self.extractor)
            browser.date_taken("photo.jpg", '"abc"', size)
        self.assertEqual(len(self.reader.gets), 8)
        self.assertEqual(self.extractor.call_count, 8)

    def test_thumbnail_header_provides_date_without_extra_reads_or_per_size_extraction(self):
        self.reader.data = exif_image() + b"x" * 200000
        self.extractor.return_value = "2026-09-11T13:34:02"
        size = len(self.reader.data)
        first = self.browser.thumbnail("photo.jpg", '"abc"', size)
        self.assertEqual(first["dateTaken"], "2026-09-11T13:34:02")
        self.assertEqual(len(self.reader.gets), 1)
        self.assertEqual(self.browser.date_taken("photo.jpg", '"abc"', size)["dateTaken"], first["dateTaken"])
        self.assertEqual(len(self.reader.gets), 1)
        larger = self.browser.thumbnail("photo.jpg", '"abc"', size, pixels=512)
        self.assertEqual(larger["dateTaken"], first["dateTaken"])
        self.assertEqual(len(self.reader.gets), 2, "Only the additional thumbnail requires another header")
        self.extractor.assert_called_once()

    def test_unavailable_thumbnail_still_returns_header_camera_date(self):
        self.reader.data = b"\xff\xd8" + b"x" * 200000
        self.extractor.return_value = "2026-09-11T13:34:02"
        result = self.browser.thumbnail("photo.jpg", '"abc"', len(self.reader.data))
        self.assertTrue(result["needsOriginal"])
        self.assertEqual(result["dateTaken"], "2026-09-11T13:34:02")
        self.assertEqual(len(self.reader.gets), 1)

    def test_old_thumbnail_only_refreshes_date_from_verified_local_original(self):
        key, etag, size = "Portfolio/photo.jpg", '"abc"', len(self.reader.data)
        target = self.browser.artifacts / (self.browser.identity(key, etag, size, 256) + ".jpg")
        target.write_bytes(b"old-thumbnail")
        first = self.browser.thumbnail(key, etag, size)
        self.assertIsNone(first["dateTaken"])
        self.assertEqual(self.reader.gets, [])
        self.extractor.assert_not_called()
        self.cache(self.reader.data, blocks=[{"Pos": 0, "Size": 2}])
        self.browser.thumbnail(key, etag, size)
        self.extractor.assert_not_called()
        self.cache(self.reader.data)
        self.extractor.return_value = "2026-09-11T13:34:02"
        second = self.browser.thumbnail(key, etag, size)
        self.assertEqual(second["dateTaken"], "2026-09-11T13:34:02")
        self.assertEqual(self.reader.gets, [])
        self.assertEqual(second["thumbnailPath"], str(target))
        self.renderer.assert_not_called()

    def test_date_upgrade_preserves_returned_thumbnail_when_cache_needs_eviction(self):
        key, etag, size = "Portfolio/photo.jpg", '"abc"', len(self.reader.data)
        target = self.browser.artifacts / (self.browser.identity(key, etag, size, 256) + ".jpg")
        target.write_bytes(b"old-thumbnail")
        os.utime(target, (1, 1))
        other = self.browser.artifacts / "other-thumbnail.jpg"
        other.write_bytes(b"other-thumbnail")
        os.utime(other, (2, 2))
        self.cache(self.reader.data)
        self.extractor.return_value = "2026-09-11T13:34:02"
        with patch.object(photo, "CACHE_FILES", 2):
            result = self.browser.thumbnail(key, etag, size)
        self.assertEqual(result["source"], "thumbnail-cache")
        self.assertEqual(result["dateTaken"], "2026-09-11T13:34:02")
        self.assertEqual(Path(result["thumbnailPath"]).read_bytes(), b"old-thumbnail")
        self.assertFalse(other.exists())
        self.assertEqual(len(list(self.browser.artifacts.iterdir())), 2)
        self.assertEqual(self.reader.gets, [])
        self.renderer.assert_not_called()

    def test_open_original_updates_negative_date_cache_and_returns_date(self):
        self.reader.data = b"\xff\xd8" + b"x" * 200000
        size = len(self.reader.data)
        self.extractor.side_effect = lambda source: "2026-09-11T13:34:02" if source.stat().st_size == size else None
        self.assertIsNone(self.browser.date_taken("photo.jpg", '"abc"', size)["dateTaken"])
        opened = self.browser.open_original("photo.jpg", '"abc"', size)
        self.assertEqual(opened["dateTaken"], "2026-09-11T13:34:02")
        self.assertEqual(self.browser.open_original("photo.jpg", '"abc"', size)["dateTaken"], opened["dateTaken"])
        self.assertEqual(self.browser.date_taken("photo.jpg", '"abc"', size)["dateTaken"], opened["dateTaken"])
        self.assertEqual(self.extractor.call_count, 2)
        self.assertEqual(self.reader.gets, [("photo.jpg", '"abc"', f"bytes=0-{photo.HEADER_BYTES - 1}"),
                                            ("photo.jpg", '"abc"', None)])

    def test_concurrent_date_and_thumbnail_share_the_same_object_lock(self):
        self.reader.data = exif_image() + b"x" * 200000
        self.extractor.return_value = "2026-09-11T13:34:02"
        size = len(self.reader.data)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.browser.date_taken("photo.jpg", '"abc"', size), range(4)))
        self.assertEqual(len(self.reader.gets), 1)
        self.assertEqual(self.extractor.call_count, 1)
        self.assertTrue(all(item["dateTaken"] == "2026-09-11T13:34:02" for item in results))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pixels: self.browser.thumbnail("photo.jpg", '"abc"', size, pixels), (256, 512)))
        self.assertEqual(self.extractor.call_count, 1)
        self.assertTrue(all(item["dateTaken"] == "2026-09-11T13:34:02" for item in results))

    def test_invalid_header_length_is_rejected_without_extraction(self):
        for length in (photo.HEADER_BYTES - 1, photo.HEADER_BYTES + 1):
            with self.subTest(length=length):
                def get(key, etag, destination, byte_range=None):
                    destination.write_bytes(b"x" * length)
                self.reader.get = get
                with self.assertRaisesRegex(photo.BrowserError, "header download"):
                    self.browser.date_taken("photo.jpg", '"abc"', 200000)
                self.extractor.assert_not_called()

    def test_malformed_helper_metadata_is_unknown_and_never_exposed(self):
        helper = self.paths.home / "app/Contents/Helpers/Mountain Turtle Photo Dates"
        helper.parent.mkdir(parents=True)
        helper.write_bytes(b"test-helper")
        self.paths.resources = helper.parent.parent / "Resources"
        browser = photo.PhotoBrowser(self.connection, self.paths, self.reader, self.renderer)
        for number, response in enumerate((b"not-json-private-details", b"[]", b"{}", b'{"dateTaken":1}',
                                           b'{"dateTaken":"2026-02-30T12:30:00"}', b'{"dateTaken":"2026-09-11"}',
                                           b'{"dateTaken":"2026-09-11T13:34:02Z"}', b"x" * 4097)):
            with self.subTest(response=response[:50]), patch.object(photo, "run_process", return_value=response) as run:
                result = browser.date_taken(f"photo-{number}.jpg", '"abc"', len(self.reader.data))
                self.assertEqual(result["dateTaken"], None)
                self.assertNotIn("error", result)
                self.assertEqual(run.call_args.kwargs["timeout"], photo.DATE_HELPER_TIMEOUT)
                self.assertEqual(run.call_args.args[0][0], str(helper))

    def test_helper_errors_fail_safe_but_cancellation_propagates(self):
        for number, error in enumerate((photo.BrowserError("sensitive subprocess output"),
                                        subprocess.TimeoutExpired("helper", photo.DATE_HELPER_TIMEOUT))):
            self.extractor.side_effect = error
            key = f"photo-{number}.jpg"
            result = self.browser.date_taken(key, '"abc"', len(self.reader.data))
            self.assertIsNone(result["dateTaken"])
            self.assertNotIn("error", result)
            self.assertIsNone(self.browser.date_record(self.browser.identity(key, '"abc"', len(self.reader.data))))
        self.extractor.side_effect = None
        self.extractor.return_value = "2026-09-11T13:34:02"
        self.assertEqual(self.browser.date_taken("photo-0.jpg", '"abc"', len(self.reader.data))["dateTaken"], "2026-09-11T13:34:02")
        self.extractor.side_effect = photo.Cancelled("cancelled")
        with self.assertRaises(photo.Cancelled):
            self.browser.date_taken("cancelled.jpg", '"abc"', len(self.reader.data))
        self.assertIsNone(self.browser.date_record(self.browser.identity("cancelled.jpg", '"abc"', len(self.reader.data))))

    def test_missing_helper_does_not_persist_missing_date(self):
        browser = photo.PhotoBrowser(self.connection, self.paths, self.reader, self.renderer)
        with patch.object(photo, "run_process") as run:
            result = browser.date_taken("photo.jpg", '"abc"', len(self.reader.data))
        self.assertIsNone(result["dateTaken"])
        self.assertIsNone(browser.date_record(browser.identity("photo.jpg", '"abc"', len(self.reader.data))))
        run.assert_not_called()

    def test_completed_original_without_camera_date_is_negatively_cached(self):
        self.cache(self.reader.data)
        first = self.browser.date_taken("Portfolio/photo.jpg", '"abc"', len(self.reader.data))
        second = self.browser.date_taken("Portfolio/photo.jpg", '"abc"', len(self.reader.data))
        self.assertIsNone(first["dateTaken"])
        self.assertEqual(first, second)
        self.extractor.assert_called_once()
        self.assertEqual(self.reader.gets, [])

    def test_symlink_date_cache_is_rejected_without_touching_target(self):
        outside = self.paths.home / "private.json"
        outside.write_text('{"dateTaken":"2026-09-11T13:34:02","complete":true,"version":1}')
        digest = self.browser.identity("photo.jpg", '"abc"', len(self.reader.data))
        (self.browser.artifacts / (digest + ".date-taken")).symlink_to(outside)
        with self.assertRaises(photo.BrowserError):
            self.browser.date_taken("photo.jpg", '"abc"', len(self.reader.data))
        self.assertEqual(self.reader.gets, [])
        self.extractor.assert_not_called()

    def test_date_taken_command_accepts_cache_only_without_full_download_option(self):
        args = photo.parser().parse_args(["date-taken", "test-connection", "--key", "photo.jpg", "--etag", '"abc"', "--size", "123", "--cache-only"])
        self.assertEqual((args.command, args.id, args.key, args.etag, args.size, args.cache_only),
                         ("date-taken", "test-connection", "photo.jpg", '"abc"', 123, True))
        self.assertFalse(hasattr(args, "allow_original"))

    def test_timeout_reaps_local_child_process(self):
        processes = []
        original = subprocess.Popen
        def launch(*args, **kwargs):
            child = original(*args, **kwargs)
            processes.append(child)
            return child
        with patch.object(photo.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(subprocess.TimeoutExpired):
                photo.run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.02)
        self.assertIsNotNone(processes[0].poll())

    @unittest.skipUnless(Path("/usr/bin/sips").is_file(), "macOS renderer required")
    def test_native_renderer_creates_a_real_jpeg_locally(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0ioAAAAASUVORK5CYII=")
        source, output = Path(self.temp.name) / "input.png", Path(self.temp.name) / "preview.jpg"
        source.write_bytes(png)
        photo.render_thumbnail(source, output, 256)
        self.assertTrue(output.read_bytes().startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
