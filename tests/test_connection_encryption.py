"""Exercise the actual native portable-connection encryption on macOS."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "requires macOS CryptoKit")
class ConnectionEncryptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.executable = Path(cls.directory.name) / "connection-encryption-tests"
        root = Path(__file__).resolve().parents[1]
        subprocess.run([
            "xcrun", "swiftc", "-O", "-swift-version", "5",
            str(root / "Sources/ConnectionEncryption.swift"),
            str(root / "tests/ConnectionEncryptionTests.swift"),
            "-o", str(cls.executable),
        ], check=True, capture_output=True, text=True, timeout=120)

    def run_case(self, name):
        result = subprocess.run([str(self.executable), name], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_round_trip_and_randomness(self):
        self.run_case("roundtrip")

    def test_optional_saved_password_and_unicode(self):
        self.run_case("unicode")

    def test_wrong_password_and_tampering_share_error(self):
        self.run_case("tampering")

    def test_metadata_is_validated_before_password_work(self):
        self.run_case("metadata")

    def test_malformed_boolean_version_and_kdf_work_factor(self):
        self.run_case("types")

    def test_export_and_import_limits(self):
        self.run_case("limits")

    def test_saved_password_limit_and_invalid_characters(self):
        self.run_case("saved_password")

    def test_independent_pbkdf2_reference(self):
        self.run_case("reference")


if __name__ == "__main__":
    unittest.main()
