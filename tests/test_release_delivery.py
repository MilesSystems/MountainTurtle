import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_delivery", ROOT / "scripts/verify-published-release.py")
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


class ReleaseDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.assets = {
            "release.json": json.dumps({"version": "0.8.0", "tag": "v0.8.0",
                                         "repository": "MilesSystems/MountainTurtle"}).encode(),
            "appcast.xml": b"signed feed fixture",
            "MountainTurtle-0.8.0.zip": b"signed archive fixture",
            "MountainTurtle-0.8.0.md": b"notes fixture",
        }
        for name, data in self.assets.items():
            (self.directory / name).write_bytes(data)
        self.write_checksums()
        self.requests = []

    def write_checksums(self):
        (self.directory / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in self.assets.items()))

    def fetch(self, url, size):
        self.requests.append(url)
        name = url.rsplit("/", 1)[1]
        self.assertEqual(size, len(self.assets[name]))
        return hashlib.sha256(self.assets[name]).hexdigest()

    def test_checks_installed_feed_and_every_public_asset(self):
        self.assertEqual(delivery.verify(self.directory, self.fetch), delivery.RELEASES + "/tag/v0.8.0")
        self.assertEqual(self.requests[0], delivery.RELEASES + "/latest/download/appcast.xml")
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(all("/download/v0.8.0/" in url for url in self.requests[1:]))

    def test_latest_feed_mismatch_fails_even_when_tag_assets_match(self):
        def fetch(url, size):
            return "0" * 64 if "/latest/" in url else self.fetch(url, size)
        with self.assertRaisesRegex(RuntimeError, "public update feed"):
            delivery.verify(self.directory, fetch)

    def test_archive_mismatch_fails(self):
        def fetch(url, size):
            return "0" * 64 if url.endswith(".zip") else self.fetch(url, size)
        with self.assertRaisesRegex(RuntimeError, "asset differs"):
            delivery.verify(self.directory, fetch)

    def test_changed_local_asset_fails_before_network(self):
        (self.directory / "appcast.xml").write_bytes(b"changed feed")
        with self.assertRaisesRegex(ValueError, "Prepared asset changed"):
            delivery.verify(self.directory, self.fetch)
        self.assertEqual(self.requests, [])

    def test_missing_checksum_fails_before_network(self):
        self.assets.pop("MountainTurtle-0.8.0.zip")
        self.write_checksums()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            delivery.verify(self.directory, self.fetch)
        self.assertEqual(self.requests, [])

    def test_symlinked_local_asset_is_rejected(self):
        path = self.directory / "appcast.xml"
        path.rename(self.directory / "other.xml")
        path.symlink_to("other.xml")
        with self.assertRaisesRegex(ValueError, "Prepared asset changed"):
            delivery.verify(self.directory, self.fetch)


if __name__ == "__main__":
    unittest.main()
