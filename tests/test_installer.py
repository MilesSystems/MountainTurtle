import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
INSTALL_CODE = (ROOT / "scripts/install.sh").read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.source = self.home / "build/Mountain Turtle.app"
        self.destination = self.home / "Applications/Mountain Turtle.app"
        for app, value in [(self.source, b"new app"), (self.destination, b"previous app")]:
            (app / "Contents").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(value)
        self.pgrep = [1, 1]
        self.corrupt_archive = False
        self.registration_error = None
        self.registrations = []

    def run_command(self, args, **kwargs):
        if args[0].endswith("/lsregister"):
            self.registrations.append(args)
            self.assertEqual((self.destination / "Contents/Info.plist").read_bytes(), b"new app")
            self.assertFalse(list(self.destination.parent.glob(".mountainturtle-install-*")))
            if self.registration_error:
                raise self.registration_error
        if args[0] == "/usr/bin/pgrep":
            return subprocess.CompletedProcess(args, self.pgrep.pop(0))
        if args[0] == "/usr/bin/ditto":
            source, archive = map(Path, args[-2:])
            if self.corrupt_archive:
                archive.write_bytes(b"invalid archive")
            else:
                with zipfile.ZipFile(archive, "w") as output:
                    for item in source.rglob("*"):
                        if item.is_file():
                            output.write(item, str(item.relative_to(source.parent)))
        return subprocess.CompletedProcess(args, 0)

    def install(self):
        with patch("sys.argv", ["install", str(self.source), str(self.destination)]), \
             patch.object(Path, "home", return_value=self.home), \
             patch("subprocess.run", side_effect=self.run_command), \
             contextlib.redirect_stdout(io.StringIO()):
            exec(compile(INSTALL_CODE, "scripts/install.sh", "exec"), {})

    def archives(self):
        return list((self.home / "Library/Application Support/Mountain Turtle/Backups").glob("*.zip"))

    def assert_original_preserved(self):
        self.assertEqual((self.destination / "Contents/Info.plist").read_bytes(), b"previous app")

    def test_update_keeps_verified_private_archive_outside_applications(self):
        self.install()
        self.assertEqual((self.destination / "Contents/Info.plist").read_bytes(), b"new app")
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])
        self.assertEqual([args[1:] for args in self.registrations], [["-f", str(self.destination)]])
        archive, = self.archives()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with zipfile.ZipFile(archive) as saved:
            self.assertIsNone(saved.testzip())
            self.assertEqual(saved.read("Mountain Turtle.app/Contents/Info.plist"), b"previous app")

    def test_corrupt_archive_aborts_before_replacing_original(self):
        self.corrupt_archive = True
        with self.assertRaises(zipfile.BadZipFile):
            self.install()
        self.assert_original_preserved()
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.registrations, [])

    def test_failed_final_rename_restores_original_and_retains_archive(self):
        real_rename = Path.rename

        def fail_new_app(path, target):
            if path.name == "Mountain Turtle.app" and path.parent.name.startswith(".mountainturtle-install-"):
                raise OSError("simulated install failure")
            return real_rename(path, target)

        with patch.object(Path, "rename", fail_new_app), self.assertRaises(OSError):
            self.install()
        self.assert_original_preserved()
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(self.registrations, [])

    def test_app_opened_during_backup_aborts_before_replacing_original(self):
        self.pgrep = [1, 0]
        with self.assertRaisesRegex(RuntimeError, "started during backup"):
            self.install()
        self.assert_original_preserved()
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(self.registrations, [])

    def test_registration_failure_warns_without_rolling_back_successful_install(self):
        for error in [OSError("missing utility"), subprocess.CalledProcessError(1, "lsregister"),
                      subprocess.TimeoutExpired("lsregister", 15)]:
            with self.subTest(error=type(error).__name__):
                self.registration_error = error
                self.pgrep = [1, 1]
                warnings = io.StringIO()
                with contextlib.redirect_stderr(warnings):
                    self.install()
                self.assertIn("app was installed", warnings.getvalue())
                self.assertEqual((self.destination / "Contents/Info.plist").read_bytes(), b"new app")


if __name__ == "__main__":
    unittest.main()
