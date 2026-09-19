"""Exercise connection-file handling from the actual app sources without launching it."""

from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "requires macOS AppKit")
class ConnectionTransferNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="mountainturtle-transfer-tests-")
        cls.addClassCleanup(cls.directory.cleanup)
        temporary = Path(cls.directory.name)
        cls.executable = temporary / "connection-transfer-tests"
        root = Path(__file__).resolve().parents[1]
        sparkle = subprocess.run([str(root / "scripts/fetch-sparkle.sh")],
                                 capture_output=True, text=True, check=True, timeout=180).stdout.strip()
        sources = []
        for source in sorted((root / "Sources").glob("*.swift")):
            if source.name == "MountainTurtle.swift":
                # Keep the real app implementations but replace the application
                # entry point with a test runner. No AppModel or app is started.
                content, count = re.subn(r"@main(?=\s+struct MountainTurtleApp\b)", "", source.read_text())
                if count != 1:
                    raise AssertionError("Could not replace the Mountain Turtle app entry point")
                source = temporary / source.name
                source.write_text(content)
            sources.append(str(source))
        result = subprocess.run([
            "xcrun", "swiftc", "-Onone", "-swift-version", "5", "-parse-as-library",
            "-target", platform.machine() + "-apple-macosx14.0",
            "-framework", "AppKit", "-framework", "SwiftUI", "-framework", "FinderSync",
            "-F", sparkle, "-framework", "Sparkle", "-Xlinker", "-rpath", "-Xlinker", sparkle,
            *sources, str(root / "tests/ConnectionTransferTests.swift"), "-o", str(cls.executable),
        ], capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise AssertionError("Native transfer test compilation failed:\n" + result.stdout + result.stderr)

    def run_case(self, name):
        result = subprocess.run([str(self.executable), name], cwd=self.directory.name,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS " + name)

    def test_inspection_rejects_empty_and_oversized_before_subprocess(self):
        self.run_case("inspection_limits")

    def test_inspection_size_boundary_reaches_service(self):
        self.run_case("inspection_boundary")

    def test_file_read_rejects_nonlocal_wrong_extension_and_directory(self):
        self.run_case("file_types")

    def test_file_read_checks_empty_oversized_and_exact_limit(self):
        self.run_case("file_limits")

    def test_available_name_avoids_case_insensitive_duplicates(self):
        self.run_case("duplicate_names")

    def test_available_name_preserves_unicode_within_utf8_limit(self):
        self.run_case("unicode_names")


if __name__ == "__main__":
    unittest.main()
