import http.client
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "service/finder_badges.py"
spec = importlib.util.spec_from_file_location("finder_badges", SOURCE)
badges = importlib.util.module_from_spec(spec)
spec.loader.exec_module(badges)


class BadgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        home = Path(self.temporary.name)
        self.paths = SimpleNamespace(base=home / "Support", cache=home / "Cache", resources=home / "Resources")
        self.root = str(home / "Mounts/Photos")
        self.connection = {"id": "connection-id", "name": "Photos", "mountPath": self.root,
                           "bucket": "private-bucket", "profile": "private-profile",
                           "state": "connected", "mounted": True}
        overlay = self.paths.base / "icon-overlay"
        overlay.mkdir(parents=True)
        for name in (".VolumeIcon.icns", "._.", "._.VolumeIcon.icns"):
            (overlay / name).touch()

    def cache(self, metadata, size=10, name="photo.jpg", namespace="volume"):
        cache = self.paths.cache / self.connection["id"]
        meta, data = (cache / "vfsMeta" / namespace / name, cache / "vfs" / namespace / name)
        meta.parent.mkdir(parents=True, exist_ok=True)
        data.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps(metadata))
        with data.open("wb") as handle:
            handle.truncate(size)
        return meta, data

    def state(self, name="photo.jpg"):
        return badges.badge_for_path(self.paths, [self.connection], self.root + "/" + name)

    def remote_config(self, contents):
        path = self.paths.base / "remotes" / (self.connection["id"] + ".conf")
        path.parent.mkdir(exist_ok=True)
        path.write_text(contents)
        return path

    def complete(self, **extra):
        return dict({"Size": 10, "Dirty": False, "Rs": [{"Pos": 0, "Size": 10}]}, **extra)

    def test_uncached_path_never_touches_mount_or_scans(self):
        real_open = os.open
        opened = []

        def limited_open(path, *args, **kwargs):
            opened.append(str(path))
            self.assertFalse(str(path).startswith(self.root))
            return real_open(path, *args, **kwargs)

        with patch.object(badges.os, "open", side_effect=limited_open), \
                patch.object(os, "walk", side_effect=AssertionError("Must not scan")):
            self.assertEqual(self.state(), "online")
        self.assertTrue(opened)

    def test_complete_cached_file_and_zero_length_file(self):
        self.cache(self.complete())
        self.assertEqual(self.state(), "cached")
        self.cache({"Size": 0, "Dirty": False, "Rs": None}, size=0, name="empty")
        self.assertEqual(self.state("empty"), "cached")

    def test_sparse_ranges_are_partial_not_a_completed_size(self):
        self.cache(self.complete(Rs=[{"Pos": 0, "Size": 3}, {"Pos": 7, "Size": 3}]))
        self.assertEqual(self.state(), "partial")
        self.cache(self.complete(Rs=[]))
        self.assertEqual(self.state(), "online")

    def test_contiguous_and_overlapping_coverage_is_complete(self):
        for ranges in ([{"Pos": 5, "Size": 5}, {"Pos": 0, "Size": 5}],
                       [{"Pos": 0, "Size": 7}, {"Pos": 5, "Size": 5}]):
            self.cache(self.complete(Rs=ranges))
            self.assertEqual(self.state(), "cached")

    def test_invalid_range_schema_never_claims_cached(self):
        for ranges in ([{"Pos": -1, "Size": 10}], [{"Pos": 0, "Size": 11}],
                       [{"Pos": True, "Size": 10}], [{"Pos": 0}], "complete", [None]):
            with self.subTest(ranges=ranges):
                self.cache(self.complete(Rs=ranges))
                self.assertEqual(self.state(), "unknown")
        for size in (-1, True, "10", None):
            self.cache(self.complete(Size=size))
            self.assertEqual(self.state(), "unknown")

    def test_dirty_cache_is_pending_even_with_full_coverage(self):
        self.cache(self.complete(Dirty=True))
        self.assertEqual(self.state(), "pending")

    def test_missing_backing_file_or_size_disagreement_is_unknown(self):
        _, data = self.cache(self.complete(), size=9)
        self.assertEqual(self.state(), "unknown")
        data.unlink()
        self.assertEqual(self.state(), "unknown")

    def test_corrupt_or_incomplete_metadata_is_unknown(self):
        meta, _ = self.cache(self.complete())
        for raw in ('{"Dirty":', '[]', '{"Size":10,"Rs":[]}', '{"Dirty": "false"}'):
            meta.write_text(raw)
            self.assertEqual(self.state(), "unknown")
        meta.write_text("x" * (badges.MAX_METADATA + 1))
        self.assertEqual(self.state(), "unknown")

    def test_directories_and_disconnected_roots_do_not_claim_subtree_cached(self):
        self.cache(self.complete(), name="nested/photo.jpg")
        self.assertEqual(self.state("nested"), "unknown")
        self.assertEqual(badges.badge_for_path(self.paths, [self.connection], self.root), "unknown")
        self.connection["mounted"] = False
        self.assertEqual(self.state("nested/photo.jpg"), "unknown")

    def test_local_helper_files_do_not_get_cloud_status(self):
        for name in (".VolumeIcon.icns", "._.", "._.VolumeIcon.icns", ".DS_Store", "nested/._photo.jpg"):
            self.assertEqual(self.state(name), "unknown")

    def test_traversal_other_roots_and_prefix_collisions_are_rejected(self):
        for path in (self.root + "/../secret", self.root + "/./photo.jpg", self.root + "2/photo.jpg",
                     self.root + "//photo.jpg", "/etc/passwd", "relative", self.root + "/nul\0"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                badges.badge_for_path(self.paths, [self.connection], path)

    def test_symlink_metadata_backing_file_and_parent_are_not_followed(self):
        meta, data = self.cache(self.complete())
        outside = self.paths.base.parent / "outside.json"
        outside.write_text(json.dumps(self.complete()))
        meta.unlink()
        meta.symlink_to(outside)
        self.assertEqual(self.state(), "unknown")
        meta.unlink()
        meta.write_text(json.dumps(self.complete()))
        data.unlink()
        data.symlink_to(outside)
        self.assertEqual(self.state(), "unknown")
        # A symlink anywhere below the cache root is refused, even if it points
        # back to another otherwise valid cache directory.
        (meta.parent / "linked").symlink_to(meta.parent, target_is_directory=True)
        self.assertEqual(self.state("linked/photo.jpg"), "unknown")

    def test_fifo_metadata_is_nonblocking_and_unknown(self):
        meta, _ = self.cache(self.complete())
        meta.unlink()
        os.mkfifo(meta)
        self.assertEqual(self.state(), "unknown")

    def test_s3_namespace_without_overlay_does_not_use_old_volume_cache(self):
        self.cache(self.complete())
        (self.paths.base / "icon-overlay/.VolumeIcon.icns").unlink()
        self.assertEqual(self.state(), "online")
        self.cache(self.complete(), namespace="s3/" + self.connection["bucket"])
        self.assertEqual(self.state(), "cached")

    def bridge(self):
        bridge = badges.BadgeBridge(self.paths, lambda: [dict(self.connection)]).start()
        self.addCleanup(bridge.stop)
        return bridge

    def test_sftp_badges_use_overlay_without_an_s3_bucket(self):
        self.connection.pop("bucket")
        self.connection.update(backend="sftp", remotePath="/test files")
        overlay = self.paths.base / "icon-overlay-sftp"
        overlay.mkdir()
        for name in (".VolumeIcon.icns", "._.", "._.VolumeIcon.icns"):
            (overlay / name).touch()
        self.cache(self.complete())
        self.assertEqual(self.state(), "cached")

    def test_sftp_direct_cache_namespace_and_pending_writes(self):
        self.connection.update(backend="sftp", bucket="", remotePath="/test files")
        # An unrelated S3 overlay and stale union cache must not hide direct SFTP writes.
        self.cache(self.complete())
        self.cache(self.complete(Dirty=True), namespace="sftp/test files")
        self.assertEqual(self.state(), "pending")
        self.connection["remotePath"] = "../other"
        self.assertEqual(self.state(), "unknown")

    def test_incomplete_sftp_overlay_does_not_reuse_old_union_cache(self):
        self.connection.update(backend="sftp", bucket="", remotePath="/test files")
        overlay = self.paths.base / "icon-overlay-sftp"
        overlay.mkdir()
        for name in ("._.", "._.VolumeIcon.icns"):
            (overlay / name).touch()
        self.cache(self.complete())
        self.assertEqual(self.state(), "online")
        self.cache(self.complete(), namespace="sftp/test files")
        self.assertEqual(self.state(), "cached")

    def test_recovered_sftp_mount_uses_legacy_union_config_for_cached_and_pending_files(self):
        self.connection.update(backend="sftp", bucket="", remotePath="/test files")
        self.remote_config('[sftp]\ntype = sftp\n[volume]\ntype = union\n'
                           'upstreams = "old-icon-overlay:ro" "sftp:/test files/"\n')
        self.assertFalse((self.paths.base / "icon-overlay-sftp").exists())
        self.cache(self.complete())
        self.assertEqual(self.state(), "cached")
        self.cache(self.complete(Dirty=True))
        self.assertEqual(self.state(), "pending")

    def test_direct_sftp_config_takes_precedence_over_both_existing_overlays(self):
        self.connection.update(backend="sftp", bucket="", remotePath="/test files")
        overlay = self.paths.base / "icon-overlay-sftp"
        overlay.mkdir()
        for name in (".VolumeIcon.icns", "._.", "._.VolumeIcon.icns"):
            (overlay / name).touch()
        self.remote_config("[sftp]\ntype = sftp\n")
        self.cache(self.complete())
        self.assertEqual(self.state(), "online")
        self.cache(self.complete(Dirty=True), namespace="sftp/test files")
        self.assertEqual(self.state(), "pending")

    def test_malformed_or_oversized_remote_config_cannot_report_false_online(self):
        self.connection.update(backend="sftp", bucket="", remotePath="/test files")
        for contents in ("not an ini file", "", "[sftp]\ntype = s3\n",
                         "[sftp]\ntype = sftp\n[volume]\ntype = union\n",
                         "[sftp]\ntype = sftp\n" + "#" * badges.MAX_CONFIG):
            with self.subTest(contents=contents[:30]):
                self.remote_config(contents)
                self.assertEqual(self.state(), "unknown")

    def test_remote_config_read_rejects_symlinks_special_files_and_permission_errors(self):
        self.connection.update(backend="sftp", bucket="", remotePath="/test files")
        config = self.remote_config("[sftp]\ntype = sftp\n")
        outside = self.paths.base / "outside.conf"
        outside.write_text(config.read_text())
        config.unlink()
        config.symlink_to(outside)
        self.assertEqual(self.state(), "unknown")
        config.unlink()
        os.mkfifo(config)
        self.assertEqual(self.state(), "unknown")
        config.unlink()
        self.remote_config("[sftp]\ntype = sftp\n")
        with patch.object(badges.os, "open", side_effect=PermissionError):
            self.assertEqual(self.state(), "unknown")

    def test_sftp_roots_disable_photo_browser_without_exposing_server(self):
        self.connection.update(backend="sftp", host="private-host", user="private-user")
        status, response = self.request(self.bridge())
        self.assertEqual(status, 200)
        self.assertFalse(response["roots"][0]["supportsPhotoBrowser"])
        self.assertNotIn("private", json.dumps(response))

    def request(self, bridge, method="GET", route="/v1/roots", body=None, headers=None, auth=True):
        connection = http.client.HTTPConnection("127.0.0.1", bridge.server.server_address[1], timeout=3)
        self.addCleanup(connection.close)
        supplied = {"Authorization": "Bearer " + bridge.token} if auth else {}
        if body is not None:
            supplied["Content-Type"] = "application/json"
            body = json.dumps(body)
        supplied.update(headers or {})
        connection.request(method, route, body=body, headers=supplied)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_bridge_discovery_is_private_and_roots_omit_cloud_settings(self):
        bridge = self.bridge()
        config = json.loads(bridge.config.read_text())
        self.assertEqual(stat.S_IMODE(bridge.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(bridge.config.stat().st_mode), 0o600)
        self.assertEqual(config, {"version": 1, "url": bridge.url, "token": bridge.token})
        status, response = self.request(bridge)
        self.assertEqual(status, 200)
        self.assertEqual(set(response["roots"][0]), set(badges.ROOT_FIELDS))
        self.assertNotIn("private", json.dumps(response))

    def test_bridge_auth_origin_host_and_unknown_route_guards(self):
        bridge = self.bridge()
        for kwargs, expected in (({"auth": False}, 401),
                                 ({"headers": {"Authorization": "Bearer wrong"}}, 401),
                                 ({"headers": {"Origin": "http://untrusted.example"}}, 403),
                                 ({"headers": {"Host": "untrusted.example"}}, 403),
                                 ({"route": "/other"}, 404),
                                 ({"route": "/v1/roots?token=secret"}, 404)):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.request(bridge, **kwargs)[0], expected)

    def test_bridge_returns_only_requested_badges(self):
        self.cache(self.complete())
        bridge = self.bridge()
        paths = [self.root + "/photo.jpg", self.root + "/uncached.jpg"]
        status, response = self.request(bridge, "POST", "/v1/badges", {"paths": paths})
        self.assertEqual(status, 200)
        self.assertEqual(response, {"badges": [{"path": paths[0], "state": "cached"},
                                               {"path": paths[1], "state": "online"}]})

    def test_bridge_rejects_unbounded_or_invalid_batches(self):
        bridge = self.bridge()
        for payload in ({"paths": [self.root + "/p"] * 129}, {"paths": [1]}, {},
                        {"paths": [self.root + "/../secret"]}):
            self.assertEqual(self.request(bridge, "POST", "/v1/badges", payload)[0], 400)
        payload = {"paths": [self.root + "/" + "p" * badges.MAX_BODY]}
        self.assertEqual(self.request(bridge, "POST", "/v1/badges", payload)[0], 413)
        self.assertEqual(self.request(bridge, "POST", "/v1/badges", {"paths": []},
                                      headers={"Content-Type": "text/plain"})[0], 415)

    def test_stop_removes_only_own_discovery_config(self):
        bridge = self.bridge()
        bridge.stop()
        self.assertFalse(bridge.config.exists())
        successor = self.bridge()
        successor.config.write_text('{"token":"new-owner"}')
        successor.stop()
        self.assertEqual(json.loads(successor.config.read_text())["token"], "new-owner")

    def test_failed_start_can_be_stopped_without_waiting_for_a_server_thread(self):
        for failing in ("server_bind", "server_activate"):
            with self.subTest(failing=failing):
                bridge = badges.BadgeBridge(self.paths, lambda: [self.connection])
                with patch.object(badges.HTTPServer, failing, side_effect=OSError("unavailable")):
                    with self.assertRaises(OSError):
                        bridge.start()
                self.assertIsNone(bridge.server)
                self.assertIsNone(bridge.thread)
                bridge.stop()

    def test_stop_handles_a_server_that_never_started(self):
        bridge = badges.BadgeBridge(self.paths, lambda: [self.connection])
        bridge.server = badges.HTTPServer(("127.0.0.1", 0), badges.BaseHTTPRequestHandler)
        bridge.stop()
        self.assertIsNone(bridge.server)


if __name__ == "__main__":
    unittest.main()
