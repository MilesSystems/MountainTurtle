"""Native rename regression on an isolated, case-sensitive SFTP/NFS mount.

No saved connections, user files, credentials, or installed app are used.
The APFS image models a case-sensitive server even on a default macOS disk.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
import turtle_service as turtle


@unittest.skipUnless(sys.platform == "darwin" and all(shutil.which(tool) for tool in
                     ("rclone", "ssh-keygen", "xcrun", "hdiutil")),
                     "requires macOS, rclone, SSH tools, and Command Line Tools")
class FinderRenameTests(unittest.TestCase):
    def run_command(self, args, **kwargs):
        return subprocess.run(list(map(str, args)), check=True, capture_output=True,
                              text=True, timeout=60, **kwargs)

    def stop_process(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="turtle-finder-rename-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.helper = self.base / "rename-probe"
        self.run_command(["xcrun", "clang", "-Wno-deprecated-declarations", "-framework", "Foundation",
                          "-framework", "CoreServices", ROOT / "tests/FinderRenameProbe.m", "-o", self.helper])
        image = self.base / "server.sparseimage"
        self.served = self.base / "server"
        self.served.mkdir()
        self.run_command(["hdiutil", "create", "-size", "64m", "-type", "SPARSE", "-fs",
                          "Case-sensitive APFS", "-volname", "TurtleRenameTest", image])
        self.run_command(["hdiutil", "attach", image, "-mountpoint", self.served, "-nobrowse"])
        self.addCleanup(self.run_command, ["hdiutil", "detach", self.served])
        folder = self.served / "Highschool"
        folder.mkdir()
        (folder / "keep.txt").write_text("preserve these bytes")
        for name in ("host", "client"):
            self.run_command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.base / name])
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        self.rclone = shutil.which("rclone")
        # The fixture server must not inherit macOS's VFS case-folding either.
        server = subprocess.Popen([self.rclone, "serve", "sftp", str(self.served),
            "--vfs-case-insensitive=false", "--addr", f"127.0.0.1:{port}",
            "--key", str(self.base / "host"), "--authorized-keys", str(self.base / "client.pub"),
            "--user", "tester", "--config", str(self.base / "server.conf")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, server)
        for _ in range(100):
            self.assertIsNone(server.poll(), "SFTP fixture exited before listening")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("SFTP fixture did not start")
        host_key = (self.base / "host.pub").read_text().split()
        hosts = self.base / "known_hosts"
        hosts.write_text(f"[127.0.0.1]:{port} " + " ".join(host_key[:2]) + "\n")
        self.paths = turtle.Paths(home=self.base / "home", resources=ROOT / "Resources")
        self.paths.prepare()
        connection = dict(id="fixture", name="Rename test", backend="sftp", readOnly=False,
            host="127.0.0.1", user="tester", port=port, authMode="keyFile",
            keyFile=str(self.base / "client"), knownHostsFile=str(hosts), remotePath="/")
        config, remote = turtle.connection_config(connection, self.paths)
        (self.paths.remotes / "fixture.conf").write_text(config)
        self.mount = self.paths.mounts / connection["name"]
        self.mount.mkdir(parents=True)
        process = subprocess.Popen(turtle.mount_command(connection, self.paths, self.rclone, remote),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, process)
        for _ in range(100):
            self.assertIsNone(process.poll(), "NFS fixture exited before mounting")
            if os.path.ismount(self.mount):
                break
            time.sleep(0.1)
        else:
            self.fail("NFS fixture did not mount")
        self.addCleanup(self.run_command, ["/sbin/umount", self.mount])

    def rename(self, old, new, succeeds=True):
        result = subprocess.run([str(self.helper), str(self.mount / old), new],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)

    def test_native_case_and_underscore_renames_preserve_contents_and_metadata(self):
        self.assertEqual((self.mount / "Highschool/keep.txt").read_text(), "preserve these bytes")
        # FinderInfo creates an AppleDouble sidecar on this NFS mount.
        self.run_command(["xattr", "-wx", "com.apple.FinderInfo", "00" * 32, self.mount / "Highschool"])
        for old, new in (("Highschool", "highschool"), ("highschool", "_highschool"),
                         ("_highschool", "highschool")):
            with self.subTest(old=old, new=new):
                self.rename(old, new)
                self.assertFalse((self.served / old).exists())
                self.assertEqual((self.served / new / "keep.txt").read_text(), "preserve these bytes")
                self.assertTrue((self.mount / ("._" + new)).is_file())
                self.assertFalse((self.mount / ("._" + old)).exists())
        # A real destination must still be protected by the native API.
        self.rename("highschool", "highschool")
        self.run_command(["mkdir", self.mount / "occupied"])
        self.rename("highschool", "occupied", succeeds=False)
        self.assertEqual((self.served / "highschool/keep.txt").read_text(), "preserve these bytes")


if __name__ == "__main__":
    unittest.main()
