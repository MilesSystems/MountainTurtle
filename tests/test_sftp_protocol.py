import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "service"))
import drive_metrics
spec = importlib.util.spec_from_file_location("turtle_smoke", root / "service/turtle_service.py")
turtle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turtle)
class SFTPProtocolTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("rclone") and shutil.which("ssh-keygen"), "Requires rclone and ssh-keygen")
    def test_real_sftp_read_quoted_folder_host_key_rejection_and_private_rc(self):
        rclone = shutil.which("rclone")
        with tempfile.TemporaryDirectory(prefix="turtle-sftp-protocol-") as temporary:
            base = Path(temporary)
            for name in ("host", "client", "wrong"):
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(base / name)], check=True)
            served = base / "served"
            folder = served / 'Family "photos":ro'
            folder.mkdir(parents=True)
            (folder / "hello.txt").write_text("SFTP protocol fixture")
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            rc = turtle.remote_control_settings()
            env = turtle.environment(None, None)
            env.update(RCLONE_RC_USER=rc["rcUser"], RCLONE_RC_PASS=rc["rcPass"])
            command = [rclone, "serve", "sftp", str(served), "--addr", f"127.0.0.1:{port}",
                "--key", str(base / "host"), "--authorized-keys", str(base / "client.pub"),
                "--user", "tester", "--config", str(base / "server.conf"), "--cache-dir", str(base / "server-cache"),
                "--rc", "--rc-addr", f"127.0.0.1:{rc['rcPort']}"]
            with (base / "server.log").open("w+") as log:
                server = subprocess.Popen(command, env=env, stdout=log, stderr=log)
                try:
                    for _ in range(50):
                        if server.poll() is not None:
                            raise RuntimeError("Local SFTP fixture did not start")
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                                break
                        except OSError:
                            time.sleep(0.1)
                    host_public = (base / "host.pub").read_text().split()
                    hosts = base / "known_hosts"
                    hosts.write_text(f"[127.0.0.1]:{port} " + " ".join(host_public[:2]) + "\n")
                    paths = turtle.Paths(home=base / "home", resources=root / "Resources")
                    paths.prepare()
                    connection = dict(id="fixture", name="Test", backend="sftp", host="127.0.0.1", user="tester", port=port,
                        authMode="keyFile", keyFile=str(base / "client"), knownHostsFile=str(hosts), remotePath='/Family "photos":ro',
                        readOnly=True)
                    config, remote = turtle.connection_config(connection, paths)
                    config_path = base / "client.conf"
                    config_path.write_text(config)
                    client_env = turtle.mount_environment(connection, paths, rclone)
                    common = ["--config", str(config_path), "--retries", "1", "--low-level-retries", "1", "--contimeout", "2s"]
                    result = subprocess.run([rclone, "lsf", remote, *common], env=client_env, text=True, capture_output=True, timeout=10)
                    assert result.returncode == 0, result.stderr
                    assert "hello.txt" in result.stdout, result.stdout
                    result = subprocess.run([rclone, "cat", remote + "hello.txt", *common], env=client_env, text=True, capture_output=True, timeout=10)
                    assert result.returncode == 0 and result.stdout == "SFTP protocol fixture", result.stderr
                    capacity = drive_metrics.storage(connection, paths)
                    assert capacity["status"] == "available", capacity
                    assert capacity["scope"] == "remoteFilesystem"
                    assert capacity["totalBytes"] > 0 and capacity["usedBytes"] >= 0 and capacity["freeBytes"] >= 0, capacity
                    assert capacity["estimate"] is None
                    assert capacity["source"] == "SFTP server filesystem statistics"
                    wrong_public = (base / "wrong.pub").read_text().split()
                    hosts.write_text(f"[127.0.0.1]:{port} " + " ".join(wrong_public[:2]) + "\n")
                    result = subprocess.run([rclone, "lsf", remote, *common], env=client_env, text=True, capture_output=True, timeout=10)
                    assert result.returncode != 0 and "key mismatch" in result.stderr, result.stderr
                    url = f"http://127.0.0.1:{rc['rcPort']}/core/stats"
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
                    try:
                        opener.open(request, timeout=2)
                        raise AssertionError("Unauthenticated RC request unexpectedly succeeded")
                    except urllib.error.HTTPError as error:
                        assert error.code == 401, error.code
                    token = base64.b64encode((rc["rcUser"] + ":" + rc["rcPass"]).encode()).decode()
                    request.add_header("Authorization", "Basic " + token)
                    with opener.open(request, timeout=2) as response:
                        stats = json.load(response)
                    assert "bytes" in stats and "transfers" in stats
                finally:
                    server.terminate()
                    server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
